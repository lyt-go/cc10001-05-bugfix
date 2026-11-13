"""解析规则接口：维护解析规则，覆盖提交规则验证、启用规则、废弃规则等动作。

状态只能逐级推进：草稿 -> 规则验证中 -> 已启用 -> 已废弃。启用必须先落规则
验证中并通过测试；已废弃规则只能新建下一代，不能把旧编号复活。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.parserule import ParseruleService

router = APIRouter(prefix="/api/parserule", tags=["解析规则"])

service = ParseruleService()

LIST_FIELDS = ["规则编号", "页面选择器", "内容选择器", "属性提取器", "列表容错", "空值占位符", "字符清洗正则", "输出字段映射"]
STATUSES = ["草稿", "规则验证中", "已启用", "已废弃"]


def _ensure(entry_id: int) -> None:
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"解析规则 {entry_id} 不存在或已归档")


@router.get("/status", response_model=dict)
def rule_status(numbers: str = Query(default="", description="逗号分隔的规则编号")) -> dict:
    """批量读取规则已落库的当前状态，供任务清单 / 任务详情对齐使用。"""
    wanted = [item.strip() for item in numbers.split(",") if item.strip()]
    return {"items": service.project_rules(wanted)}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则验证中、已启用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤解析规则列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条解析规则，缺字段或编号重复时说明原因而不是静默丢弃。"""
    entry, message = service.create_entry(payload.merged_values())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="解析规则已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改规则字段并重新执行规则测试；已废弃规则不能再改。"""
    _ensure(entry_id)
    entry, message = service.update_entry(entry_id, payload.merged_values())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=f"解析规则已更新；{entry.get('验证结论')}", entry=entry)


@router.post("/{entry_id}/next-generation", response_model=ActionResult)
def create_next_generation(entry_id: int, payload: EntryPayload) -> ActionResult:
    """为已废弃规则新建下一代：换新编号重新走草稿到验证流程，旧编号保持废弃。"""
    _ensure(entry_id)
    entry, message = service.create_next_generation(entry_id, payload.merged_values())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=f"已新建下一代规则 {entry.get('规则编号')}，请从草稿重新提交规则验证",
        entry=entry,
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条解析规则执行提交规则验证、启用规则、废弃规则；不允许的动作会被拦下并说明原因。"""
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
    """导出解析规则清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "parserule", "total": total, "items": items}
