"""解析规则业务规则：状态流转、字段校验与筛选口径都收在这里。

规则状态只能逐级推进：

    草稿 --提交规则验证--> 规则验证中 --验证通过后启用规则--> 已启用
                              |                                  |
                              +--------------废弃规则-------------+--> 已废弃

草稿不能一步跳到已启用；规则验证中没有通过验证项也不能启用；已废弃的规则编号
不能复活，只能新建下一代规则。
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
from app.store import store

MODULE = "parserule"
REQUIRED_FIELDS = ["规则编号", "页面选择器", "内容选择器"]
# 规则测试要逐项确认的字段：缺任何一项都不能启用。
VALIDATION_FIELDS = ["页面选择器", "内容选择器", "属性提取器", "列表容错", "空值占位符"]
VALIDATION_MESSAGES = {
    "页面选择器": "页面选择器缺失，无法定位商品卡片",
    "内容选择器": "内容选择器缺失，无法提取标题和价格",
    "属性提取器": "属性提取器缺失，无法读取 href",
    "列表容错": "列表容错尚未验证，无法确认列表解析",
    "空值占位符": "空值占位符尚未验证，无法确认空值处理",
}

STATUS_DRAFT = "草稿"
STATUS_VALIDATING = "规则验证中"
STATUS_ENABLED = "已启用"
STATUS_DEPRECATED = "已废弃"
STATUS_ORDER = [STATUS_DRAFT, STATUS_VALIDATING, STATUS_ENABLED, STATUS_DEPRECATED]
ACTION_RULES = {
    "提交规则验证": STATUS_VALIDATING,
    "重新验证规则": STATUS_VALIDATING,
    "启用规则": STATUS_ENABLED,
    "废弃规则": STATUS_DEPRECATED,
}
# 每个动作只允许从这些状态发起，跨级请求直接拒绝。
ALLOWED_FROM = {
    "提交规则验证": {STATUS_DRAFT},
    "重新验证规则": {STATUS_VALIDATING},
    "启用规则": {STATUS_VALIDATING},
    "废弃规则": {STATUS_DRAFT, STATUS_VALIDATING, STATUS_ENABLED},
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(values: dict[str, Any], field: str) -> str:
    """取字段的文本值；None / 空白统一按缺失处理。"""
    return str(values.get(field) or "").strip()


def validate_rule(values: dict[str, Any]) -> dict[str, Any]:
    """执行规则测试，逐项给出结论。

    测试口径只认已经落库的字段：选择器、属性提取器、列表容错与空值占位符都要
    有明确配置，任意一项没确认就不能算通过。
    """
    checks: list[dict[str, Any]] = []
    failed: list[str] = []
    for field in VALIDATION_FIELDS:
        ok = bool(_text(values, field))
        if not ok:
            failed.append(field)
        checks.append({
            "field": field,
            "ok": ok,
            "message": "已验证" if ok else VALIDATION_MESSAGES[field],
        })
    return {
        "passed": not failed,
        "checked_at": _now(),
        "checks": checks,
        "failed_fields": failed,
    }


def validation_summary(entry: dict[str, Any]) -> str:
    """验证结论口径：给其它模块复用时保持一致。"""
    validation = entry.get("validation")
    if not isinstance(validation, dict):
        return "尚未验证"
    if validation.get("passed"):
        return "验证通过"
    failed = validation.get("failed_fields") or []
    return "验证不通过：" + "、".join(str(item) for item in failed)


def find_rule_by_number(number: str) -> dict[str, Any] | None:
    """按规则编号读取已落库的规则；包含已废弃规则，交给调用方判断可用性。"""
    number = str(number or "").strip()
    if not number:
        return None
    for row in store.rows(MODULE):
        if str(row.get("规则编号") or "").strip() == number:
            return row
    return None


def rule_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """规则身份快照：回执键和动作历史都用它，保证同一结论可被重放。

    包含当时的规则状态与验证结论：规则后来补齐字段、重新验证或废弃，身份随之
    变化，新请求不会被旧回执挡住。
    """
    return {
        "id": entry.get("id"),
        "规则编号": entry.get("规则编号"),
        "status": entry.get("status"),
        "内容": {
            field: entry.get(field)
            for field in VALIDATION_FIELDS + ["字符清洗正则", "输出字段映射"]
        },
        "验证结论": validation_summary(entry),
    }


def rule_can_enable(entry: dict[str, Any]) -> tuple[bool, str]:
    """判断规则能否被任务引用启动；返回 (是否可用, 不可用原因)。

    状态为已启用还不够：必须有一份通过的验证结论，否则视为没验证过。
    """
    status = entry.get("status")
    if status == STATUS_DEPRECATED:
        return False, f"规则 {entry.get('规则编号')} 已废弃，规则编号不能复活"
    if status != STATUS_ENABLED:
        return False, f"规则 {entry.get('规则编号')} 当前为「{status}」，未启用"
    validation = entry.get("validation")
    if not isinstance(validation, dict) or not validation.get("passed"):
        return False, f"规则 {entry.get('规则编号')} 没有通过的验证结论"
    return True, ""


def _available_actions(entry: dict[str, Any]) -> list[str]:
    """按当前状态与验证结论给出可执行动作，前后端用同一口径。"""
    status = entry.get("status")
    actions: list[str] = []
    if status == STATUS_DRAFT:
        actions = ["提交规则验证", "废弃规则"]
    elif status == STATUS_VALIDATING:
        actions = ["重新验证规则", "废弃规则"]
        if bool((entry.get("validation") or {}).get("passed")):
            actions.insert(1, "启用规则")
    elif status == STATUS_ENABLED:
        actions = ["废弃规则"]
    elif status == STATUS_DEPRECATED:
        actions = ["新建下一代规则"]
    return actions


def _present(entry: dict[str, Any]) -> dict[str, Any]:
    """输出副本：补上派生字段，避免把展示口径写回数据仓库。"""
    row = dict(entry)
    row["规则状态"] = entry.get("status")
    row["验证结论"] = validation_summary(entry)
    row["可执行动作"] = _available_actions(entry)
    row["available_actions"] = row["可执行动作"]
    row["validation_passed"] = bool((entry.get("validation") or {}).get("passed"))
    row["blocked_reason"] = (
        ""
        if entry.get("status") == STATUS_ENABLED
        else (
            f"规则 {entry.get('规则编号')} 已废弃，只能新建下一代规则"
            if entry.get("status") == STATUS_DEPRECATED
            else f"规则 {entry.get('规则编号')} 当前为「{entry.get('status')}」，不能用于启动任务"
        )
    )
    if entry.get("status") == STATUS_DEPRECATED:
        row["受影响原因"] = row["blocked_reason"]
    return row


def _find_by_number(number: str, *, exclude_id: int | None = None) -> dict[str, Any] | None:
    for row in store.rows(MODULE):
        if exclude_id is not None and int(row.get("id", 0)) == exclude_id:
            continue
        if str(row.get("规则编号") or "").strip() == number:
            return row
    return None


def _next_generation_number(base: str) -> str:
    """下一代编号在原编号后追加 V2、V3……；旧编号永不复用。"""
    generation = 2
    while _find_by_number(f"{base}-V{generation}") is not None:
        generation += 1
    return f"{base}-V{generation}"


class ParseruleService:
    def project_rules(self, numbers: list[str]) -> dict[str, dict[str, Any]]:
        """按规则编号读取当前落库状态，供跨模块准入判断使用。"""
        wanted = {str(number or "").strip() for number in numbers if str(number or "").strip()}
        found: dict[str, dict[str, Any]] = {}
        for row in store.rows(MODULE):
            number = str(row.get("规则编号") or "").strip()
            if number in wanted:
                found[number] = _present(row)
        return found

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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [_present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return _present(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记草稿规则；规则编号已在任何状态用过都不能复活。"""
        missing = [field for field in REQUIRED_FIELDS if not _text(values, field)]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        number = _text(values, "规则编号")
        existing = _find_by_number(number)
        if existing is not None:
            return None, (
                f"规则编号 {number} 已被规则 {existing.get('id')} 使用"
                f"（当前状态「{existing.get('status')}」），不能重复登记；"
                "已废弃规则请新建下一代规则"
            )
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 登记时把所有已提交字段都落库，而不是只留存必填的三项。
        for key, value in values.items():
            entry[str(key)] = value
        entry["规则编号"] = number
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["generation"] = 1
        entry.pop("validation", None)
        rows.append(entry)
        return _present(entry), ""

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改规则字段；已废弃规则不允许再改，改了字段后验证结论作废。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"解析规则 {entry_id} 不存在或已归档"
        if entry.get("status") == STATUS_DEPRECATED:
            return None, f"解析规则 {entry_id} 已废弃，不能修改，请新建下一代规则"
        number = _text(values, "规则编号")
        if number and number != str(entry.get("规则编号") or ""):
            existing = _find_by_number(number, exclude_id=entry_id)
            if existing is not None:
                return None, f"规则编号 {number} 已被规则 {existing.get('id')} 使用，不能改用它"
            entry["规则编号"] = number
        for key, value in values.items():
            if key in {"id", "status", "pending", "abnormal", "validation", "generation", "规则编号"}:
                continue
            entry[str(key)] = value
        entry["validation"] = validate_rule(entry)
        entry["abnormal"] = not entry["validation"]["passed"]
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
            return ActionOutcome(False, f"解析规则 {entry_id} 不存在或已归档")
        if action not in ACTION_RULES:
            return ActionOutcome(False, f"动作「{action}」不属于解析规则可执行范围")

        context = stable_token(rule_identity(entry))
        request_id = request_id_from(values)
        key = receipt_key(MODULE, entry_id, action, context, request_id)
        # 同一请求只认首个回执；后到请求直接返回已处理，不再重复生效。
        prior = replay(key)
        if prior is not None:
            prior.entry = _present(entry)
            return prior

        current = entry.get("status")
        target = ACTION_RULES[action]
        allowed = ALLOWED_FROM[action]

        if current == STATUS_DEPRECATED:
            outcome = ActionOutcome(
                False,
                f"解析规则 {entry.get('规则编号')} 已废弃，不能重新启用、废弃或重走验证，请新建下一代规则",
            )
            _remember(key, outcome, entry, action)
            outcome.entry = _present(entry)
            return outcome

        # 启用 / 废弃这类一次性动作：首个请求已落到目标状态，后到的重复请求
        # 只回“已处理”，不再重复生效。提交验证 / 重新验证不走这条捷径，
        # 补齐字段后需要重新执行测试。
        if action in {"启用规则", "废弃规则"} and current == target:
            outcome = ActionOutcome(True, DUPLICATE_MESSAGE, duplicate=True, detail="首个请求已生效")
            outcome.entry = _present(entry)
            return outcome

        if current not in allowed:
            reason = (
                f"规则当前为「{current}」，不能直接{action}；"
                + (
                    "请先提交规则验证并通过测试"
                    if target == STATUS_ENABLED
                    else "状态不允许该动作"
                )
            )
            _remember(key, outcome := ActionOutcome(False, reason), entry, action)
            outcome.entry = _present(entry)
            return outcome

        if action in {"提交规则验证", "重新验证规则"}:
            entry["status"] = STATUS_VALIDATING
            entry["validation"] = validate_rule(entry)
            entry["abnormal"] = not entry["validation"]["passed"]
            entry["pending"] = True
            entry["验证时间"] = entry["validation"]["checked_at"]
            outcome = ActionOutcome(
                True,
                f"解析规则已{action}；{validation_summary(entry)}",
                entry=_present(entry),
            )
            _remember(key, outcome, entry, action)
            return outcome

        if action == "启用规则":
            validation = entry.get("validation")
            if not isinstance(validation, dict) or not validation.get("passed"):
                failed = (validation or {}).get("failed_fields") or []
                reason = "规则验证未通过，不能启用"
                if failed:
                    reason += "；未确认项：" + "、".join(str(item) for item in failed)
                reason += "。请补齐字段后重新验证规则"
                outcome = ActionOutcome(False, reason)
                _remember(key, outcome, entry, action)
                outcome.entry = _present(entry)
                return outcome
            entry["status"] = STATUS_ENABLED
            entry["pending"] = False
            entry["abnormal"] = False
            entry["enabled_at"] = _now()
            outcome = ActionOutcome(True, "解析规则已启用规则", entry=_present(entry))
            _remember(key, outcome, entry, action)
            return outcome

        # 废弃规则：保留验证结论作为历史快照，规则本身不能再被任务引用。
        entry["status"] = STATUS_DEPRECATED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["deprecated_at"] = _now()
        entry["历史验证结论"] = validation_summary(entry)
        outcome = ActionOutcome(
            True,
            f"解析规则已废弃规则；规则编号 {entry.get('规则编号')} 不再可启用，请新建下一代规则",
            entry=_present(entry),
        )
        _remember(key, outcome, entry, action)
        return outcome

    def create_next_generation(
        self,
        entry_id: int,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """为已废弃规则新建下一代：复制字段，换新编号，重新走草稿到验证流程。"""
        values = values or {}
        source = store.find(MODULE, entry_id)
        if source is None:
            return None, f"解析规则 {entry_id} 不存在或已归档"
        if source.get("status") != STATUS_DEPRECATED:
            return None, f"解析规则 {entry_id} 尚未废弃，不能新建下一代规则"
        base = str(source.get("规则编号") or "").strip()
        number = _next_generation_number(base)
        inherited = {
            key: value
            for key, value in source.items()
            if key not in {"id", "status", "pending", "abnormal", "validation", "generation"}
            and key
            not in {
                "规则编号",
                "验证结论",
                "验证时间",
                "可执行动作",
                "available_actions",
                "历史验证结论",
                "动作历史",
                "enabled_at",
                "deprecated_at",
                "deprecated_reason",
                "superseded_by",
                "下一代规则编号",
                "受影响原因",
                "blocked_reason",
            }
        }
        inherited.update({key: value for key, value in values.items() if key != "规则编号"})
        inherited["规则编号"] = number
        inherited["上一代规则编号"] = base
        missing = [field for field in REQUIRED_FIELDS if not _text(inherited, field)]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update(inherited)
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["generation"] = int(source.get("generation") or 1) + 1
        rows.append(entry)
        source["superseded_by"] = number
        source["下一代规则编号"] = number
        return _present(entry), ""


def _remember(key: str, outcome: ActionOutcome, entry: dict[str, Any], action: str) -> None:
    """落回执，同时把规则侧的动作结论记入历史（只追加，不改写旧记录）。"""
    remember(key, outcome)
    entry.setdefault("动作历史", []).append({
        "time": _now(),
        "action": action,
        "ok": outcome.ok,
        "message": outcome.message,
        "规则状态": entry.get("status"),
        "验证结论": validation_summary(entry),
    })
