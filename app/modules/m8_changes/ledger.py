"""实际服务成本重算与降级损失台账。

口径：
- 客户应收（charge_lines）是客户已确认的原服务运费，本模块绝不改写，
  发票与对账的应收口径保持不变。
- 实际成本（cost_lines）按变更后的实际服务重算；没有变更的运单快照应收行，
  成本等于应收、损失为 0。
- 降级损失 = 应收 − 成本，全程 Decimal 量化到分；损失为负（升级）如实记负，
  不做归零，保证可核。
重算采用「整票全量替换」，同一输入重算结果逐分相同。
"""

from __future__ import annotations

from decimal import Decimal

from app import store
from app.modules.m2_rates import service as rates
from app.modules.m4_weights import billable
from app.modules.m5_charges import money
from app.modules.m8_changes import costing, identity
from app.reasons import sort_codes


def _snapshot(charge: dict, waybill: dict) -> dict:
    """无变更运单：成本直接快照应收，两侧天然相等。"""
    return {
        "waybill_no": charge["waybill_no"],
        "change_id": "",
        "billed_service": waybill["service"],
        "actual_service": waybill["service"],
        "version_id": charge["version_id"],
        "billable_kg": "",
        "base_amount": charge["base_amount"],
        "step_amount": charge["step_amount"],
        "remote_amount": charge["remote_amount"],
        "fuel_amount": charge["fuel_amount"],
        "insurance_amount": charge["insurance_amount"],
        "total_amount": charge["total_amount"],
        "receivable_amount": charge["total_amount"],
        "downgrade_loss": "0.00",
        "codes": "",
    }


def _cost_line(waybill: dict, charge: dict, change: dict, index_map: dict) -> dict:
    """有变更运单：按实际服务重算计费重与运费，再与应收对损失。"""
    actual_service = change["to_service"]
    card, select_codes = costing.select_card(index_map, waybill, actual_service)
    receivable = money.q2(Decimal(charge["total_amount"]))

    if card is None:
        # 实际服务在提货时刻没有可用运价：成本不可核，单列不可计费，不吞差异
        return {
            "waybill_no": waybill["waybill_no"],
            "change_id": change["change_id"],
            "billed_service": waybill["service"],
            "actual_service": actual_service,
            "version_id": "",
            "billable_kg": "",
            "base_amount": "0.00",
            "step_amount": "0.00",
            "remote_amount": "0.00",
            "fuel_amount": "0.00",
            "insurance_amount": "0.00",
            "total_amount": "0.00",
            "receivable_amount": str(receivable),
            "downgrade_loss": str(receivable),
            "codes": ",".join(sort_codes(select_codes + ["COST_UNRATEABLE"])),
        }

    weight = costing.weight_for(waybill, card)
    priced = costing.charge_for(waybill, card, weight["steps"])
    cost = money.q2(Decimal(priced["total_amount"]))
    loss = money.q2(receivable - cost)

    direction = identity.classify(change["from_service"], actual_service)
    direction_code = "SERVICE_DOWNGRADED" if direction == identity.DOWNGRADE else "SERVICE_CHANGED"
    codes = list(priced["codes"])
    if weight["source"] == billable.VOLUMETRIC:
        codes.append("VOLUMETRIC_APPLIED")
    codes.append(direction_code)

    return {
        "waybill_no": waybill["waybill_no"],
        "change_id": change["change_id"],
        "billed_service": waybill["service"],
        "actual_service": actual_service,
        "version_id": weight["version_id"],
        "billable_kg": weight["billable_kg"],
        "base_amount": priced["base_amount"],
        "step_amount": priced["step_amount"],
        "remote_amount": priced["remote_amount"],
        "fuel_amount": priced["fuel_amount"],
        "insurance_amount": priced["insurance_amount"],
        "total_amount": str(cost),
        "receivable_amount": str(receivable),
        "downgrade_loss": str(loss),
        "codes": ",".join(sort_codes(codes)),
    }


def recompute(run_id: str) -> dict:
    """按当前变更台账全量重算成本账。无应收行时清空成本并返回空统计。"""
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    charges = {row["waybill_no"]: row for row in store.charge_lines(run_id)}
    changes = {row["waybill_no"]: row for row in store.active_changes(run_id)}
    index_map = rates.index()

    lines: list[dict] = []
    for waybill_no, charge in sorted(charges.items()):
        waybill = waybills.get(waybill_no)
        if waybill is None:
            continue
        change = changes.get(waybill_no)
        if change is None:
            lines.append(_snapshot(charge, waybill))
        else:
            lines.append(_cost_line(waybill, charge, change, index_map))

    store.replace_cost_lines(run_id, lines)
    return summarize(run_id)


def _sum(rows: list[dict], field: str) -> Decimal:
    return money.total([Decimal(row[field]) for row in rows])


def summarize(run_id: str) -> dict:
    rows = store.cost_lines(run_id)
    changed = [row for row in rows if row["change_id"]]
    downgraded = [row for row in changed if "SERVICE_DOWNGRADED" in row["codes"]]
    money_loss = [row for row in changed if Decimal(row["downgrade_loss"]) > 0]
    unrateable = [row for row in rows if "COST_UNRATEABLE" in row["codes"]]
    receivable = _sum(rows, "receivable_amount")
    cost = _sum(rows, "total_amount")
    loss = _sum(rows, "downgrade_loss")
    return {
        "run_id": run_id,
        "cost_rows": len(rows),
        "changed_rows": len(changed),
        "downgraded_rows": len(downgraded),
        "money_loss_rows": len(money_loss),
        "unrateable_rows": len(unrateable),
        "receivable_total": str(receivable),
        "cost_total": str(cost),
        "downgrade_loss_total": str(loss),
        "conserved": money.conserved(receivable, [cost, loss]),
    }
