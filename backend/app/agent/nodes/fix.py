"""fix 节点：携带原始报错修正 SQL。

这是整条链路可靠性的关键。数据库返回的报错（字段不存在、语法错误、聚合不合法）
信息量远大于让模型自查，把它原样回传做定向修正，比重新生成一次的成功率高得多。

历史修正记录一并注入 Prompt，避免模型在同一个错误上反复兜圈。

retry_count 在这里递增，语义是「已经修正了几次」，路由据此判断是否还有配额。
成功与失败两条路径都要加：只加成功路径的话，模型持续报错时计数不涨，
链路会在 fix 与 validate 之间空转。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.prompts import render_prompt
from app.agent.state import AgentState, new_stage
from app.llm.gateway import Gateway, LLMError
from app.llm.schemas import SqlDraft

logger = logging.getLogger(__name__)

NODE = "fix"


def fix_node(state: AgentState, *, gateway: Gateway) -> dict[str, Any]:
    started = time.monotonic()
    retrieved = state.get("retrieved") or {}
    sql = state.get("sql") or ""
    error = state.get("sql_error") or "（未提供报错信息）"
    # retry_count 计的是已经做过的修正次数，本次是第 attempt 次
    attempt = int(state.get("retry_count") or 0) + 1

    history_text = _render_fix_history(state.get("fix_history") or [])

    prompt = render_prompt(
        "fix",
        fix_history=history_text or "（这是第一次修正）",
        question=state.get("question") or "",
        context=retrieved.get("context") or "（无）",
        sql=sql,
        sql_error=error,
    )

    try:
        draft = gateway.complete_json(prompt, SqlDraft, tag=NODE)
    except LLMError as exc:
        logger.warning("第 %d 次修正失败：%s", attempt, exc)
        return {
            "sql_error": f"修正失败：{exc}",
            # 修正自身失败也要计入配额，否则连续失败时链路不会收敛到 give_up
            "retry_count": attempt,
            "stages": [
                new_stage(NODE, "修正查询", f"第 {attempt} 次修正未成功", _ms(started))
            ],
        }

    logger.info("第 %d 次修正：%s", attempt, draft.reasoning[:80])
    return {
        "sql": draft.sql,
        "sql_error": None,
        "retry_count": attempt,
        # 状态里已有原语句，修正后不再覆盖 original_sql，保留首次生成的版本用于回溯
        "stages": [
            new_stage(
                NODE,
                "修正查询",
                f"第 {attempt} 次修正：{draft.reasoning or '已重新生成语句'}",
                _ms(started),
            )
        ],
    }


def _render_fix_history(items: list[dict[str, str]]) -> str:
    if not items:
        return ""
    lines = []
    for i, item in enumerate(items, 1):
        stage = item.get("stage", "unknown")
        lines.append(f"{i}. 阶段 {stage} 报错：{item.get('error', '')[:300]}")
    return "\n".join(lines)


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
