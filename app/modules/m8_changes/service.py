"""M8 编排：接收承运商服务变更，幂等落账后全量重算实际成本。

设计要点：
- 幂等：优先用上游 change_id，缺失时用规范化载荷摘要兜底；同一变更重试只返回
  既有记录，不新增第二条差异（CHANGE_DUPLICATE 仅作返回提示，不重复落库）。
- 校验：运单必须存在、承运商必须一致；实际服务缺运价不拒绝接收，而是在成本
  重算时落成 COST_UNRATEABLE，把不可核金额显式挂出来。
- 失效重算：每次接收有效变更都整票重算成本账，上游 M4/M5 重算后也会调用
  recompute，旧成本结果被整体替换，不存在陈旧成本与新应收并存。
"""

from __future__ import annotations

from app import manifest, store
from app.errors import DomainError
from app.modules.m8_changes import identity, ledger


def receive_change(run_id: str, payload: dict) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)

    normalized = identity.normalize_change(payload)
    if not normalized["waybill_no"]:
        raise DomainError("MISSING_KEY", "服务变更缺少运单号", 422)
    if not normalized["to_service"]:
        raise DomainError("MISSING_KEY", "服务变更缺少实际服务 to_service", 422)

    waybill = _waybill(run_id, normalized["waybill_no"])
    if not normalized["from_service"]:
        normalized["from_service"] = waybill["service"]
    carrier = payload.get("carrier") or waybill["carrier"]
    carrier = str(carrier).strip().upper()
    if carrier != waybill["carrier"]:
        raise DomainError(
            "CHANGE_CARRIER_MISMATCH",
            f"变更承运商 {carrier} 与运单 {waybill['carrier']} 不符",
            422,
        )

    digest = identity.payload_digest(normalized)
    change_id = identity.idempotency_key(normalized, digest)
    duplicate = store.get_change(run_id, change_id)
    # 摘要兜底只在「同样没有上游 change_id」的记录之间匹配，
    # 不能让无号消息命中一条显式 id 的变更而被误判成重复。
    if duplicate is None and not normalized["change_id"]:
        by_digest = store.get_change_by_digest(run_id, digest)
        if by_digest is not None and by_digest["change_id"].startswith("digest:"):
            duplicate = by_digest
    if duplicate is not None:
        return {
            "run_id": run_id,
            "change_id": duplicate["change_id"],
            "duplicate": True,
            "code": "CHANGE_DUPLICATE",
            "detail": "同一服务变更已接收，重试不新增差异",
            "summary": ledger.summarize(run_id),
        }

    # 服务变更不改分区：成本重算始终按运单原分区择版，台账也只记录运单分区，
    # 入参里的 zone 仅作校验，不一致直接拒绝，避免存一个未被使用的冲突值。
    zone_name = waybill["zone"]
    payload_zone = str(payload.get("zone") or "").strip().upper()
    if payload_zone and payload_zone != zone_name:
        raise DomainError(
            "CHANGE_CARRIER_MISMATCH",
            f"变更分区 {payload_zone} 与运单 {zone_name} 不符，服务变更不允许改分区",
            422,
        )
    direction = identity.classify(normalized["from_service"], normalized["to_service"])
    now = manifest.utc_now_iso()
    record = {
        "change_id": change_id,
        "waybill_no": normalized["waybill_no"],
        "carrier": carrier,
        "from_service": normalized["from_service"],
        "to_service": normalized["to_service"],
        "zone": zone_name,
        "change_type": direction,
        "source": normalized["source"],
        "seq": normalized["seq"],
        "status": "received",
        "detail": str(payload.get("detail") or ""),
        "digest": digest,
        "received_at": now,
    }
    store.insert_change(run_id, record)
    store.add_stage(run_id, "changes", now)
    summary = ledger.recompute(run_id)
    return {
        "run_id": run_id,
        "change_id": change_id,
        "duplicate": False,
        "change_type": direction,
        "summary": summary,
    }


def _waybill(run_id: str, waybill_no: str) -> dict:
    for row in store.waybills(run_id):
        if row["waybill_no"] == waybill_no:
            return row
    raise DomainError(
        "CHANGE_WAYBILL_MISSING",
        f"变更指向的运单不存在：{waybill_no}",
        422,
    )


def receive_many(run_id: str, payloads: list[dict]) -> dict:
    """批量接收：逐条幂等，返回接收/重复计数与最终台账统计。"""
    accepted = 0
    duplicates = 0
    last_change_id = ""
    for payload in payloads:
        result = receive_change(run_id, payload)
        last_change_id = result["change_id"]
        if result["duplicate"]:
            duplicates += 1
        else:
            accepted += 1
    return {
        "run_id": run_id,
        "accepted": accepted,
        "duplicates": duplicates,
        "last_change_id": last_change_id,
        "summary": ledger.summarize(run_id),
    }


def changes(run_id: str) -> list[dict]:
    return store.service_changes(run_id)


def cost_lines(run_id: str) -> list[dict]:
    return store.cost_lines(run_id)


def recompute(run_id: str) -> dict:
    """供上游 M4/M5 重算后调用：让基于旧计费结果的成本账失效并重算。"""
    if not store.get_run(run_id):
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    return ledger.recompute(run_id)


def summary(run_id: str) -> dict:
    return ledger.summarize(run_id)
