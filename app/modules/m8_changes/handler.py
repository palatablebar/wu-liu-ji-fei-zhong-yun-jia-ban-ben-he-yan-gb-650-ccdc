"""M8 入口：服务变更工作台、变更接收（单条/批量）与成本台账查询。"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse

from app import store, web
from app.modules.m8_changes import service

router = APIRouter(tags=["changes"])


def _annotated(run_id: str) -> list[dict]:
    rows = service.cost_lines(run_id)
    for row in rows:
        row["has_money_loss"] = Decimal(row["downgrade_loss"]) > 0
    return rows


@router.get("/changes", response_class=HTMLResponse)
def page_changes(request: Request):
    context = web.shell(request, active="changes")
    run_id = context["run_id"]
    context.update({
        "changes": service.changes(run_id) if run_id else [],
        "cost_lines": _annotated(run_id) if run_id else [],
        "summary": service.summary(run_id) if run_id else {},
    })
    return web.page("changes.html", context)


@router.get("/api/changes")
def api_changes(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "summary": service.summary(run_id) if run_id else {},
        "changes": service.changes(run_id) if run_id else [],
        "cost_lines": service.cost_lines(run_id) if run_id else [],
    }


@router.post("/api/changes")
def api_receive(payload: dict = Body(...), run: str = ""):
    run_id = run or store.latest_run_id()
    if "changes" in payload and isinstance(payload["changes"], list):
        result = service.receive_many(run_id, payload["changes"])
    else:
        single = service.receive_change(run_id, payload)
        result = single
    return {"ok": True, "result": result}


@router.post("/api/changes/recompute")
def api_recompute(run: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "result": service.recompute(run_id)}
