"""服务变更台账导出：变更依据 + 客户应收 + 实际服务成本 + 降级损失，逐票可核。

与账单/证据 CSV 共用同一批库表，同状态下重复导出字节一致（确定性排序、
Decimal 定长字符串、时间戳只取库内已存值）。
"""

from __future__ import annotations

import csv
import io

from app import store
from app.reasons import label

CHANGE_HEADER = [
    "waybill_no", "carrier", "change_id", "from_service", "to_service",
    "change_type", "source", "seq", "status", "received_at",
    "billed_service", "actual_service", "version_id", "billable_kg",
    "receivable_amount", "cost_total", "downgrade_loss",
    "reason_codes", "reason_labels",
]


def _cost_index(run_id: str) -> dict:
    return {row["waybill_no"]: row for row in store.cost_lines(run_id)}


def rows(run_id: str) -> list[dict]:
    costs = _cost_index(run_id)
    merged: list[dict] = []
    for change in store.service_changes(run_id):
        cost = costs.get(change["waybill_no"], {})
        codes = [c for c in (cost.get("codes") or "").split(",") if c]
        merged.append({
            "waybill_no": change["waybill_no"],
            "carrier": change["carrier"],
            "change_id": change["change_id"],
            "from_service": change["from_service"],
            "to_service": change["to_service"],
            "change_type": change["change_type"],
            "source": change["source"],
            "seq": change["seq"],
            "status": change["status"],
            "received_at": change["received_at"],
            "billed_service": cost.get("billed_service", change["from_service"]),
            "actual_service": cost.get("actual_service", change["to_service"]),
            "version_id": cost.get("version_id", ""),
            "billable_kg": cost.get("billable_kg", ""),
            "receivable_amount": cost.get("receivable_amount", "0.00"),
            "cost_total": cost.get("total_amount", "0.00"),
            "downgrade_loss": cost.get("downgrade_loss", "0.00"),
            "reason_codes": " ".join(codes),
            "reason_labels": " ".join(label(c) for c in codes),
        })
    merged.sort(key=lambda row: (row["waybill_no"], row["seq"], row["change_id"]))
    return merged


def csv_text(run_id: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CHANGE_HEADER)
    for row in rows(run_id):
        writer.writerow([row[key] for key in CHANGE_HEADER])
    return buffer.getvalue()
