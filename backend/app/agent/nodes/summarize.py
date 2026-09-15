"""summarize 节点：结果集 → 经营结论。

结论只依据本轮结果集，口径边界从语义层召回结果里挑选，不允许模型自行补充。
最后把结论、口径、数据行数组装成 answer，作为一轮回答的正文。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.prompts import format_rows, render_prompt
from app.agent.state import AgentState, new_stage
from app.llm.gateway import Gateway, LLMError
from app.llm.schemas import Insight
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

NODE = "summarize"
PREVIEW_N = 20


def summarize_node(state: AgentState, *, gateway: Gateway) -> dict[str, Any]:
    started = time.monotonic()
    columns = list(state.get("columns") or [])
    rows = list(state.get("rows") or [])
    retrieved = state.get("retrieved") or {}
    question = state.get("question") or ""

    caveats = retrieved.get("caveats") or []
    time_range = (retrieved.get("time_range") or {}).get("resolved") or "未限定，覆盖库内全部数据"

    if not rows:
        # 空结果不是错误，但必须给出可读解释，否则用户会以为系统坏了
        answer = (
            "查询执行成功，但没有返回数据。可能的原因：筛选条件过严、"
            "所选时间区间内确实没有成交记录，或维度取值与库内实际值不一致。"
            "可以放宽时间范围或换一个维度再试。"
        )
        return {
            "insight": answer,
            "highlights": [],
            "answer": answer,
            "status": "ok",
            "messages": [AIMessage(content=answer)],
            "stages": [new_stage(NODE, "分析结果", "结果集为空", _ms(started))],
        }

    prompt = render_prompt(
        "summarize",
        caveats="\n".join(f"- {c}" for c in caveats) or "（本轮未命中已登记指标口径）",
        time_range=time_range,
        question=question,
        columns="、".join(columns),
        preview_n=PREVIEW_N,
        row_count=len(rows),
        rows_preview=format_rows(rows, columns, PREVIEW_N),
        sql=state.get("sql") or "",
    )

    insight: Insight | None = None
    try:
        insight = gateway.complete_json(prompt, Insight, tag=NODE)
    except LLMError as exc:
        logger.warning("结论生成失败：%s", exc)

    if insight is None:
        # 结论生成失败时退回可读的事实陈述，数据与图表照常呈现
        answer = f"查询返回 {len(rows)} 行结果，具体见上方图表与结果表。"
        return {
            "insight": answer,
            "highlights": [],
            "answer": answer,
            "status": "ok",
            "messages": [AIMessage(content=answer)],
            "stages": [new_stage(NODE, "分析结果", "结论生成失败，已呈现原始结果", _ms(started))],
        }

    parts = [insight.conclusion.strip()]
    if insight.highlights:
        parts.append("\n".join(f"- {h}" for h in insight.highlights))
    answer = "\n\n".join(p for p in parts if p)

    used_caveats = insight.caveats or caveats

    return {
        "insight": insight.conclusion.strip(),
        "highlights": insight.highlights,
        "answer": answer,
        "metric_used": list(state.get("metric_used") or []),
        "status": "ok",
        "messages": [AIMessage(content=answer)],
        "stages": [
            new_stage(
                NODE,
                "分析结果",
                f"已生成 {len(rows)} 行数据的解读"
                + (f"，口径说明 {len(used_caveats)} 条" if used_caveats else ""),
                _ms(started),
            )
        ],
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
