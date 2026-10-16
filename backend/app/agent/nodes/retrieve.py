"""retrieve 节点：语义层召回。

调用语义层把问题映射到相关的指标口径、表结构、维度与示例，并渲染成
prompt 段落放进状态。本节点不调用大模型，因此召回结果完全由配置决定，
出现问题时改 YAML 即可，不需要调 Prompt。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from app.agent.state import AgentState, new_stage
from app.semantic.expression_check import signatures_for
from app.semantic.loader import Catalog, recall, render_context

logger = logging.getLogger(__name__)

NODE = "retrieve"


def retrieve_node(
    state: AgentState,
    *,
    catalog: Catalog,
    # 时间词锚点，取自 QueryRepository.data_as_of（库内数据截止日）。
    # 不传则按系统日期换算，数据陈旧时会落到没有数据的月份。
    today: dt.date | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    question = state.get("question") or ""

    hit = recall(question, catalog, today)
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
    if hit.unsupported_dimensions:
        # FR-D-4 补充：时间线里留痕，便于回看时判断这一轮的口径风险
        detail += "；未收录维度 " + "、".join(hit.unsupported_dimensions)

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
            # 口径签名：validate 节点拿它比对生成结果的聚合表达式。放在
            # retrieved 里而不是让 validate 自己查 Catalog，是为了把「本轮
            # 哪些指标需要校验」这个决定留在召回环节。
            "metric_signatures": signatures_for(hit.metrics),
            # FR-D-4 补充：有分组诉求但本库无对应物的维度。retrieved 里单独留一份，
            # graph 据此在拿到真实 SQL 后补出「实际按哪个字段分组」的提示。
            "unsupported_dimensions": list(hit.unsupported_dimensions),
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
