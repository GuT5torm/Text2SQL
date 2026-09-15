"""结果集 → ECharts option。

两条路径并存，以模型判断为主、规则兜底：

  1. visualize 节点让模型输出 ChartSpec（图型 + 字段映射 + 标题）；
  2. 本模块校验模型给的字段是否真实存在于结果集中，字段对不上时按结果集
     结构重新决策，退回规则路径。

这样即使模型选错字段或选错图型，前端也不会渲染出空白图表。规则部分是可单测
的纯函数，答辩时可以逐条演示「什么结构走什么图」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.llm.schemas import ChartSpec

# 列名里出现这些词就按时间维度处理，走折线
_TIME_HINTS = ("月", "日", "年", "季度", "周", "时间", "date", "month", "year", "day", "quarter", "week")
_TIME_VALUE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$|^\d{4}-Q[1-4]$")

# 占比类问题的提示词。命中且只有一个度量时用饼图
_SHARE_HINTS = ("占比", "构成", "比例", "分布", "结构")

CHART_TYPES = ("line", "bar", "pie", "table")


@dataclass
class ChartPayload:
    chart_type: str
    option: dict[str, Any] | None
    reason: str
    warnings: list[str] = field(default_factory=list)
    # 表格兜底时带的列与行，前端可直接渲染
    table_columns: list[str] = field(default_factory=list)
    table_rows: list[dict[str, Any]] = field(default_factory=list)


def _is_numeric(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text:
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


def classify_columns(
    columns: list[str], rows: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """把列分成维度列与度量列。整列都是数值的算度量，其余算维度。"""
    measures: list[str] = []
    dimensions: list[str] = []
    for col in columns:
        values = [r.get(col) for r in rows]
        non_null = [v for v in values if v is not None]
        if non_null and all(_is_numeric(v) for v in non_null):
            measures.append(col)
        else:
            dimensions.append(col)
    return dimensions, measures


def is_time_dimension(column: str, rows: list[dict[str, Any]]) -> bool:
    if any(h in column.lower() for h in _TIME_HINTS):
        return True
    samples = [str(r.get(column)) for r in rows[:20] if r.get(column) is not None]
    if not samples:
        return False
    return sum(1 for s in samples if _TIME_VALUE_RE.match(s)) >= max(1, len(samples) // 2)


def decide_chart_type(
    columns: list[str], rows: list[dict[str, Any]], question: str = ""
) -> tuple[str, str]:
    """规则决策，返回 (图型, 理由)。"""
    if not rows:
        return "table", "结果集为空，无可视化内容"

    dimensions, measures = classify_columns(columns, rows)

    if len(columns) > 4 or len(measures) == 0:
        return "table", "列数较多或没有可用度量，退回表格更易读"
    if len(dimensions) == 0:
        return "table", "没有可用于横轴的维度"

    if any(h in question for h in _SHARE_HINTS) and len(dimensions) == 1 and len(measures) == 1 and len(rows) <= 12:
        return "pie", "问题问的是占比，且维度取值不超过 12 个"

    if is_time_dimension(dimensions[0], rows):
        return "line", f"横轴「{dimensions[0]}」为时间维度，趋势用折线"

    if len(dimensions) == 1 and len(rows) <= 30:
        return "bar", f"单维度「{dimensions[0]}」对比，用柱状"

    return "table", "维度组合不适合用单一图型表达"


def validate_spec(
    spec: ChartSpec, columns: list[str], rows: list[dict[str, Any]]
) -> tuple[ChartSpec | None, list[str]]:
    """校验模型给的字段映射。字段对不上就整体作废，退回规则决策。"""
    warnings: list[str] = []
    if spec.chart_type not in CHART_TYPES:
        return None, [f"模型给出的图型 {spec.chart_type} 不在允许范围内"]

    if spec.chart_type == "table":
        return spec, warnings

    present = set(columns)
    for fld in [spec.x_field, *spec.y_fields, spec.series_field]:
        if fld and fld not in present:
            return None, [f"模型指定的字段「{fld}」不在结果集列中，已按规则重新选择图型"]

    dimensions, measures = classify_columns(columns, rows)
    x = spec.x_field or (dimensions[0] if dimensions else None)
    ys = [y for y in spec.y_fields if y in present] or measures
    if x is None or not ys:
        return None, ["结果集缺少可用的维度或度量字段，已按规则重新选择图型"]

    if spec.x_field is None:
        warnings.append(f"模型未指定横轴，已取第一个维度「{x}」")
    if not spec.y_fields:
        warnings.append(f"模型未指定度量，已取「{'、'.join(ys[:3])}」")
    return spec.model_copy(update={"x_field": x, "y_fields": ys}), warnings


def _axis_label(rows: list[dict[str, Any]], field_name: str) -> dict[str, Any]:
    longest = max((len(str(r.get(field_name, ""))) for r in rows), default=0)
    # 标签过长时倾斜，避免横轴文字互相压盖
    return {"rotate": 30, "interval": 0} if longest > 6 else {"interval": "auto"}


def build_option(
    spec: ChartSpec, columns: list[str], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """按已校验的 spec 生成 ECharts option。"""
    title = spec.title or "查询结果"
    base: dict[str, Any] = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "item" if spec.chart_type == "pie" else "axis"},
        "grid": {"left": 48, "right": 24, "bottom": 56, "top": 48, "containLabel": True},
    }

    x = spec.x_field
    ys = spec.y_fields
    assert x is not None and ys

    if spec.chart_type == "pie":
        y = ys[0]
        base["legend"] = {"bottom": 0, "type": "scroll"}
        base["series"] = [
            {
                "type": "pie",
                "radius": ["38%", "62%"],
                "center": ["50%", "48%"],
                "data": [{"name": str(r.get(x)), "value": r.get(y)} for r in rows],
                "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.2)"}},
                "label": {"formatter": "{b}: {d}%"},
            }
        ]
        return base

    categories = [str(r.get(x)) for r in rows]
    base["xAxis"] = {
        "type": "category",
        "data": categories,
        "axisLabel": _axis_label(rows, x),
        "name": x,
        "nameLocation": "middle",
        "nameGap": 32,
    }
    base["yAxis"] = {"type": "value", "name": ys[0] if len(ys) == 1 else ""}

    # series_field 存在时按该列拆成多条系列，例如「各区域各品类」按区域拆分
    series: list[dict[str, Any]] = []
    if spec.series_field and spec.series_field in columns:
        groups: dict[str, list[Any]] = {}
        for r in rows:
            groups.setdefault(str(r.get(spec.series_field)), []).append(r)
        for name, group in groups.items():
            by_x = {str(g.get(x)): g for g in group}
            for y in ys:
                series.append(
                    {
                        "name": f"{name} · {y}" if len(ys) > 1 else name,
                        "type": spec.chart_type,
                        "data": [by_x.get(c, {}).get(y) for c in categories],
                        "emphasis": {"focus": "series"},
                    }
                )
    else:
        for y in ys:
            series.append(
                {
                    "name": y,
                    "type": spec.chart_type,
                    "data": [r.get(y) for r in rows],
                    "smooth": spec.chart_type == "line",
                    "emphasis": {"focus": "series"},
                }
            )
    base["series"] = series
    if len(series) > 1:
        base["legend"] = {"bottom": 0, "type": "scroll"}
    return base


def build_chart(
    columns: list[str],
    rows: list[dict[str, Any]],
    spec: ChartSpec | None = None,
    question: str = "",
) -> ChartPayload:
    """总入口：优先用模型给的 spec，校验不过则按规则兜底。"""
    warnings: list[str] = []

    if not rows or not columns:
        return ChartPayload(
            chart_type="table",
            option=None,
            reason="结果集为空，无图表可渲染",
            warnings=["查询没有返回数据，请确认筛选条件是否过严"] if not rows else [],
            table_columns=columns,
            table_rows=rows,
        )

    chosen: ChartSpec | None = None
    reason = ""
    if spec is not None:
        valid, warns = validate_spec(spec, columns, rows)
        warnings.extend(warns)
        if valid is not None and valid.chart_type != "table":
            chosen = valid
            reason = spec.reason or "由模型选择合适的图型"

    if chosen is None:
        chart_type, rule_reason = decide_chart_type(columns, rows, question)
        if not reason:
            reason = rule_reason
        if chart_type == "table":
            return ChartPayload(
                chart_type="table",
                option=None,
                reason=reason,
                warnings=warnings,
                table_columns=columns,
                table_rows=rows,
            )
        dimensions, measures = classify_columns(columns, rows)
        chosen = ChartSpec(
            chart_type=chart_type,  # type: ignore[arg-type]
            x_field=dimensions[0],
            y_fields=measures[:3],
            title=question[:40] if question else "查询结果",
            reason=reason,
        )

    if chosen.chart_type == "table":
        return ChartPayload(
            chart_type="table",
            option=None,
            reason=reason or "模型判断用表格展示",
            warnings=warnings,
            table_columns=columns,
            table_rows=rows,
        )

    return ChartPayload(
        chart_type=chosen.chart_type,
        option=build_option(chosen, columns, rows),
        reason=reason or chosen.reason,
        warnings=warnings,
        table_columns=columns,
        table_rows=rows,
    )
