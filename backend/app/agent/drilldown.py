"""FR-L-2 下钻：从异常标记与结果表单元格构造下一轮问句。

实现不是新链路，而是槽位继承。异常标记自带 metric、dimension、slice 三个
字段，把它们加一层维度后拼成完整问句，走现有的
route → retrieve → generate → validate → execute 链路即可。

两个触发点：

    finding  点击异常标记 → 该指标 + 该切片 + 加深一层维度
    cell     点击结果表单元格 → 同口径 + 该维度值过滤（不加深）

为什么 cell 不加深：点击数字的目的是验证这个数字怎么来的，问句应当还原
「同口径 + 该维度值过滤」的查询，而不是跳到另一个维度。加深一层是异常标记
的职责——标记已经说明「这里不对」，下一步才是「按什么拆开看」。

一次只加深一层，不做多级递归。递归要引入路径管理与返回上一级的状态机，
收益不足以支撑这份复杂度；需要更深时连续点击即可，每一层都是普通轮次，
照常落库、照常可回看。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.semantic.loader import Catalog

# 时间类维度不参与下钻。它们已经由 time_range 承担，再作为「加深的一层」
# 会变成「按月份再看一遍」，与用户点开某个区域想看细分的预期不符。
TIME_DIMENSIONS = frozenset({"order_date", "order_month", "order_quarter"})

DrillSource = Literal["finding", "cell"]


@dataclass(frozen=True)
class DrilldownPlan:
    """一次下钻的产出。前端拿 question 走 /api/chat，其余字段用于展示与断言。"""

    question: str
    source: DrillSource
    metric: str | None
    dimension: str | None  # 当前切片所属维度
    slice_value: str | None  # 切片取值
    next_dimension: str | None  # 加深的那一层；cell 触发时为 None
    next_dimension_display: str | None
    # 构造不出来时的说明。前端据此禁用按钮并给出原因，比静默无响应好
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.question)


def build_drilldown(
    *,
    source: DrillSource,
    metric: str | None,
    dimension: str | None = None,
    slice_value: str | None = None,
    catalog: Catalog,
    time_label: str | None = None,
) -> DrilldownPlan:
    """按槽位继承规则拼出下一轮问句。

    metric / dimension 既可能是显示名（成交总额、销售大区）也可能是配置名
    （gmv、region）——finding 里写的是显示名，前端也可能直接传配置名，这里都认。
    """
    if source not in ("finding", "cell"):
        raise ValueError(f"下钻来源非法：{source}")

    metric_def = _resolve_metric(catalog, metric)
    metric_display = (metric_def or {}).get("display") or metric or "该指标"

    if source == "cell":
        if not slice_value:
            return DrilldownPlan(
                question="", source=source, metric=metric_display, dimension=dimension,
                slice_value=slice_value, next_dimension=None, next_dimension_display=None,
                reason="单元格下钻需要给出该行的维度取值",
            )
        question = f"{time_label or ''}{slice_value}的{metric_display}"
        return DrilldownPlan(
            question=question,
            source=source,
            metric=metric_display,
            dimension=dimension,
            slice_value=slice_value,
            next_dimension=None,
            next_dimension_display=None,
            reason="同口径下钻：沿用本轮指标，只按该维度取值过滤",
        )

    next_dim = _next_dimension(catalog, metric_def, dimension)
    if next_dim is None:
        return DrilldownPlan(
            question="", source=source, metric=metric_display, dimension=dimension,
            slice_value=slice_value, next_dimension=None, next_dimension_display=None,
            reason="该指标已无可加深的维度，下钻到此为止",
        )

    next_display = next_dim.get("display") or next_dim["name"]
    head = f"{time_label or ''}{slice_value or ''}"
    # 没有时间限定也没有切片时不能留下一个孤零零的「的」字
    question = (
        f"{head}的{metric_display}按{next_display}分布" if head
        else f"{metric_display}按{next_display}分布"
    )
    return DrilldownPlan(
        question=question,
        source=source,
        metric=metric_display,
        dimension=dimension,
        slice_value=slice_value,
        next_dimension=next_dim["name"],
        next_dimension_display=next_display,
        reason=f"在「{dimension or '整体'}」基础上加深一层到「{next_display}」",
    )


def _resolve_metric(catalog: Catalog, metric: str | None) -> dict[str, Any] | None:
    if not metric:
        return None
    if metric in catalog.metric_by_name:
        return catalog.metric_by_name[metric]
    name = catalog.metric_name_by_display.get(metric)
    return catalog.metric_by_name.get(name) if name else None


def _next_dimension(
    catalog: Catalog, metric_def: dict[str, Any] | None, current: str | None
) -> dict[str, Any] | None:
    """取加深的那一层维度：指标声明的维度里，当前维度之后的第一个非时间维度。

    顺序取自 metrics.yaml 的 dimensions 列表——它由业务约定写在配置里，比在
    代码里另排一份顺序更容易改，也不会出现「配置说有这个维度、代码却跳过了它」。
    """
    candidates = _candidate_dimension_names(catalog, metric_def)
    if not candidates:
        return None

    if current:
        current_name = _dimension_name(catalog, current)
        if current_name in candidates:
            after = candidates[candidates.index(current_name) + 1:]
            return catalog.dimension_by_name.get(after[0]) if after else None
        # 当前维度不在该指标的维度列表里（例如按城市看 gmv），从头取第一个
    return catalog.dimension_by_name.get(candidates[0])


def _candidate_dimension_names(
    catalog: Catalog, metric_def: dict[str, Any] | None
) -> list[str]:
    if metric_def:
        declared = [
            d for d in (metric_def.get("dimensions") or [])
            if d not in TIME_DIMENSIONS and d in catalog.dimension_by_name
        ]
        if declared:
            return declared
    # 指标缺失或未声明维度时退回全局顺序：至少还能下钻，而不是直接不可用
    return [
        d["name"]
        for d in catalog.dimensions
        if d["name"] not in TIME_DIMENSIONS
    ]


def _dimension_name(catalog: Catalog, dimension: str) -> str:
    """把维度写法归一成配置名。

    入来源有三种：配置名（region）、显示名（销售大区）、结果表的列别名（大区）。
    第三种最容易被忽略——findings 里的 dimension 取的是结果集列名，而列名来自
    SQL 的 AS，写的是口语简称。列别名都登记在维度的 synonyms 里，这里一并认。
    """
    if dimension in catalog.dimension_by_name:
        return dimension
    for d in catalog.dimensions:
        if dimension in {d.get("display"), *(d.get("synonyms") or [])}:
            return d["name"]
    return dimension


__all__ = ["build_drilldown", "DrilldownPlan", "TIME_DIMENSIONS"]
