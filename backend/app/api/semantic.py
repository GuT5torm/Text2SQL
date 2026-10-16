"""语义层接口：把指标口径与表结构暴露给前端与教学环节。

本系统的一条设计立场是「任何数字都要能追溯到具体口径」。前端把本轮命中的口径
标注出来，靠的就是这里的数据；实训环节也用它直观展示「改了 YAML 之后召回结果
怎么变」，比读代码快得多。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import Container, get_container
from app.semantic.loader import describe_catalog, recall, render_context

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/semantic", tags=["semantic"])


@router.get("")
def overview(container: Container = Depends(get_container)) -> dict[str, Any]:
    """概览：指标清单、表清单、维度清单与数量统计。"""
    return describe_catalog(container.catalog)


@router.get("/metrics")
def metrics(container: Container = Depends(get_container)) -> dict[str, Any]:
    """指标口径全文，含口径表达式与边界说明。"""
    return {"items": container.catalog.metrics}


@router.get("/tables")
def tables(container: Container = Depends(get_container)) -> dict[str, Any]:
    """表结构与字段元数据全文。"""
    return {"items": container.catalog.tables, "dimensions": container.catalog.dimensions}


@router.get("/synonyms")
def synonyms(container: Container = Depends(get_container)) -> dict[str, Any]:
    return {
        "synonyms": container.catalog.synonyms,
        "value_synonyms": container.catalog.value_synonyms,
        "time_terms": container.catalog.time_terms,
    }


@router.get("/recall")
def debug_recall(
    q: str = Query(min_length=1, description="待召回的自然语言问题"),
    with_context: bool = Query(default=False, description="是否一并返回渲染后的 prompt 段落"),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    """调试接口：不调用模型，直接看到规则召回命中了什么。

    时间词按库内数据截止日换算，与 retrieve 节点同一锚点——调试接口按系统
    日期换算的话，这里看到的区间会和问答里实际用的区间对不上。
    """
    hit = recall(q, container.catalog, container.repo.data_as_of())
    payload: dict[str, Any] = {
        "question": q,
        "metrics": [m["name"] for m in hit.metrics],
        "metric_displays": hit.metric_displays(),
        "tables": [t["name"] for t in hit.tables],
        "dimensions": [d["name"] for d in hit.dimensions],
        "matched_terms": hit.matched_terms,
        "value_hints": hit.value_hints,
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
    }
    if with_context:
        payload["context"] = render_context(hit)
    return payload


@router.get("/health")
def semantic_health(container: Container = Depends(get_container)) -> dict[str, Any]:
    """核对语义层登记的表与业务库实际存在的表是否一致。

    语义层漏登记一张表，生成的 SQL 就会被守卫当成越权拦下；注册了不存在的表，
    召回会把模型引向不存在的对象。这个接口把两种偏差都暴露出来。
    """
    declared = {t["name"] for t in container.catalog.tables}
    actual = set(container.repo.list_tables())
    missing_in_semantic = sorted(actual - declared)
    missing_in_db = sorted(declared - actual)
    row_counts = {t: container.repo.count_rows(t) for t in sorted(declared & actual)}
    return {
        "consistent": not missing_in_semantic and not missing_in_db,
        "declared_count": len(declared),
        "actual_count": len(actual),
        "missing_in_semantic": missing_in_semantic,
        "missing_in_db": missing_in_db,
        "row_counts": row_counts,
    }
