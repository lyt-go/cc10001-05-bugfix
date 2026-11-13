"""采集任务接口：维护采集任务，覆盖提交调度、启动任务、终止任务等动作。

任务的准入结论必须实时读取关联解析规则的落库状态：规则验证中只能查看，
已废弃的规则禁止再次启动，已完成的采集记录与历史日志保持原样。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.taskcrawl import TaskcrawlService

router = APIRouter(prefix="/api/taskcrawl", tags=["采集任务"])

service = TaskcrawlService()

LIST_FIELDS = ["任务编号", "采集源名称", "规则编号", "调度周期", "增量字段", "批次大小", "起始水位", "截止水位"]
STATUSES = ["草稿", "待调度", "执行中", "已完成", "已终止"]


@router.get("/rule-status", response_model=dict)
def rule_status(numbers: str = Query(default="", description="逗号分隔的规则编号")) -> dict:
    """读取任务引用规则的当前状态，任务清单与规则详情冲突时以此为准。"""
    wanted = [item.strip() for item in numbers.split(",") if item.strip()]
    return {"items": service.rule_status_board(wanted)}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、待调度、执行中、已完成、已终止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤采集任务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采集任务，缺字段或编号重复时说明原因而不是静默丢弃。"""
    entry, message = service.create_entry(payload.merged_values())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="采集任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条采集任务执行提交调度、启动任务、终止任务；规则状态不允许时会被拦下并说明原因。"""
    values = payload.merged_values()
    action = str(values.get("action") or "").strip()
    outcome = service.run_action(entry_id, action, values)
    return ActionResult(
        ok=outcome.ok,
        message=outcome.message,
        entry=outcome.entry,
        duplicate=outcome.duplicate,
    )


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "taskcrawl", "total": total, "items": items}
