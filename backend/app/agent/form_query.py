"""复杂查询表单的适配层：结构化条件 → 槽位 / 可读问句 / 谓词清单。

表单查询与自由提问的分界线在这里：条件是用户在界面上确定的，route 不需要
（也不应该）再让模型从问句里抽一遍。本模块把表单结构翻译成链路里已经存在的
三样东西——六类槽位、一句可读问句、一段谓词清单——之后走的是同一张图。

放 agent/ 而不是 semantic/：槽位的形态是编排层的契约（前端标注、槽位覆盖核对
都按它取数），问句渲染又依赖 slots 的拼法，两者都不属于语义层。
"""

from __future__ import annotations

from typing import Any

from app.semantic.filters import (
    ResolvedCondition,
    describe_condition,
    render_predicate,
    resolve_conditions,
    resolve_dimension_def,
    resolve_metric_def,
    value_labels,
)
from app.semantic.loader import Catalog


def build_form_payload(payload: dict[str, Any], catalog: Catalog) -> dict[str, Any]:
    """把请求体归一成 state 要的两个字段。

    维度在这里归一成配置名（region 而不是 品类），下游的槽位渲染、强断言都按
    配置名取定义，避免「显示名改了，历史落库的槽位取不到定义」这类漂移。
    """
    conditions = resolve_conditions(payload, catalog)
    dimensions: list[str] = []
    for token in payload.get("dimensions") or []:
        dim = resolve_dimension_def(catalog, str(token))
        dimensions.append(dim["name"] if dim else str(token))
    return {
        "form_query": {
            "metric": str(payload.get("metric") or "").strip(),
            "dimensions": dimensions,
            "order_by": payload.get("order_by"),
            "top_n": payload.get("top_n"),
        },
        "form_conditions": [c.as_dict() for c in conditions],
    }


def build_slots_from_form(
    form_query: dict[str, Any],
    conditions: list[Any],
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    """表单 → 六类槽位。

    与模型抽取的槽位形态保持一致（filter 的 field 用显示名、时间只作标签），
    否则槽位覆盖核对与前端标注认不出表单填进来的内容。
    """
    metric_def = resolve_metric_def(catalog, (form_query or {}).get("metric"))
    slots: dict[str, Any] = {
        "metrics": [metric_def.get("display") or form_query.get("metric")]
        if metric_def
        else ([form_query.get("metric")] if form_query.get("metric") else []),
        "dimensions": _dimension_displays(form_query.get("dimensions"), catalog),
        "filters": [],
        "time_range": "",
        "order_by": form_query.get("order_by"),
        "top_n": form_query.get("top_n"),
    }

    for cond in as_conditions(conditions):
        if cond.op in ("eq", "in"):
            labels = value_labels(catalog, cond)
            if labels:
                slots["filters"].append({"field": cond.display, "value": labels[0]})
            continue
        # 时间类：slots 只留标签与边界，日期区间本身进 form_conditions
        if cond.op == "between":
            slots["time_range"] = f"{cond.start} 至 {cond.end}"
            slots["filters"].append(
                {"field": cond.display, "op": ">=", "value": cond.start}
            )
        elif cond.op == "gte":
            slots["time_range"] = f"{cond.start} 起"
            slots["filters"].append(
                {"field": cond.display, "op": ">=", "value": cond.start}
            )
        elif cond.op == "lte":
            slots["time_range"] = f"截至 {cond.end}"
            slots["filters"].append(
                {"field": cond.display, "op": "<=", "value": cond.end}
            )
    return slots


def render_form_question(
    form_query: dict[str, Any],
    conditions: list[Any],
    catalog: Catalog | None = None,
) -> str:
    """渲染一句可读问句，供落库、历史回看与召回使用。

    表单没有「用户原话」，落库时这句话就是原话。它要过两关：召回能认出指标
    （否则口径标注为空），人能看懂（否则回看时只是一串字段名）。
    """
    metric_def = resolve_metric_def(catalog, (form_query or {}).get("metric"))
    metric = (
        metric_def.get("display")
        if metric_def
        else str((form_query or {}).get("metric") or "指标")
    )

    head, filters_text = "", []
    for cond in as_conditions(conditions):
        if _is_time(cond):
            head = _time_phrase(cond)
            continue
        filters_text.append(describe_condition(cond, catalog))

    if head and filters_text:
        body = f"{head}，{'，'.join(filters_text)}的{metric}"
    elif filters_text:
        body = f"{'，'.join(filters_text)}的{metric}"
    elif head:
        body = f"{head}的{metric}"
    else:
        body = metric

    dims = _dimension_displays(form_query.get("dimensions"), catalog)
    if dims:
        body += "，按" + "、".join(dims) + "分组"
    top_n = (form_query or {}).get("top_n")
    if top_n:
        body += f"，取前 {top_n}"
    return body


def render_condition_block(
    conditions: list[Any],
    catalog: Catalog | None = None,
    form_query: dict[str, Any] | None = None,
) -> str:
    """生成 generate 提示词的「必须满足的查询条件」一节。无条件时返回空串。

    整节拼好再交给模板，而不是让模板按占位符是否存在决定输不输出——半截标题
    比空节更难排查。

    分组维度一并写进这一节：表单里维度的个数与顺序是确定的，交给模型自选会
    出现「结果集相同、行序不同、图表图例错乱」这类不报错的偏差（F6）。
    """
    resolved = as_conditions(conditions)
    if not resolved:
        return ""

    lines = [
        "## 必须满足的查询条件（来自查询表单，逐条落入 SQL）",
        "",
        "以下条件由用户在查询表单中确定，必须全部出现在 WHERE 或 HAVING 中。"
        "取值不得改写，算子不得更换；between 为左闭右开：",
        "",
    ]
    lines += [
        f"{i}. `{render_predicate(c)}`" for i, c in enumerate(resolved, start=1)
    ]

    joins = [c for c in resolved if c.join]
    if joins:
        lines += [
            "",
            "以下条件涉及其他表，必须写 JOIN。漏 JOIN 时若列名恰好存在于 FROM 里的"
            "另一张表，语句照常执行、过滤落在错误的列上，全程无报错：",
        ]
        lines += [f"- `{c.field}`：`{c.join}`" for c in joins]

    notes = [c for c in resolved if c.note]
    if notes:
        lines += ["", "条件的使用约束："]
        lines += [f"- `{c.field}`：{c.note}" for c in notes]

    dims = _dimension_lines(form_query, catalog)
    if dims:
        lines += [
            "",
            "分组维度按下述顺序与个数输出，不要自行增删（顺序变了结果集相同、"
            "行序不同，前端图表系列会错乱）：",
        ]
        lines += [f"- {d}" for d in dims]
    return "\n".join(lines)


def _dimension_lines(
    form_query: dict[str, Any] | None, catalog: Catalog | None
) -> list[str]:
    """分组维度的展示行：显示名 + 表达式。表达式让模型不必回推语义层。"""
    out: list[str] = []
    for token in (form_query or {}).get("dimensions") or []:
        dim = resolve_dimension_def(catalog, str(token))
        if dim is None:
            out.append(str(token))
            continue
        out.append(
            f"{dim.get('display') or dim['name']}：`{dim.get('expression')}`"
        )
    return out


# ---------------------------------------------------------------------------


def as_conditions(conditions: list[Any]) -> list[ResolvedCondition]:
    """state 里存的是普通字典（要能进 Checkpointer），这里还原成对象。"""
    out: list[ResolvedCondition] = []
    for raw in conditions or []:
        if isinstance(raw, ResolvedCondition):
            out.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        data = dict(raw)
        data["values"] = tuple(data.get("values") or ())
        out.append(
            ResolvedCondition(
                field=str(data.get("field") or ""),
                display=str(data.get("display") or ""),
                op=str(data.get("op") or ""),
                table=str(data.get("table") or ""),
                column=str(data.get("column") or ""),
                values=data["values"],
                start=data.get("start"),
                end=data.get("end"),
                join=data.get("join"),
                note=data.get("note"),
            )
        )
    return out


def _is_time(cond: ResolvedCondition) -> bool:
    return cond.op in ("between", "gte", "lte")


def _time_phrase(cond: ResolvedCondition) -> str:
    if cond.op == "between":
        return f"{cond.start} 至 {cond.end}"
    if cond.op == "gte":
        return f"{cond.start} 起"
    return f"截至 {cond.end}"


def _dimension_displays(dimensions: list[Any], catalog: Catalog | None) -> list[str]:
    out: list[str] = []
    for token in dimensions or []:
        dim = resolve_dimension_def(catalog, str(token))
        out.append((dim or {}).get("display") or str(token))
    return out


__all__ = [
    "as_conditions",
    "build_form_payload",
    "build_slots_from_form",
    "render_condition_block",
    "render_form_question",
]
