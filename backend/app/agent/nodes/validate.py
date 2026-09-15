"""validate 节点：调用 SQL 守卫做安全校验。

守卫的九条规则集中在 db/guard.py，本节点只做两件事：把结果翻译成状态更新、
在校验失败时把报错记进修正历史。把校验逻辑留在守卫里，是为了让它能脱离编排层单
独单测。

重试计数由 fix 节点递增：retry_count 的语义是「已经修正了几次」，计在产生修正的
那一侧才不会出现「配置写 3、提示语写 4」这类差一错误。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.state import AgentState, new_stage
from app.db.guard import SqlGuard

logger = logging.getLogger(__name__)

NODE = "validate"


def validate_node(state: AgentState, *, guard: SqlGuard) -> dict[str, Any]:
    started = time.monotonic()
    sql = state.get("sql") or ""

    if not sql.strip():
        return _fail(state, "没有可校验的语句", started, "生成结果为空")

    result = guard.check(sql)

    if not result.ok:
        logger.info("守卫拦截：%s", result.error_text())
        return _fail(state, result.error_text(), started, "安全校验未通过")

    warnings = list(result.warnings)
    detail = f"通过校验，涉及 {len(result.tables)} 张表"
    if result.rewritten:
        # 改写是守卫主动加的包裹，必须让用户看得见，否则「实际执行的 SQL」
        # 与模型输出的 SQL 会对不上
        detail += "，已补充行数上限"

    return {
        "sql": result.sql,
        "original_sql": result.original_sql,
        "sql_error": None,
        "guard_warnings": warnings,
        "stages": [new_stage(NODE, "安全校验", detail, _ms(started))],
    }


def _fail(state: AgentState, error: str, started: float, label: str) -> dict[str, Any]:
    history = list(state.get("fix_history") or [])
    history.append({"stage": "validate", "sql": (state.get("sql") or "")[:2000], "error": error})
    return {
        "sql_error": error,
        "fix_history": history,
        "stages": [new_stage(NODE, "安全校验", f"{label}：{error[:80]}", _ms(started))],
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
