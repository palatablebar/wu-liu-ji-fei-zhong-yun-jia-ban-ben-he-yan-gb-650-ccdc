"""服务变更两账：服务成本账与降级损失账。

服务成本账：客户应收 = 实际服务成本 + 降级损失（含不可计费行的全额挂账）。
降级损失账：逐票损失之和 = 台账损失合计，且等于应收合计 − 成本合计。
两账都走 Decimal 到分，任何一侧不平都显式报差额，不隐藏降级损失。
"""

from __future__ import annotations

from decimal import Decimal

from app import store
from app.modules.m5_charges import money
from app.modules.m6_reconcile.compare import make_account


def cost_account(cost_rows: list[dict]) -> dict:
    cost = money.total([Decimal(row["total_amount"]) for row in cost_rows])
    loss = money.total([Decimal(row["downgrade_loss"]) for row in cost_rows])
    parts = [
        {"label": "实际服务成本", "value": cost},
        {"label": "降级损失", "value": loss},
    ]
    receivable = money.total([Decimal(row["receivable_amount"]) for row in cost_rows])
    return make_account("service_cost", "服务成本账", "客户应收", receivable, parts, "元")


def loss_account(cost_rows: list[dict]) -> dict:
    line_losses = money.total([Decimal(row["downgrade_loss"]) for row in cost_rows])
    receivable = money.total([Decimal(row["receivable_amount"]) for row in cost_rows])
    cost = money.total([Decimal(row["total_amount"]) for row in cost_rows])
    parts = [{"label": "逐票降级损失", "value": line_losses}]
    return make_account(
        "downgrade_loss",
        "降级损失账",
        "应收 − 成本",
        money.q2(receivable - cost),
        parts,
        "元",
    )


def accounts_for(run_id: str) -> list[dict]:
    cost_rows = store.cost_lines(run_id)
    if not cost_rows:
        return []
    return [cost_account(cost_rows), loss_account(cost_rows)]
