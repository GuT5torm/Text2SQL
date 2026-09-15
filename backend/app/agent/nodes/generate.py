"""generate 节点：生成 SQL。

输入是问题、语义层上下文与最近对话，输出一条 SQL 文本。本节点不做校验，
也不碰数据库——校验与执行各有专职节点，出错时由 fix 节点带着原始报错回来重写。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from app.agent.prompts import format_history, render_prompt
from app.agent.state import AgentState, new_stage
from app.llm.gateway import Gateway, LLMError
from app.llm.schemas import SqlDraft

logger = logging.getLogger(__name__)

NODE = "generate"


def generate_node(
    state: AgentState, *, gateway: Gateway, history_window: int = 6
) -> dict[str, Any]:
    started = time.monotonic()
    retrieved = state.get("retrieved") or {}
    context = retrieved.get("context") or "（无可用语义层信息，请按问题意图谨慎生成）"
    question = state.get("question") or ""

    prompt = render_prompt(
        "generate",
        today=dt.date.today().isoformat(),
        context=context,
        history=format_history(state.get("messages") or [], history_window) or "（无）",
        question=question,
    )

    try:
        draft = gateway.complete_json(prompt, SqlDraft, tag=NODE)
    except LLMError as exc:
        logger.warning("SQL 生成失败：%s", exc)
        return {
            "sql_error": f"SQL 生成失败：{exc}",
            "status": "error",
            "error": f"未能生成查询语句。{exc}",
            "stages": [new_stage(NODE, "生成查询", "生成失败", _ms(started))],
        }

    # 模型有时会漏填 metric_used，用召回结果补上，保证前端口径标注不空
    metric_used = draft.metric_used or list(retrieved.get("metric_displays") or [])

    logger.info("生成 SQL（%d 字符）：%s", len(draft.sql), draft.sql.splitlines()[0][:80])
    return {
        "sql": draft.sql,
        "metric_used": metric_used,
        "sql_error": None,
        "stages": [
            new_stage(
                NODE,
                "生成查询",
                draft.reasoning or f"已生成 {len(draft.sql)} 字符的查询语句",
                _ms(started),
            )
        ],
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
