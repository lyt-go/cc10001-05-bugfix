"""采集任务业务规则：状态流转、字段校验与筛选口径都收在这里。

任务能否调度 / 启动，不能只看任务清单里的历史标记，必须实时读取关联解析规则
已落库的当前状态：

    已启用（且验证通过） -> 可以提交调度、可以启动
    规则验证中           -> 只能查看，不能启动
    草稿                 -> 尚未进入验证，不能调度
    已废弃 / 不存在      -> 受影响，禁止再次启动

已完成的采集任务与历史采集日志保持原样，不回写、不改写当时的规则结论。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.outcomes import (
    DUPLICATE_MESSAGE,
    ActionOutcome,
    receipt_key,
    remember,
    replay,
    request_id_from,
    stable_token,
)
from app.services.parserule import (
    STATUS_DRAFT as RULE_DRAFT,
    STATUS_VALIDATING as RULE_VALIDATING,
    STATUS_DEPRECATED as RULE_DEPRECATED,
    find_rule_by_number,
    rule_can_enable,
    rule_identity,
    validation_summary,
)
from app.store import store

MODULE = "taskcrawl"
RULE_FIELD = "规则编号"
REQUIRED_FIELDS = ["任务编号", "采集源名称", "规则编号"]
# 调度本身的字段完整性校验；字段齐全只是必要条件，不是准入的充分条件。
SCHEDULE_FIELDS = ["调度周期", "增量字段"]
STATUS_DRAFT = "草稿"
STATUS_SCHEDULED = "待调度"
STATUS_RUNNING = "执行中"
STATUS_DONE = "已完成"
STATUS_STOPPED = "已终止"
STATUS_ORDER = [STATUS_DRAFT, STATUS_SCHEDULED, STATUS_RUNNING, STATUS_DONE, STATUS_STOPPED]
ACTION_RULES = {"提交调度": STATUS_SCHEDULED, "启动任务": STATUS_RUNNING, "终止任务": STATUS_STOPPED}
ALLOWED_FROM = {
    "提交调度": {STATUS_DRAFT},
    "启动任务": {STATUS_SCHEDULED},
    "终止任务": {STATUS_DRAFT, STATUS_SCHEDULED, STATUS_RUNNING},
}
ACTIVE_STATUSES = {STATUS_DRAFT, STATUS_SCHEDULED, STATUS_RUNNING}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(values: dict[str, Any], field: str) -> str:
    return str(values.get(field) or "").strip()


def _rule_block_reason(number: str, rule: dict[str, Any] | None) -> str:
    """规则不可用时给任务侧的受影响原因。"""
    if rule is None:
        return f"规则 {number} 不存在或已归档，任务不能启动"
    status = rule.get("status")
    if status == RULE_VALIDATING:
        return f"规则 {number} 当前为「规则验证中」，只能查看，任务不能启动"
    if status == RULE_DEPRECATED:
        return (
            f"规则 {number} 已废弃，规则编号不能复活；受影响任务不能再次启动，"
            "请为规则新建下一代并启用后再调度"
        )
    if status == RULE_DRAFT:
        return f"规则 {number} 当前为「草稿」，尚未提交规则验证，任务不能启动"
    return f"规则 {number} 当前为「{status}」，任务不能启动"


def _rule_snapshot(rule: dict[str, Any] | None) -> dict[str, Any]:
    if rule is None:
        return {"规则状态": "规则缺失", "规则验证结论": "无法验证（规则不存在）"}
    return {
        "规则编号": rule.get("规则编号"),
        "规则状态": rule.get("status"),
        "规则验证结论": validation_summary(rule),
    }


def _rule_gate(entry: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    """按规则已落库的当前状态给出任务准入结论。

    返回 (是否可启动, 受影响原因, 规则快照)。规则不存在、状态不是已启用、
    或验证结论未通过，都按不可启动处理。已完成的采集记录不因规则后来失效而改写。
    """
    number = _text(entry, RULE_FIELD)
    if not number:
        return False, "任务未填写规则编号，无法确认解析规则", None
    rule = find_rule_by_number(number)
    if rule is None:
        return False, _rule_block_reason(number, None), None
    ready, _reason = rule_can_enable(rule)
    if not ready:
        reason = _rule_block_reason(number, rule)
        if "没有通过的验证结论" in _reason:
            reason = f"规则 {number} 没有通过的验证结论，任务不能启动"
        return False, reason, rule
    return True, "", rule


def _schedule_gap(entry: dict[str, Any]) -> str:
    """调度 / 启动前必须填写的字段；字段齐全也不能替代规则准入。"""
    missing = [field for field in SCHEDULE_FIELDS if not _text(entry, field)]
    return f"缺少调度字段：{'、'.join(missing)}" if missing else ""


def _available_actions(entry: dict[str, Any], ready: bool) -> list[str]:
    """按任务当前状态和规则准入结论给出可执行动作。"""
    status = entry.get("status")
    actions: list[str] = []
    if status == STATUS_DRAFT:
        actions = ["提交调度"] if ready else []
    elif status == STATUS_SCHEDULED:
        actions = ["启动任务"] if ready else []
    # 执行中的任务不再提供重复启动；已完成 / 已终止不能再拉起。
    if status in ACTIVE_STATUSES:
        actions.append("终止任务")
    return actions


def _present(entry: dict[str, Any]) -> dict[str, Any]:
    """输出副本：派生准入结论，冲突时以规则已落库状态为准。"""
    ready, reason, rule = _rule_gate(entry)
    gap = _schedule_gap(entry)
    row = dict(entry)
    snapshot = _rule_snapshot(rule)
    row.update(snapshot)
    row["rule_status"] = row["规则状态"]
    row["rule_ready"] = ready
    row["调度字段完整"] = not gap
    row["可启动"] = ready and not gap
    row["任务准入"] = "可启动" if row["可启动"] else "禁止启动"
    row["blocked_reason"] = "" if row["可启动"] else (gap or reason)
    row["受影响原因"] = row["blocked_reason"]
    row["可执行动作"] = _available_actions(entry, row["可启动"])
    row["available_actions"] = row["可执行动作"]
    if entry.get("status") == STATUS_DONE:
        # 已完成的历史记录保持当时的规则结论，只标记它不再被启动。
        row["历史规则结论"] = dict(entry.get("规则结论快照") or {})
        row["受影响原因"] = "任务已完成，历史采集记录保持原样，不再启动"
    history = entry.get("准入历史") or []
    if history:
        row["准入历史"] = [dict(item) for item in history]
    return row


def _find_by_number(number: str, *, exclude_id: int | None = None) -> dict[str, Any] | None:
    for row in store.rows(MODULE):
        if exclude_id is not None and int(row.get("id", 0)) == exclude_id:
            continue
        if str(row.get("任务编号") or "").strip() == number:
            return row
    return None


def _append_history(entry: dict[str, Any], outcome: ActionOutcome, action: str, rule: dict[str, Any] | None) -> None:
    """记录当时看到的规则结论；只追加，不改写历史。"""
    snapshot = _rule_snapshot(rule)
    entry.setdefault("准入历史", []).append({
        "time": _now(),
        "action": action,
        "ok": outcome.ok,
        "message": outcome.message,
        "规则编号": _text(entry, RULE_FIELD),
        "规则状态": snapshot["规则状态"],
        "规则验证结论": snapshot["规则验证结论"],
        "任务状态": entry.get("status"),
    })
    if outcome.ok and action == "启动任务":
        # 只有真正启动成功的那一次才固化为历史结论，供已完成任务回看。
        entry["规则结论快照"] = dict(snapshot, 记录时间=_now())


class TaskcrawlService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [_present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return _present(entry) if entry is not None else None

    def rule_status_board(self, numbers: list[str]) -> dict[str, dict[str, Any]]:
        """批量读取规则当前状态，供任务清单 / 任务详情对齐。"""
        board: dict[str, dict[str, Any]] = {}
        for number in {str(item or "").strip() for item in numbers if str(item or "").strip()}:
            rule = find_rule_by_number(number)
            if rule is None:
                board[number] = {
                    "规则编号": number,
                    "status": None,
                    "规则状态": "规则缺失",
                    "验证结论": "规则不存在",
                }
                continue
            board[number] = {
                "id": rule.get("id"),
                "规则编号": number,
                "status": rule.get("status"),
                "规则状态": rule.get("status"),
                "验证结论": validation_summary(rule),
            }
        return board

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values, field)]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        number = _text(values, "任务编号")
        if _find_by_number(number) is not None:
            return None, f"任务编号 {number} 已存在，不能重复登记"
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for key, value in values.items():
            entry[str(key)] = value
        entry["任务编号"] = number
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return _present(entry), ""

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> ActionOutcome:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return ActionOutcome(False, f"采集任务 {entry_id} 不存在或已归档")
        if action not in ACTION_RULES:
            return ActionOutcome(False, f"动作「{action}」不属于采集任务可执行范围")

        # 调度 / 启动前实时读取规则落库状态，清单上的旧标记不能作为准入依据。
        ready, reason, rule = _rule_gate(entry)
        request_id = request_id_from(values)
        # 回执键认“规则身份 + 任务编号”，不含两边当前状态：
        # 首个请求改了任务状态后，后到的重复请求仍命中同一回执返回“已处理”；
        # 规则后来补完验证 / 被废弃，规则身份变了，才会算作一次新的调度请求。
        context = stable_token({
            "rule": rule_identity(rule) if rule is not None else "missing",
            "task": _text(entry, "任务编号"),
        })
        key = receipt_key(MODULE, entry_id, action, context, request_id)
        # 同一调度请求只认首个成功回执；失败不写回执，下一次按规则当前状态重新评估。
        prior = replay(key)
        if prior is not None:
            prior.entry = _present(entry)
            return prior

        current = entry.get("status")
        target = ACTION_RULES[action]
        # “已处理”表示首个请求已经把任务推到目标状态；它必须先满足准入，
        # 规则失效时即便状态标记还停在待调度，也要给出受影响原因，不能回“已处理”。
        already_effective = action != "终止任务" and current == target

        if already_effective:
            gap_checked = _schedule_gap(entry)
            if not ready or gap_checked:
                reason_text = gap_checked or reason
                entry["abnormal"] = True
                outcome = ActionOutcome(False, f"{reason_text}；任务未{action}")
                _append_history(entry, outcome, action, rule)
                outcome.entry = _present(entry)
                return outcome
            outcome = ActionOutcome(True, DUPLICATE_MESSAGE, duplicate=True, detail="首个请求已生效")
            outcome.entry = _present(entry)
            return outcome

        if current not in ALLOWED_FROM[action]:
            message = f"任务当前为「{current}」，不能{action}"
            if current == STATUS_DONE:
                message += "；已完成的采集记录与历史日志保持原样"
            outcome = ActionOutcome(False, message)
            remember(key, outcome)
            outcome.entry = _present(entry)
            return outcome

        gap = _schedule_gap(entry)
        if not ready or gap:
            reason_text = gap or reason
            entry["abnormal"] = True
            outcome = ActionOutcome(False, f"{reason_text}；任务未{action}")
            _append_history(entry, outcome, action, rule)
            outcome.entry = _present(entry)
            remember(key, outcome)
            return outcome

        if action == "提交调度":
            entry["status"] = STATUS_SCHEDULED
            entry["pending"] = True
        elif action == "启动任务":
            entry["status"] = STATUS_RUNNING
            entry["pending"] = False
        else:  # 终止任务
            entry["status"] = STATUS_STOPPED
            entry["pending"] = False
        entry["abnormal"] = False
        outcome = ActionOutcome(True, f"采集任务已{action}")
        _append_history(entry, outcome, action, rule)
        outcome.entry = _present(entry)
        remember(key, outcome)
        return outcome
