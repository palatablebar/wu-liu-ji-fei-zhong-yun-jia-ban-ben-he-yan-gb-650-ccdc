"""M8 服务变更：降级重算、幂等重试、应收不动、损失台账、失效重算与导出一致。

用独立临时运行（与样例主流程同输入、独立 run 状态）承接变更，避免污染共享夹具。
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import store
from app.errors import DomainError
from app.modules.m1_waybill import service as waybills
from app.modules.m4_weights import service as weights
from app.modules.m5_charges import service as charges
from app.modules.m6_reconcile import service as reconcile
from app.modules.m7_export import changes_out, csv_out
from app.modules.m7_export import service as export
from app.modules.m8_changes import identity, service as changes


@pytest.fixture()
def change_run():
    """每个测试一条全新运行，跑完 M4/M5，未挂任何变更。"""
    ingest = waybills.import_sample()
    run_id = ingest["run_id"]
    weights.compute_weights(run_id)
    charges.compute_charges(run_id)
    yield run_id
    # 清理本测试写入的变更与成本，保持其它测试的共享运行干净
    with store.session() as conn:
        conn.execute("DELETE FROM service_changes WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM cost_lines WHERE run_id = ?", (run_id,))


def _charge(run_id: str, waybill_no: str) -> dict:
    return next(r for r in store.charge_lines(run_id) if r["waybill_no"] == waybill_no)


def _cost(run_id: str, waybill_no: str) -> dict:
    return next(r for r in store.cost_lines(run_id) if r["waybill_no"] == waybill_no)


# --- 接收与降级重算 --------------------------------------------------------

def test_downgrade_recalculates_actual_cost_and_loss(change_run):
    run_id = change_run
    receivable = Decimal(_charge(run_id, "WB-1002")["total_amount"])
    result = changes.receive_change(run_id, {
        "change_id": "C-1002", "waybill_no": "WB-1002",
        "from_service": "standard", "to_service": "economy",
    })
    assert result["change_type"] == identity.DOWNGRADE
    assert result["duplicate"] is False

    row = _cost(run_id, "WB-1002")
    assert row["billed_service"] == "standard"
    assert row["actual_service"] == "economy"
    assert row["change_id"] == "C-1002"
    assert "SERVICE_DOWNGRADED" in row["codes"]
    assert row["version_id"] == "SF-economy-Z1-v2"
    # 手工复算：economy v2 最低收费 17.10 + 燃油 17.10*0.085 + 保价 0 = 18.55
    assert row["total_amount"] == "18.55"
    assert Decimal(row["receivable_amount"]) == receivable
    assert Decimal(row["downgrade_loss"]) == receivable - Decimal("18.55")


def test_receivable_never_changes_when_cost_is_recalculated(change_run):
    run_id = change_run
    before = store.get_run(run_id)["amount_total"]
    changes.receive_change(run_id, {
        "change_id": "C-1002", "waybill_no": "WB-1002",
        "from_service": "standard", "to_service": "economy",
    })
    after = store.get_run(run_id)["amount_total"]
    assert before == after == "1521.04"
    # 发票口径仍是客户确认的原服务运费
    invoice = list(csv.DictReader(io.StringIO(export.invoice_csv(run_id))))
    invoice_total = sum(Decimal(r["total_amount"]) for r in invoice)
    assert str(invoice_total) == "1521.04"


def test_signed_loss_when_volumetric_divisor_makes_cost_higher(change_run):
    """档位降级不必然省钱：economy 除数 5000 使计费重变大，损失如实记负。"""
    run_id = change_run
    changes.receive_change(run_id, {
        "change_id": "C-1001", "waybill_no": "WB-1001",
        "from_service": "standard", "to_service": "economy",
    })
    row = _cost(run_id, "WB-1001")
    assert row["billable_kg"] == "6.000"
    assert Decimal(row["downgrade_loss"]) < 0
    # 带符号损失仍满足 应收 = 成本 + 损失
    assert Decimal(row["receivable_amount"]) == (
        Decimal(row["total_amount"]) + Decimal(row["downgrade_loss"])
    )


# --- 幂等：同一变更重试不新增差异 ------------------------------------------

def test_retry_same_change_id_is_idempotent(change_run):
    run_id = change_run
    payload = {
        "change_id": "DUP-1", "waybill_no": "WB-1002",
        "from_service": "standard", "to_service": "economy",
    }
    first = changes.receive_change(run_id, payload)
    second = changes.receive_change(run_id, payload)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["code"] == "CHANGE_DUPLICATE"
    assert store.change_count(run_id) == 1
    # 成本台账行数与金额不随重试变化
    rows_once = store.cost_lines(run_id)
    changes.receive_change(run_id, payload)
    assert store.cost_lines(run_id) == rows_once


def test_retry_without_change_id_uses_payload_digest(change_run):
    run_id = change_run
    payload = {"waybill_no": "WB-1017", "from_service": "standard", "to_service": "economy"}
    first = changes.receive_change(run_id, payload)
    second = changes.receive_change(run_id, dict(payload))
    assert first["change_id"].startswith("digest:")
    assert first["change_id"] == second["change_id"]
    assert second["duplicate"] is True
    assert store.change_count(run_id) == 1


def test_keyless_message_does_not_alias_explicit_change_id(change_run):
    """无号消息即使载荷与某条显式 id 变更相同，也不能冒认对方的 id。"""
    run_id = change_run
    explicit = changes.receive_change(run_id, {
        "change_id": "EXP-1", "waybill_no": "WB-1017",
        "from_service": "standard", "to_service": "economy",
    })
    keyless = changes.receive_change(run_id, {
        "waybill_no": "WB-1017", "from_service": "standard", "to_service": "economy",
    })
    assert explicit["duplicate"] is False
    assert keyless["duplicate"] is False
    assert keyless["change_id"].startswith("digest:")
    assert keyless["change_id"] != "EXP-1"
    assert store.change_count(run_id) == 2


def test_batch_dedup_counts_duplicates(change_run):
    run_id = change_run
    result = changes.receive_many(run_id, [
        {"change_id": "B1", "waybill_no": "WB-1017", "from_service": "standard", "to_service": "economy"},
        {"change_id": "B1", "waybill_no": "WB-1017", "from_service": "standard", "to_service": "economy"},
        {"change_id": "B2", "waybill_no": "WB-1018", "from_service": "standard", "to_service": "economy"},
    ])
    assert result["accepted"] == 2
    assert result["duplicates"] == 1
    assert store.change_count(run_id) == 2


def test_latest_sequence_wins_for_same_waybill(change_run):
    run_id = change_run
    changes.receive_change(run_id, {
        "change_id": "S1", "waybill_no": "WB-1002", "seq": 1,
        "from_service": "standard", "to_service": "economy",
    })
    changes.receive_change(run_id, {
        "change_id": "S2", "waybill_no": "WB-1002", "seq": 2,
        "from_service": "economy", "to_service": "standard",
    })
    active = store.active_changes(run_id)
    assert len(active) == 1
    assert active[0]["change_id"] == "S2"
    row = _cost(run_id, "WB-1002")
    assert row["actual_service"] == "standard"
    assert Decimal(row["downgrade_loss"]) == 0


# --- 失效重算 --------------------------------------------------------------

def test_upstream_recompute_invalidates_and_rebuilds_cost(change_run):
    run_id = change_run
    changes.receive_change(run_id, {
        "change_id": "R1", "waybill_no": "WB-1002",
        "from_service": "standard", "to_service": "economy",
    })
    snapshot = [(r["waybill_no"], r["total_amount"], r["downgrade_loss"])
                for r in store.cost_lines(run_id)]
    # M5 重新计费：旧成本结果必须失效并按新应收重算，结果逐票一致、无残余
    charges.compute_charges(run_id)
    rebuilt = [(r["waybill_no"], r["total_amount"], r["downgrade_loss"])
               for r in store.cost_lines(run_id)]
    assert rebuilt == snapshot
    assert len(store.cost_lines(run_id)) == len(store.charge_lines(run_id))


def test_recompute_is_deterministic(change_run):
    run_id = change_run
    changes.receive_change(run_id, {
        "change_id": "D1", "waybill_no": "WB-1002",
        "from_service": "standard", "to_service": "economy",
    })
    first = changes.recompute(run_id)
    second = changes.recompute(run_id)
    assert first == second


# --- 对账：六账相等，损失不被隐藏 ------------------------------------------

def test_reconcile_adds_two_balanced_change_accounts(change_run):
    run_id = change_run
    changes.receive_change(run_id, {
        "change_id": "A1", "waybill_no": "WB-1002",
        "from_service": "standard", "to_service": "economy",
    })
    report = reconcile.reconcile(run_id)
    keys = [a["key"] for a in report["accounts"]]
    assert keys == ["rows", "amount", "version", "weight", "service_cost", "downgrade_loss"]
    assert report["verdict"]["balanced"] is True
    by_key = {a["key"]: a for a in report["accounts"]}
    cost_account = by_key["service_cost"]
    assert cost_account["left_value"] == Decimal("1521.04")
    assert cost_account["parts"][0]["label"] == "实际服务成本"
    assert cost_account["parts"][1]["label"] == "降级损失"


def test_unrateable_change_books_full_receivable_as_loss(change_run):
    run_id = change_run
    receivable = Decimal(_charge(run_id, "WB-1003")["total_amount"])
    changes.receive_change(run_id, {
        "change_id": "U1", "waybill_no": "WB-1003",
        "from_service": "standard", "to_service": "express",
    })
    row = _cost(run_id, "WB-1003")
    assert "COST_UNRATEABLE" in row["codes"]
    assert "RATE_MISSING" in row["codes"]
    assert Decimal(row["total_amount"]) == 0
    assert Decimal(row["downgrade_loss"]) == receivable
    # 不可核金额显式挂账，对账仍然守恒
    assert changes.summary(run_id)["unrateable_rows"] == 1
    assert reconcile.reconcile(run_id)["verdict"]["balanced"] is True


def test_validation_errors(change_run):
    run_id = change_run
    with pytest.raises(DomainError) as exc:
        changes.receive_change(run_id, {"change_id": "E1", "waybill_no": "NOPE", "to_service": "economy"})
    assert exc.value.code == "CHANGE_WAYBILL_MISSING"
    with pytest.raises(DomainError) as exc:
        changes.receive_change(run_id, {
            "change_id": "E2", "waybill_no": "WB-1002", "carrier": "JD", "to_service": "economy",
        })
    assert exc.value.code == "CHANGE_CARRIER_MISMATCH"
    with pytest.raises(DomainError) as exc:
        changes.receive_change(run_id, {
            "change_id": "E2B", "waybill_no": "WB-1002", "to_service": "economy", "zone": "Z9",
        })
    assert exc.value.code == "CHANGE_CARRIER_MISMATCH"
    with pytest.raises(DomainError) as exc:
        changes.receive_change(run_id, {"change_id": "E3", "waybill_no": "WB-1002"})
    assert exc.value.code == "MISSING_KEY"


# --- 导出一致 --------------------------------------------------------------

def test_exports_are_byte_stable_and_carry_change_basis(change_run):
    run_id = change_run
    changes.receive_change(run_id, {
        "change_id": "X1", "waybill_no": "WB-1002",
        "from_service": "standard", "to_service": "economy",
    })
    assert export.changes_csv(run_id) == export.changes_csv(run_id)
    assert export.evidence_csv(run_id) == export.evidence_csv(run_id)

    change_rows = changes_out.rows(run_id)
    assert change_rows[0]["change_id"] == "X1"
    assert list(change_rows[0]) == changes_out.CHANGE_HEADER
    evidence = list(csv.DictReader(io.StringIO(export.evidence_csv(run_id))))
    flagged = next(r for r in evidence if r["waybill_no"] == "WB-1002")
    assert flagged["actual_service"] == "economy"
    assert flagged["change_id"] == "X1"
    assert Decimal(flagged["downgrade_loss"]) == Decimal(flagged["receivable_amount"]) - Decimal(flagged["cost_total"])
    # 证据 CSV 原有列保持在表头前部，兼容旧消费方
    assert csv_out.HEADER[:5] == ["line_no", "waybill_no", "carrier", "service", "zone"]


def test_http_endpoints_and_pages():
    # 显式锚定样例主流程 run：其它测试会导入截断/空文件产生更新但无运单的 run，
    # 不能依赖 latest_run_id()，真实工作台同样以 run_id 精确寻址。
    run_id = waybills.import_sample()["run_id"]
    if not store.charge_lines(run_id):
        weights.compute_weights(run_id)
        charges.compute_charges(run_id)
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        assert client.get("/changes").status_code == 200
        payload = {"change_id": "HTTP-M8-UNIQUE", "waybill_no": "WB-1019",
                   "from_service": "standard", "to_service": "economy"}
        try:
            created = client.post(f"/api/changes?run={run_id}", json=payload)
            assert created.status_code == 200
            retry = client.post(f"/api/changes?run={run_id}", json=payload)
            assert retry.json()["result"]["duplicate"] is True
            assert client.get(f"/api/export/service-changes.csv?run={run_id}").status_code == 200
        finally:
            with store.session() as conn:
                conn.execute("DELETE FROM service_changes WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM cost_lines WHERE run_id = ?", (run_id,))
