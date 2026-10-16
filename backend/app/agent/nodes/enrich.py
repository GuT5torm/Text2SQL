"""FR-M-1 / FR-M-2 / FR-M-3 / FR-L-1 enrich 节点：计算对比基准与异常标记。

位置：execute → visualize → enrich → summarize。

为什么放在 execute 之后而不是之后才补：FRD 设计说明明确要求「实现位置在
summarize 之前」。基准值是结论生成的输入项，不是补充注释。让模型先写
出「较上月下降 8%」再补基准会退化为编造，因为模型写下的比较句与后算的
基准可能对不上。

为什么不拆成 baseline_node + finding_node 两个节点：两者都依赖本轮结果集
+ time_range，异常判定还要读基准算出的偏离，拆开需要在两个节点间传递中间
结果。enrich 单节点双职责：基准是衍生字段、finding 是衍生判定。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from app.agent.baselines import compute_baselines
from app.agent.consistency import check_time_range_consistency
from app.agent.findings import detect_findings
from app.agent.state import AgentState, new_stage
from app.db.repository import QueryRepository
from app.semantic.loader import Catalog

logger = logging.getLogger(__name__)

NODE = "enrich"


def enrich_node(
    state: AgentState,
    *,
    repo: QueryRepository,
    catalog: Catalog,
    # 时间词锚点：库内数据截止日。基准的「本期是否未走完」按它判定，而不是
    # 按系统日期——数据截止 2026-09-22 时问「本月」，9 月对系统日期（10 月）
    # 早就走完了，但对数据而言只到 22 日，后半句才是用户看到的口径。
    today: dt.date | None = None,
) -> dict[str, Any]:
    """读 execute 节点的产出，计算本轮的对比基准与异常标记并写入 state。

    失败时不抛错：基准与标记都是辅助，前端拿不到时按缺失渲染即可，不影响主链路。
    """
    started = time.monotonic()
    sql = state.get("sql") or ""
    rows = list(state.get("rows") or [])
    metric_used = list(state.get("metric_used") or [])
    retrieved = state.get("retrieved") or {}
    time_range = retrieved.get("time_range")
    data_freshness = state.get("data_freshness")

    # FR-D-5：纯函数核对，只依赖实际 SQL 与召回区间，不随基准计算成败而跳过。
    try:
        time_range_consistent: bool | None = check_time_range_consistency(sql, time_range)
    except Exception:  # noqa: BLE001 - 核对器自身异常时按「未核对」处理
        logger.warning("时间区间一致性核对异常", exc_info=True)
        time_range_consistent = None

    try:
        result = compute_baselines(
            sql=sql,
            rows=rows,
            metric_used=metric_used,
            retrieved_time_range=time_range,
            data_freshness=data_freshness,
            catalog=catalog,
            repo=repo,
            today=today,
        )
    except Exception as exc:  # noqa: BLE001 - 兜住意外路径，不影响主链路
        logger.warning("基准计算失败：%s", exc)
        return {
            "baselines": None,
            "partial_period": False,
            "granularity": None,
            "findings": [],
            "time_range_consistent": time_range_consistent,
            "stages": [new_stage(NODE, "对比基准", "计算异常，未附带", _ms(started))],
        }

    partial = bool(result.get("partial_period"))
    granularity = result.get("granularity")

    # FR-L-1：异常判定读基准算出的偏离，因此必须在基准之后。metric_used 里
    # 放的是显示名，规则表按指标名索引，由 catalog 做一次转换。
    metric = result.get("metric") or (metric_used[0] if metric_used else None)
    try:
        findings = detect_findings(
            rows=rows,
            columns=list(state.get("columns") or []),
            metric=metric,
            baselines=result,
            rules=catalog.finding_rule(metric),
        )
    except Exception as exc:  # noqa: BLE001 - 判定失败只影响标记，不影响主链路
        logger.warning("异常判定失败：%s", exc)
        findings = []

    detail_parts: list[str] = []
    if result.get("metric"):
        detail_parts.append(f"主指标：{result['metric']}")
    if granularity:
        detail_parts.append(f"粒度：{granularity}")
    if partial:
        detail_parts.append("本期未完整")
    if result.get("mom"):
        detail_parts.append("已算环比")
    if result.get("yoy"):
        detail_parts.append("已算同比")
    if findings:
        detail_parts.append(f"异常 {len(findings)} 项")
    if result.get("missing_reasons"):
        detail_parts.append(f"缺失 {len(result['missing_reasons'])} 项")
    detail = "；".join(detail_parts) or "无基准数据"

    return {
        "baselines": result,
        "partial_period": partial,
        "granularity": granularity,
        "findings": findings,
        "time_range_consistent": time_range_consistent,
        "stages": [new_stage(NODE, "对比基准", detail, _ms(started))],
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


__all__ = ["enrich_node"]