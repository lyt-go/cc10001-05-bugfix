"""动作回执：同一条规则 / 任务的同一个请求只认首个成功回执。

后到的重复请求命中回执时统一返回“已处理”，并且只接受首个成功结果，不会重复
生效；失败请求不写入成功回执，下一次提交会按当时的规则状态重新评估，不会因为
一次失败就永久挡住，也不会把失败刷成成功。回执键里固定带上规则身份（编号、
字段内容、验证结论），不包含两边的生命周期状态，避免首个请求自己改了状态之后
后到请求就绕开回执。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.store import store

DUPLICATE_MESSAGE = "已处理"


def stable_token(payload: Any) -> str:
    """把业务上下文压成稳定短串，用作回执键的一部分。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


@dataclass
class ActionOutcome:
    """单次动作的处理结果。duplicate 为真时表示命中了已有成功回执。"""

    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    duplicate: bool = False
    detail: str | None = None


def request_id_from(values: dict[str, Any]) -> str:
    """从请求体里取幂等键；兼容几种常见写法，取到第一个非空值。"""
    for key in ("request_id", "requestId", "idempotency_key", "回执编号", "回执号"):
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def receipt_key(
    module: str,
    entry_id: int,
    action: str,
    context: str,
    request_id: str = "",
) -> str:
    """生成回执键：有回执编号按编号认，没有就按业务上下文认。"""
    if request_id:
        return f"{module}:{entry_id}:{action}:receipt:{request_id}"
    return f"{module}:{entry_id}:{action}:context:{context}"


def replay(key: str) -> ActionOutcome | None:
    """读取首个成功回执；没有则返回 None。"""
    record = store.receipts.get(key)
    if record is None or not record.get("ok"):
        return None
    return ActionOutcome(
        ok=True,
        message=DUPLICATE_MESSAGE,
        duplicate=True,
        detail=str(record.get("message") or ""),
    )


def remember(key: str, outcome: ActionOutcome) -> None:
    """只落成功回执：首个成功结果之后同键请求都按“已处理”返回。"""
    if not outcome.ok:
        return
    store.receipts[key] = {
        "ok": True,
        "message": outcome.message,
        "entry": copy.deepcopy(outcome.entry),
    }
