"""FR-L-1 异常判定：把结果集与对比基准折算成结构化的 findings。

判定为什么不能交给模型：

1. 桩模式下模型不产出真实结论，规则判定才能让本需求在配置密钥之前验收；
2. 「什么算异常」是业务定义，写进配置才可复现。让模型自由发挥会出现同一份
   数据这次算异常、下次不算；
3. 标记要携带 baseline 与 deviation，这两个值是算出来的，不是生成的。

三条规则并行，任一命中即为异常：

    deviation  与对比基准的偏离超过阈值。环比优先，环比不可比时退回同比。
    rank       在同维度内排名处于前 / 后若干比例。基线取同维度的中位数。
    absolute   绝对值越过配置阈值，比率型指标用得多（退款率 > 10%）。

不完整周期不参与判定：FR-M-2 已把 mom 的 comparable 置为 false，本模块只
读这个标记，不再自己判断周期，避免两处各判一次判出不同结论。
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

logger = logging.getLogger(__name__)

# 规则表缺省时的内建默认值。配置里写了就按配置走，没写也不至于整条规则失效。
DEFAULT_DEVIATION_THRESHOLD = 0.20
DEFAULT_RANK_RATIO = 0.20
DEFAULT_MIN_SLICES = 4
DEFAULT_SEVERITY_RATIO = 2.0
DEFAULT_SEVERITY = "提示"

# 严重度序，用于去重时保留更强的一条
SEVERITY_ORDER = {"提示": 0, "关注": 1}

# 严重度相同时的规则优先序。业务给定的绝对阈值优先于统计排名——「退款率
# 超过 10%」是写进口径的判定，「比中位高 60%」只是相对位置，两者同时成立时
# 前者更能说明问题。
RULE_PRIORITY = {"absolute": 2, "deviation": 1, "rank": 0}

# 单轮最多返回几条。异常标记的作用是引导下钻，不是把结果集复述一遍；
# 超过这个数量说明阈值配得太松，应当调配置而不是多看几条。
MAX_FINDINGS = 5

Finding = dict[str, Any]


def detect_findings(
    *,
    rows: list[dict[str, Any]],
    columns: list[str],
    metric: str | None,
    baselines: dict[str, Any] | None,
    rules: dict[str, Any] | None,
) -> list[Finding]:
    """按规则表判定异常，返回 findings 列表（可能为空）。

    rows / columns 是本轮执行结果，metric 是主指标显示名，baselines 是 enrich
    节点算出的对比基准。三者任一缺失时对应规则自动跳过，不抛异常——异常判定
    是增值信息，不能因为它失败让整轮问答失败。
    """
    rules = rules or {}
    deviation_threshold = _float(rules.get("deviation_threshold"), DEFAULT_DEVIATION_THRESHOLD)
    rank_ratio = _float(rules.get("rank_ratio"), DEFAULT_RANK_RATIO)
    min_slices = _int(rules.get("min_slices"), DEFAULT_MIN_SLICES)
    severity_ratio = _float(rules.get("severity_ratio"), DEFAULT_SEVERITY_RATIO)
    base_severity = rules.get("severity") or DEFAULT_SEVERITY

    found: list[Finding] = []
    found += _rule_deviation(metric, baselines, deviation_threshold, base_severity)
    found += _rule_rank(
        rows, columns, metric, rank_ratio, min_slices, severity_ratio,
        deviation_threshold, base_severity,
    )
    found += _rule_absolute(rows, columns, metric, rules.get("absolute"))

    return _dedupe(found)


# ---------------------------------------------------------------------------
# 规则一：与对比基准的偏离
# ---------------------------------------------------------------------------

def _rule_deviation(
    metric: str | None,
    baselines: dict[str, Any] | None,
    threshold: float,
    base_severity: str,
) -> list[Finding]:
    if not baselines or not metric:
        return []
    current = baselines.get("current_value")
    if not isinstance(current, (int, float)):
        return []

    # 环比优先；环比不可比（不完整周期或缺失）时退回同比。
    # 两条都给会让同一个异常出现两次，结论里读起来像两个问题。
    for key in ("mom", "yoy"):
        item = baselines.get(key)
        if not isinstance(item, dict):
            continue
        if item.get("comparable") is False:
            continue
        deviation = item.get("deviation")
        baseline_value = item.get("value")
        if not isinstance(deviation, (int, float)):
            continue
        if abs(deviation) < threshold:
            continue
        severity = "关注" if abs(deviation) >= threshold * 2 else base_severity
        direction = "高于" if deviation > 0 else "低于"
        return [
            {
                "metric": metric,
                "dimension": None,
                "slice": None,
                "value": current,
                "baseline": baseline_value,
                "deviation": round(float(deviation), 4),
                "severity": severity,
                "rule": "deviation",
                "reason": (
                    f"整体{metric}{direction}{item.get('label') or key}"
                    f"{abs(deviation):.1%}，超过阈值 {threshold:.0%}"
                ),
            }
        ]
    return []


# ---------------------------------------------------------------------------
# 规则二：同维度内排名处于两端
# ---------------------------------------------------------------------------

def _rule_rank(
    rows: list[dict[str, Any]],
    columns: list[str],
    metric: str | None,
    rank_ratio: float,
    min_slices: int,
    severity_ratio: float,
    deviation_threshold: float,
    base_severity: str,
) -> list[Finding]:
    if not rows or not metric:
        return []
    value_column = _pick_value_column(columns, rows, metric)
    if value_column is None:
        return []

    values = [r.get(value_column) for r in rows]
    numeric = [(i, float(v)) for i, v in enumerate(values) if isinstance(v, (int, float))]
    if len(numeric) < min_slices:
        # 切片太少时第一名没有统计意义，三个大区里最高的那个不叫异常
        return []

    dimension_column = _pick_dimension_column(columns, rows, value_column)
    ordered = sorted(numeric, key=lambda x: x[1])
    median = statistics.median(v for _, v in ordered)
    if not median:
        return []

    count = max(1, int(len(ordered) * rank_ratio))
    rank_of = {index: rank for rank, (index, _) in enumerate(ordered, start=1)}
    out: list[Finding] = []
    for index in [i for i, _ in ordered[:count]] + [i for i, _ in ordered[-count:]]:
        value = float(values[index])
        deviation = (value - median) / median
        if abs(deviation) < deviation_threshold:
            continue
        rank = rank_of[index]
        high = rank > len(ordered) - count
        # 展示用名次从高往低数：值最大的说「第 1 高」，而不是「第 7 名」
        rank_desc = len(ordered) - rank + 1
        severity = (
            "关注"
            if abs(deviation) >= deviation_threshold * severity_ratio
            else base_severity
        )
        out.append(
            {
                "metric": metric,
                "dimension": dimension_column,
                "slice": _slice_value(rows[index], dimension_column),
                "value": round(value, 4),
                "baseline": round(median, 4),
                "deviation": round(deviation, 4),
                "severity": severity,
                "rule": "rank",
                "reason": (
                    f"在 {len(ordered)} 个{dimension_column or '切片'}中居"
                    f"{'前' if high else '后'}{count} 位"
                    f"（第 {rank_desc if high else rank} "
                    f"{'高' if high else '低'}），"
                    f"与中位水平偏离 {deviation:+.1%}"
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 规则三：绝对值越过阈值
# ---------------------------------------------------------------------------

def _rule_absolute(
    rows: list[dict[str, Any]],
    columns: list[str],
    metric: str | None,
    absolute: Any,
) -> list[Finding]:
    if not isinstance(absolute, dict) or not metric:
        return []
    threshold = absolute.get("threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return []
    direction = absolute.get("direction") or "above"
    severity = absolute.get("severity") or "关注"

    if not rows:
        return []
    value_column = _pick_value_column(columns, rows, metric)
    if value_column is None:
        return []
    dimension_column = _pick_dimension_column(columns, rows, value_column)

    out: list[Finding] = []
    for row in rows:
        value = row.get(value_column)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        crossed = value > threshold if direction == "above" else value < threshold
        if not crossed:
            continue
        out.append(
            {
                "metric": metric,
                "dimension": dimension_column,
                "slice": _slice_value(row, dimension_column),
                "value": round(value, 4),
                "baseline": float(threshold),
                "deviation": None,
                "severity": severity,
                "rule": "absolute",
                "reason": (
                    f"{metric} {_fmt(value)} "
                    f"{'超过' if direction == 'above' else '低于'}阈值 {_fmt(threshold)}"
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _dedupe(findings: list[Finding]) -> list[Finding]:
    """同一（维度, 切片）只留一条：严重度高的优先，同级取偏离大的。

    排名规则与绝对阈值规则经常命中同一行，不去重会在界面上出现两个指向同一
    个切片的标记，用户点开看到的是同一份数据。
    """
    best: dict[tuple[Any, Any], Finding] = {}
    for f in findings:
        key = (f.get("dimension"), f.get("slice"))
        current = best.get(key)
        if current is None or _stronger(f, current):
            best[key] = f
    ordered = sorted(
        best.values(),
        key=lambda f: (
            -SEVERITY_ORDER.get(f.get("severity"), 0),
            -abs(f.get("deviation") or 0.0),
        ),
    )
    return ordered[:MAX_FINDINGS]


def _stronger(left: Finding, right: Finding) -> bool:
    lv = SEVERITY_ORDER.get(left.get("severity"), 0)
    rv = SEVERITY_ORDER.get(right.get("severity"), 0)
    if lv != rv:
        return lv > rv
    lp = RULE_PRIORITY.get(left.get("rule"), 0)
    rp = RULE_PRIORITY.get(right.get("rule"), 0)
    if lp != rp:
        return lp > rp
    return abs(left.get("deviation") or 0.0) > abs(right.get("deviation") or 0.0)


def _pick_value_column(columns: list[str], rows: list[dict[str, Any]], metric: str) -> str | None:
    """选指标列：先按列名与主指标同名，否则取最右侧的数值列。

    「最右」而不是「第一个数值列」，是因为结果表通常把维度放左边、指标放右边；
    维度列本身是数值时（例如按月份分组得到 202608）取第一个会取错。
    """
    if metric in columns:
        return metric
    numeric = [c for c in columns if _is_numeric_column(rows, c)]
    return numeric[-1] if numeric else None


def _pick_dimension_column(
    columns: list[str], rows: list[dict[str, Any]], value_column: str
) -> str | None:
    """选维度列：优先第一个非数值列，全都是数值时取第一个非指标列。

    后一种情况对应「按月份看走势」：202605 这类月份键是整数，按非数值列找
    会找不到维度，结果标记里没有 slice，前端也就无处可点。
    """
    for c in columns:
        if c != value_column and not _is_numeric_column(rows, c):
            return c
    for c in columns:
        if c != value_column:
            return c
    return None


def _is_numeric_column(rows: list[dict[str, Any]], column: str) -> bool:
    values = [r.get(column) for r in rows]
    present = [v for v in values if v is not None]
    if not present:
        return False
    numeric = [v for v in present if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return len(numeric) == len(present)


def _slice_value(row: dict[str, Any], dimension_column: str | None) -> Any:
    if dimension_column is None:
        return None
    return row.get(dimension_column)


def _float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _fmt(value: float) -> str:
    """比率型与金额型共用的展示格式，只用于 reason 文案。

    绝对值不超过 1 的按百分比写——比率型指标（退款率 0.142）写成 14.2% 比
    写成 0.14 好读，而金额与件数几乎都远大于 1，走千分位。
    """
    if -1 <= value <= 1:
        return f"{value:.1%}"
    return f"{value:,.2f}"


__all__ = ["detect_findings", "MAX_FINDINGS"]
