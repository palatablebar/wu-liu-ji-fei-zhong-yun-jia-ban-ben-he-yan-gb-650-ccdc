"""按指定服务重算一票的计费重与运费。

服务变更后重算实际成本必须与 M4/M5 走同一套规则（同一择版半开区间、同一
体积重除数、同一 0.5 kg 进位、同一分项叠加顺序），因此这里直接复用既有规则
函数，只把入参服务换成变更后的实际服务，避免两套算法漂移。
"""

from __future__ import annotations

from decimal import Decimal

from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable, rounding, volumetric
from app.modules.m5_charges import addon, money, zone


def weight_for(waybill: dict, card: dict) -> dict:
    """与 M4 完全一致：体积重 → 取大 → 按台阶进位 → 算续重段数。"""
    actual = Decimal(waybill["weight_kg"])
    volume = volumetric.volumetric_kg(
        Decimal(waybill["length_cm"]),
        Decimal(waybill["width_cm"]),
        Decimal(waybill["height_cm"]),
        int(card["volume_divisor"]),
    )
    raw, source = billable.pick(actual, volume)
    step = Decimal(card["step_weight_kg"])
    billable_kg = rounding.ceil_to_step(raw, step)
    steps = rounding.steps_above(billable_kg, Decimal(card["first_weight_kg"]), step)
    return {
        "actual_kg": str(actual),
        "volumetric_kg": str(volume),
        "source": source,
        "billable_kg": str(billable_kg),
        "steps": steps,
        "version_id": card["version_id"],
    }


def charge_for(waybill: dict, card: dict, steps: int) -> dict:
    """与 M5 完全一致：首重 + 续重 + 兜底 + 偏远 + 燃油 + 保价，分项先量化到分。"""
    first = zone.first_amount(card)
    step_part = zone.step_amount(steps, card)
    topup = zone.min_topup(first + step_part, card)
    base = money.q2(first + topup)
    remote = addon.remote_amount(card)
    fuel = addon.fuel_amount(base, step_part, card)
    declared = Decimal(waybill["declared_value"])
    insurance = addon.insurance_amount(declared, card)
    amount = money.total([base, step_part, remote, fuel, insurance])

    codes: list[str] = []
    if topup > 0:
        codes.append("MIN_CHARGE_APPLIED")
    if remote > 0:
        codes.append("ZONE_REMOTE")
    return {
        "base_amount": str(base),
        "step_amount": str(step_part),
        "remote_amount": str(remote),
        "fuel_amount": str(fuel),
        "insurance_amount": str(insurance),
        "total_amount": str(amount),
        "codes": codes,
    }


def select_card(index_map: dict, waybill: dict, service: str):
    """按给定服务（而非运单原始服务）在提货时刻择版。"""
    pickup = rateversion.parse_utc(waybill["pickup_utc"])
    return rates.select_card(
        index_map, waybill["carrier"], service, waybill["zone"], pickup
    )
