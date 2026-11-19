"""种子页段清单接口：页段边界查询、阶段顺向推进、版本冲突处理与中断恢复。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.seed_segment import SeedSegmentService

router = APIRouter(prefix="/api/seed_segment", tags=["种子页段清单"])

service = SeedSegmentService()

LIST_FIELDS = ["段编号", "关联种子", "分页参数", "页段范围", "游标起点", "当前游标", "已完成页数", "清单版本"]
STATUSES = ["待采集", "采集中", "已完成", "已归档"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按段编号检索"),
    status: str | None = Query(default=None, description="待采集、采集中、已完成、已归档"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、段编号关键字与阶段过滤种子页段清单，按版本优先级排序。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="seed_segment.csv"'})


@router.get("/manifest/{seed_id}")
def get_manifest(seed_id: int) -> dict:
    """按版本优先级查看某个种子的页段清单：页段范围、游标起点、剩余页与状态记录。"""
    view = service.manifest_view(seed_id)
    if not view.get("ok"):
        raise HTTPException(status_code=404, detail=view.get("error"))
    return view


@router.post("/resolve_conflict")
def resolve_conflict(payload: dict[str, object] = Body(default_factory=dict)) -> dict:
    """处理版本冲突：strategy 为「沿用旧游标」或「新检查点」。"""
    seed_id = payload.get("seed_id")
    strategy = str(payload.get("strategy") or "").strip()
    if seed_id is None:
        return {"ok": False, "error": "缺少seed_id"}
    if not strategy:
        return {"ok": False, "error": "缺少strategy"}
    return service.resolve_conflict(int(seed_id), strategy)


@router.post("/resume")
def resume_unfinished(payload: dict[str, object] = Body(default_factory=dict)) -> dict:
    """中断恢复：按版本优先级扫描清单，只续做未完成项。"""
    seed_id = payload.get("seed_id")
    if seed_id is None:
        return {"ok": False, "error": "缺少seed_id"}
    return service.resume_unfinished(int(seed_id))


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条种子页段明细（含按先后顺序排列的状态记录）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"种子页段 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条种子页段，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="种子页段已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条种子页段执行开始采集、标记完成、归档页段、恢复续采；阶段只能顺向变更。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
