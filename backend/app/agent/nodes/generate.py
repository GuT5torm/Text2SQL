"""generate 节点：生成 SQL。

输入是问题、语义层上下文与最近对话，输出一条 SQL 文本。本节点不做校验，
也不碰数据库——校验与执行各有专职节点，出错时由 fix 节点带着原始报错回来重写。

FR-J-4：一次生成两条候选，按守卫通过情况、字段命中率与执行代价择优。
桩模式下两候选相同，但择优函数本身可单测。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from app.agent.candidate_rerank import rerank_candidates
from app.agent.form_query import render_condition_block
from app.agent.prompts import format_history, render_prompt
from app.agent.state import AgentState, new_stage
from app.db.guard import SqlGuard
from app.llm.gateway import Gateway, LLMError
from app.llm.schemas import SqlDraft
from app.semantic.loader import Catalog

logger = logging.getLogger(__name__)

NODE = "generate"

# FR-J-4：是否启用多候选择优。标为可选机制，桩模式下两候选相同。
# 设为 True 让机制生效；切到真实模型后会真正生成两条不同候选。
ENABLE_RERANK = True


def generate_node(
    state: AgentState,
    *,
    gateway: Gateway,
    history_window: int = 6,
    guard: SqlGuard | None = None,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    retrieved = state.get("retrieved") or {}
    context = retrieved.get("context") or "（无可用语义层信息，请按问题意图谨慎生成）"
    question = state.get("question") or ""

    # 表单路径才渲染条件清单：自由提问的条件由模型自己抽，再给一份谓词清单
    # 等于让它对同一件事看两遍，反而容易改坏。空串时模板整节不输出。
    conditions_block = (
        render_condition_block(
            state.get("form_conditions") or [], catalog, state.get("form_query")
        )
        if state.get("query_source") == "form"
        else ""
    )

    prompt = render_prompt(
        "generate",
        today=dt.date.today().isoformat(),
        dialect=getattr(gateway.settings, "sql_dialect", "sqlite"),
        context=context,
        conditions=conditions_block,
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

    # FR-J-4：多候选择优。guard 与 catalog 可用时生成第二条候选并择优。
    # 桩模式下第二条候选与第一条相同（_mock_json 对同一 prompt 返回同一结果），
    # 但 rerank_candidates 函数本身可单测。
    if ENABLE_RERANK and guard is not None and catalog is not None:
        try:
            second = gateway.complete_json(prompt, SqlDraft, tag=NODE)
            candidates = [draft, second]
            draft, scores = rerank_candidates(candidates, guard, catalog)
            logger.info(
                "候选择优：%d 条候选，选中总分 %.1f",
                len(candidates),
                scores[0].total,
            )
        except Exception as exc:  # noqa: BLE001 - 择优失败不影响主链路
            logger.warning("候选择优失败，使用首条候选：%s", exc)

    # 模型有时会漏填 metric_used，用召回结果补上，保证前端口径标注不空
    metric_used = draft.metric_used or list(retrieved.get("metric_displays") or [])

    logger.info("生成 SQL（%d 字符）：%s", len(draft.sql), draft.sql.splitlines()[0][:80])
    return {
        "sql": draft.sql,
        "metric_used": metric_used,
        "sql_error": None,
        # FR-C-1：把模型的写法说明落到 SqlPanel 的折叠项。
        # reasoning 已在 stage detail 里用过一次，再独立出来供「写法说明」折叠项用。
        "sql_reasoning": draft.reasoning or "",
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
