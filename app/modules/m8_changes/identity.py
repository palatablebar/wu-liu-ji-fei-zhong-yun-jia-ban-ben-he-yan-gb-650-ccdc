"""服务变更的规范化、幂等键与降级判定。

幂等键优先取上游变更号 change_id；上游没给时用规范化载荷的 sha256 兜底，
保证同一条变更无论重试多少次，在 (run_id, 键) 上只落一条、只产生一份差异。
"""

from __future__ import annotations

import hashlib
from typing import Any

from app import manifest

DOWNGRADE = "downgrade"
UPGRADE = "upgrade"
SAME = "same"

# 服务档位表：数字越小越快（越贵）。只用于判定变更方向与原因码，不参与计价。
SERVICE_TIER: dict[str, int] = {
    "express": 0,
    "standard": 1,
    "economy": 2,
}


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_change(payload: dict) -> dict:
    """把入参归一成稳定字段；缺省项从运单补，保证同一变更重试载荷一致。"""
    waybill_no = normalize_text(payload.get("waybill_no"))
    from_service = normalize_text(payload.get("from_service")).lower()
    to_service = normalize_text(payload.get("to_service")).lower()
    return {
        "change_id": normalize_text(payload.get("change_id")),
        "waybill_no": waybill_no,
        "from_service": from_service,
        "to_service": to_service,
        "source": normalize_text(payload.get("source")) or "carrier",
        "seq": int(payload.get("seq") or 0),
    }


def payload_digest(normalized: dict) -> str:
    """无 change_id 时的兜底幂等键，只由业务内容决定，与送达时间无关。"""
    seed = manifest.dumps([
        normalized["waybill_no"], normalized["from_service"],
        normalized["to_service"], normalized["source"], normalized["seq"],
    ])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def idempotency_key(normalized: dict, digest: str) -> str:
    return normalized["change_id"] or f"digest:{digest[:16]}"


def classify(from_service: str, to_service: str) -> str:
    """按服务档位判定方向；未知档位时只要服务名不同就记为变更，不臆断升降。"""
    if from_service == to_service:
        return SAME
    left = SERVICE_TIER.get(from_service)
    right = SERVICE_TIER.get(to_service)
    if left is None or right is None:
        return SAME if from_service == to_service else "changed"
    if right > left:
        return DOWNGRADE
    if right < left:
        return UPGRADE
    return SAME
