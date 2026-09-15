"""retrieve 节点：语义层召回。

调用语义层把问题映射到相关的指标口径、表结构、维度与示例，并渲染成
prompt 段落放进状态。本节点不调用大模型，因此召回结果完全由配置决定，
出现问题时改 YAML 即可，不需要调 Prompt。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.state import AgentState, new_stage
from app.semantic.loader import Catalog, recall, render_context

logger = logging.getLogger(__name__)

NODE = "retrieve"


def retrieve_node(state: AgentState, *, catalog: Catalog) -> dict[str, Any]:
    started = time.monotonic()
    question = state.get("question") or ""

    hit = recall(question, catalog)
    context = render_context(hit)

    tables = [t["name"] for t in hit.tables]
    metrics = hit.metric_displays()

    detail = (
        f"命中 {len(metrics)} 个指标口径、{len(tables)} 张表"
        if not hit.is_empty
        else "未命中已登记的口径与表，将按通用规则生成"
    )
    if hit.time_range:
        detail += f"；时间范围 {hit.time_range.label} → {hit.time_range.resolved}"

    logger.info(
        "语义召回：metrics=%s tables=%s terms=%s",
        metrics,
        tables,
        hit.matched_terms,
    )

    return {
        "retrieved": {
            "context": context,
            "metric_displays": metrics,
            "caveats": hit.caveats(),
            "tables": tables,
            "matched_terms": hit.matched_terms,
            "time_range": (
                {
                    "label": hit.time_range.label,
                    "start": hit.time_range.start,
                    "end": hit.time_range.end,
                    "resolved": hit.time_range.resolved,
                }
                if hit.time_range
                else None
            ),
        },
        "stages": [
            new_stage(
                NODE, "检索口径", detail, (time.monotonic() - started) * 1000
            )
        ],
    }
