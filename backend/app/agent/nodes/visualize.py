"""visualize 节点：结果集 → 图表配置。

模型只负责选图型与字段映射，ECharts option 由 chart/builder.py 构造。
模型给的字段一旦对不上结果集的列，builder 会按结果集结构重新决策，
因此本节点在模型出错时仍能产出可渲染的图表。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.prompts import format_rows, render_prompt
from app.agent.state import AgentState, new_stage
from app.agent.nodes._freshness import compute_data_freshness
from app.chart.builder import build_chart
from app.db.repository import QueryRepository
from app.llm.gateway import Gateway, LLMError
from app.llm.schemas import ChartSpec

logger = logging.getLogger(__name__)

NODE = "visualize"
PREVIEW_N = 20


def visualize_node(
    state: AgentState, *, gateway: Gateway, repo: QueryRepository
) -> dict[str, Any]:
    started = time.monotonic()
    columns = list(state.get("columns") or [])
    rows = list(state.get("rows") or [])
    question = state.get("question") or ""

    # FR-D-2 数据时效：走到这一步说明 SQL 已执行过，至少有一张表被访问过。
    # 失败路径（execute 报错进入 fix / give_up）也会进入可视化的极少见，
    # 此时同样计算时效，让回看时能看到当时的数据快照日期。
    try:
        data_freshness = compute_data_freshness(repo)
    except Exception as exc:  # noqa: BLE001 - 时效计算失败不应阻塞可视化和结论
        logger.warning("数据时效计算失败：%s", exc)
        data_freshness = None

    if not rows:
        payload = build_chart(columns, rows, None, question)
        return {
            "chart": _to_dict(payload),
            "data_freshness": data_freshness,
            "stages": [
                new_stage(NODE, "绘制图表", "结果集为空，退回表格", _ms(started))
            ],
        }

    prompt = render_prompt(
        "visualize",
        question=question,
        columns="、".join(columns),
        preview_n=PREVIEW_N,
        row_count=len(rows),
        rows_preview=format_rows(rows, columns, PREVIEW_N),
    )

    spec: ChartSpec | None = None
    try:
        spec = gateway.complete_json(prompt, ChartSpec, tag=NODE)
    except LLMError as exc:
        # 图表决策失败不影响数据本身，按规则兜底即可
        logger.warning("图表决策失败，改用规则兜底：%s", exc)

    payload = build_chart(columns, rows, spec, question)

    detail = f"选择 {payload.chart_type} 图：{payload.reason}"
    return {
        "chart": _to_dict(payload),
        "data_freshness": data_freshness,
        "stages": [new_stage(NODE, "绘制图表", detail, _ms(started))],
    }


def _to_dict(payload: Any) -> dict[str, Any]:
    return {
        "chart_type": payload.chart_type,
        "option": payload.option,
        "reason": payload.reason,
        "warnings": list(payload.warnings),
        "table_columns": payload.table_columns,
        "table_rows": payload.table_rows,
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
