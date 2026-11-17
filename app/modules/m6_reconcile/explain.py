"""差异归因：把不平的账换成一句可复核的话，平的账只给结论。"""

from __future__ import annotations

from decimal import Decimal

BALANCED = "两侧相等"
ROW_TEMPLATE = "差 {delta} 行：采信与下游行数不等"
AMOUNT_TEMPLATE = "差 {delta} 元：分项之和与应收不等"
VERSION_TEMPLATE = "差 {delta} 行：有运单未落到单一版本"
WEIGHT_TEMPLATE = "差 {delta} 行：计费重来源分类不闭合"
COST_TEMPLATE = "差 {delta} 元：实际成本与降级损失之和不等于客户应收"
LOSS_TEMPLATE = "差 {delta} 元：逐票损失之和不等于应收减成本"

TEMPLATES = {
    "rows": ROW_TEMPLATE,
    "amount": AMOUNT_TEMPLATE,
    "version": VERSION_TEMPLATE,
    "weight": WEIGHT_TEMPLATE,
    "service_cost": COST_TEMPLATE,
    "downgrade_loss": LOSS_TEMPLATE,
}


def note(account: dict) -> str:
    if account["balanced"]:
        return BALANCED
    template = TEMPLATES.get(account["key"], "差 {delta}")
    return template.format(delta=account["delta"])


def annotated(accounts: list[dict]) -> list[dict]:
    for account in accounts:
        account["note"] = note(account)
    return accounts


def verdict(accounts: list[dict]) -> dict:
    broken = [account["key"] for account in accounts if not account["balanced"]]
    return {
        "balanced": not broken,
        "broken": broken,
        "count": len(accounts),
    }


def money_delta(value: Decimal) -> str:
    return f"{value:,.2f}"
