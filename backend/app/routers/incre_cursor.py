"""增量游标接口：维护增量标记，覆盖提交校验、标记采集中、复位游标等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.incre_cursor import IncreCursorService

router = APIRouter(prefix="/api/incre_cursor", tags=["增量游标"])

service = IncreCursorService()

LIST_FIELDS = ["标记编号", "关联种子", "增量字段", "上次位置", "当前位置", "增量类型", "覆写策略", "更新间隔"]
STATUSES = ["草稿", "待校验", "采集中", "已复位"]


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
    keyword: str | None = Query(default=None, description="按标记编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、采集中、已复位"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、标记编号关键字与状态过滤增量游标列表。"""
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
    output.write("\ufeff")
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
        headers={"Content-Disposition": 'attachment; filename="incre_cursor.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条增量标记明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"增量标记 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条增量标记，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="增量标记已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条增量标记执行提交校验、标记采集中、复位游标；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/advance")
def advance_cursor(entry_id: int, payload: dict[str, object]) -> dict:
    """推进游标到新位置。"""
    new_pos = payload.get("position")
    if new_pos is None:
        return {"ok": False, "error": "缺少position"}
    return service.advance_cursor(entry_id, str(new_pos))


@router.post("/{entry_id}/rollback")
def rollback_to_checkpoint(entry_id: int, payload: dict[str, object]) -> dict:
    """回滚到指定检查点。"""
    idx = payload.get("index")
    if idx is None:
        return {"ok": False, "error": "缺少index"}
    return service.rollback_to_checkpoint(entry_id, int(idx))


@router.post("/{entry_id}/build_checkpoint")
def build_checkpoint(entry_id: int) -> dict:
    """读取种子页段清单，只从当前可写页段的起点建立检查点。"""
    return service.build_checkpoint_from_manifest(entry_id)


@router.post("/{entry_id}/consume")
def consume_pages(entry_id: int, payload: dict[str, object] = Body(default_factory=dict)) -> dict:
    """在可写页段内消费页码，回写已消费页段。"""
    pages = int(payload.get("pages") or 1)
    return service.consume_page(entry_id, pages)
