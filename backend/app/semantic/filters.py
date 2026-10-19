"""表单查询条件的白名单校验与谓词渲染。

自由提问的条件由模型从问句里抽，抽漏了没有任何报错；表单查询的条件是用户在
界面上逐条选定的，确定的东西不该再交给模型猜。代价是必须自己把住两道口：

1. 提交前：字段、算子、取值必须对得上语义层登记（filters.yaml），不匹配的
   一律拒绝，并把「哪一项不匹配、可用的是什么」说清楚。否则用户看到的是守卫
   拦下的一条 R5 错误码，而不是「这个字段没有登记，不能按它查」；
2. 生成前：条件渲染成 SQL 谓词清单交回给模型，取值标准值化、区间左闭右开，
   模型不需要再猜写法。

本模块是纯函数模块：不连接数据库、不调用模型，与 loader 同层。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from app.semantic.loader import Catalog, FILTER_OPS

# 时间与日期类条件固定落在订单下单时间上。语义层的全部时间谓词都写在
# orders.created_at，data_as_of 也按它取，表单没有第二个选择。
TIME_FIELD = "orders.created_at"

# 条件数量上限。表单在界面上逐条加行，不加限制时一条超长条件链会让生成的
# 语句既不可读也不可核对；超出后报错比静默截断好。
MAX_CONDITIONS = 12
MAX_IN_VALUES = 20
# 与守卫的 max_rows 对齐，超过 1000 的 TopN 会被守卫改写，不如在提交前说清
MAX_TOP_N = 1000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ResolvedCondition:
    """一条校验通过的表单条件。

    values 存库内标准值（enum 的 key），start / end 存日期区间的起止。
    两者分开而不是塞进同一个 value 字段，是因为强断言对它们的判定方式不同：
    取值类要求每个值都出现在 IN 列表里，区间类要求两个边界都在语句里。
    """

    field: str  # 登记名，如 orders.region
    display: str  # 中文显示名，用于问句渲染与回显
    op: str
    table: str
    column: str
    values: tuple[str, ...] = ()
    start: str | None = None
    end: str | None = None
    join: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """转成可落进 state 的普通字典。tuple 换成 list，便于 JSON 化。"""
        return {
            "field": self.field,
            "display": self.display,
            "op": self.op,
            "table": self.table,
            "column": self.column,
            "values": list(self.values),
            "start": self.start,
            "end": self.end,
            "join": self.join,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 语义层解析
# ---------------------------------------------------------------------------


def resolve_metric_def(catalog: Catalog, metric: str | None) -> dict[str, Any] | None:
    """指标写法归一成定义。配置名（gmv）与显示名（成交总额）都认。"""
    if not metric:
        return None
    if metric in catalog.metric_by_name:
        return catalog.metric_by_name[metric]
    name = catalog.metric_name_by_display.get(metric)
    return catalog.metric_by_name.get(name) if name else None


def resolve_dimension_def(catalog: Catalog, token: str | None) -> dict[str, Any] | None:
    """维度写法归一成定义。配置名、显示名与同义词都认。"""
    if not token:
        return None
    if token in catalog.dimension_by_name:
        return catalog.dimension_by_name[token]
    for d in catalog.dimensions:
        if token in {d.get("display"), *(d.get("synonyms") or [])}:
            return d
    return None


def _enum_of(catalog: Catalog, table: str, column: str) -> dict[str, str]:
    t = catalog.table_by_name.get(table)
    if t is None:
        return {}
    for f in t.get("fields") or []:
        if f.get("name") == column:
            return dict(f.get("enum") or {})
    return {}


def _value_label(catalog: Catalog, cond: ResolvedCondition, value: str) -> str:
    """取值的中文标签。库内标准值（gold）对人不友好，回显与问句里说「金卡」。"""
    mapping = _enum_of(catalog, cond.table, cond.column)
    return mapping.get(value, value)


def value_labels(catalog: Catalog | None, cond: ResolvedCondition) -> list[str]:
    if catalog is None:
        return list(cond.values)
    return [_value_label(catalog, cond, v) for v in cond.values]


# ---------------------------------------------------------------------------
# 提交前校验
# ---------------------------------------------------------------------------


def validate_form_query(payload: dict[str, Any], catalog: Catalog) -> list[str]:
    """校验一份表单查询，返回全部问题（空列表表示通过）。

    一次列全部问题而不是遇错即返：表单一次提交六七个字段，逐个报错要来回
    六次才能提交成功。
    """
    problems: list[str] = []

    metric = str(payload.get("metric") or "").strip()
    metric_def = resolve_metric_def(catalog, metric)
    if not metric:
        problems.append("指标 metric 不能为空")
    elif metric_def is None:
        problems.append(
            f"指标「{metric}」未在语义层登记，可用指标："
            + "、".join(m["display"] for m in catalog.metrics)
        )

    for token in payload.get("dimensions") or []:
        problems.extend(_validate_dimension(catalog, metric_def, str(token)))

    conditions = list(payload.get("conditions") or [])
    if len(conditions) > MAX_CONDITIONS:
        problems.append(
            f"条件数量 {len(conditions)} 超过上限 {MAX_CONDITIONS}，请拆成多个查询"
        )
    for idx, raw in enumerate(conditions, start=1):
        problems.extend(_validate_condition(raw, catalog, idx))

    time_range = payload.get("time_range")
    problems.extend(_validate_exclusions(conditions, catalog))
    # 顶部时间区间与条件行里的下单时间是同一个字段，合并后再查重
    problems.extend(_validate_duplicate_fields(conditions, catalog, time_range))
    problems.extend(_validate_time_range(time_range))

    top_n = payload.get("top_n")
    if top_n is not None:
        try:
            number = int(str(top_n).strip())
        except (TypeError, ValueError):
            problems.append(f"top_n 必须是整数，当前为 {top_n}")
        else:
            if not 1 <= number <= MAX_TOP_N:
                problems.append(f"top_n 必须在 1 到 {MAX_TOP_N} 之间，当前为 {number}")

    order_by = payload.get("order_by")
    if order_by is not None:
        if not isinstance(order_by, dict):
            problems.append("order_by 必须是 {field, direction} 结构")
        else:
            direction = str(order_by.get("direction") or "").strip().lower()
            if direction not in ("asc", "desc"):
                problems.append(
                    f"order_by.direction 只能是 asc 或 desc，当前为 {direction or '空'}"
                )
            if not str(order_by.get("field") or "").strip():
                problems.append("order_by.field 不能为空")

    return problems


def _validate_dimension(
    catalog: Catalog, metric_def: dict[str, Any] | None, token: str
) -> list[str]:
    dim = resolve_dimension_def(catalog, token)
    if dim is None:
        return [f"分组维度「{token}」未在语义层登记"]
    if metric_def is None:
        return []
    allowed = list(metric_def.get("dimensions") or [])
    if allowed and dim["name"] not in allowed:
        names = [
            (catalog.dimension_by_name.get(n) or {}).get("display") or n for n in allowed
        ]
        return [
            f"指标「{metric_def.get('display') or metric_def.get('name')}」"
            f"不支持按「{token}」分组，可用维度：" + "、".join(names)
        ]
    return []


def _validate_condition(raw: Any, catalog: Catalog, idx: int) -> list[str]:
    prefix = f"第 {idx} 个条件"
    if not isinstance(raw, dict):
        return [f"{prefix}必须是 {{field, op, value}} 结构"]

    field_name = str(raw.get("field") or "").strip()
    if not field_name:
        return [f"{prefix}缺少 field"]
    entry = catalog.filter_by_name.get(field_name)
    if entry is None:
        return [
            f"{prefix}的字段「{field_name}」未登记为可用条件，可用字段："
            + "、".join(f"{f['field']}（{f.get('display') or ''}）" for f in catalog.filter_fields)
        ]

    op = str(raw.get("op") or "").strip()
    if op not in FILTER_OPS:
        return [f"{prefix}的算子「{op or '空'}」非法，允许 {list(FILTER_OPS)}"]
    if op not in (entry.get("ops") or []):
        return [
            f"{prefix}：字段 {field_name} 不支持算子 {op}，"
            f"支持 {list(entry.get('ops') or [])}"
        ]

    value = raw.get("value")
    value_to = raw.get("value_to")

    if op in ("eq", "in"):
        raws = value if isinstance(value, list) else ([value] if value is not None else [])
        if op == "in" and not isinstance(value, list):
            return [f"{prefix}：算子 in 的 value 必须是数组"]
        if not raws:
            return [f"{prefix}：算子 {op} 缺少取值"]
        if len(raws) > MAX_IN_VALUES:
            return [f"{prefix}的取值数量 {len(raws)} 超过上限 {MAX_IN_VALUES}"]
        return _validate_enum_values(catalog, entry, raws, prefix)

    if op == "between":
        start = _as_date(value)
        end = _as_date(value_to)
        if start is None:
            return [f"{prefix}：between 缺少起始日期 value（格式 YYYY-MM-DD）"]
        if end is None:
            return [f"{prefix}：between 缺少结束日期 value_to（格式 YYYY-MM-DD）"]
        if start >= end:
            return [f"{prefix}：区间起始 {start} 不早于结束 {end}"]
        return []

    # gte / lte
    point = _as_date(value)
    if point is None:
        return [f"{prefix}：算子 {op} 需要日期取值（格式 YYYY-MM-DD），当前为 {value}"]
    return []


def _validate_enum_values(
    catalog: Catalog, entry: dict[str, Any], values: list[Any], prefix: str
) -> list[str]:
    """取值必须在字段的 enum 里。key 与显示值都接受，提交期统一成 key。"""
    field_name = str(entry.get("field") or "")
    table, column = field_name.split(".", 1)
    mapping = _enum_of(catalog, table, column)
    accepted = set(mapping) | set(mapping.values())
    # 没有 enum 的字段（values_from: date 之类）不做取值校验，
    # 那类字段也不会登记 eq / in，加载期已拦过
    if not accepted:
        return []
    problems = []
    for value in values:
        text = str(value or "").strip()
        if text not in accepted:
            problems.append(
                f"{prefix}：字段 {field_name} 没有取值「{text}」，"
                f"可用取值：" + "、".join(sorted(mapping.values()) or sorted(mapping))
            )
    return problems


def _validate_exclusions(
    conditions: list[Any], catalog: Catalog
) -> list[str]:
    """互斥条件不能同时出现。

    规则登记在 filters.yaml 的 exclude_with 上，前后端同一份：前端用它置灰
    选项，后端用它拒绝提交。只写一边会出现「界面置灰了但接口仍接受」的口子。
    """
    used: list[str] = []
    for raw in conditions:
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field") or "").strip()
        if field in catalog.filter_by_name:
            used.append(field)

    problems: list[str] = []
    reported: set[tuple[str, str]] = set()
    for field in used:
        entry = catalog.filter_by_name[field]
        for other in entry.get("exclude_with") or []:
            if other not in used:
                continue
            key = tuple(sorted((field, str(other))))
            if key in reported:
                continue
            reported.add(key)
            left = str((catalog.filter_by_name.get(field) or {}).get("display") or field)
            right = str((catalog.filter_by_name.get(str(other)) or {}).get("display") or other)
            problems.append(
                f"条件「{left}」与「{right}」不能同时使用：两者在业务上不同时成立，"
                f"组合后结果恒为空集"
            )
    return problems


def _validate_duplicate_fields(
    conditions: list[Any], catalog: Catalog, time_range: Any = None
) -> list[str]:
    """同一字段只接受一个条件。

    两个条件落在同一字段上是「且」的关系，`region = 华东 AND region = 华南` 恒为
    空集，`created_at >= A AND created_at >= B` 则只留更严的那个——两种都不是用户
    想要的结果，且都不会报错。多值应当用 in，不该加两行。

    顶部的时间区间会被解析成同一个 created_at 条件，因此一并参与查重，否则
    「区间 + 条件行」这种重复查不出来。
    """
    counts: dict[str, int] = {}
    if isinstance(time_range, dict) and time_range.get("start"):
        counts[TIME_FIELD] = 1
    for raw in conditions:
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field") or "").strip()
        if field in catalog.filter_by_name:
            counts[field] = counts.get(field, 0) + 1

    problems: list[str] = []
    for field, count in counts.items():
        if count < 2:
            continue
        entry = catalog.filter_by_name[field]
        display = str(entry.get("display") or field)
        extra = (
            "顶部的时间区间与条件行里的下单时间二选一即可"
            if field == TIME_FIELD
            else f"多个取值请用算子 in，不要加 {count} 行"
        )
        problems.append(f"条件「{display}」出现了 {count} 次：{extra}")
    return problems


def _validate_time_range(time_range: Any) -> list[str]:
    if time_range is None:
        return []
    if not isinstance(time_range, dict):
        return ["time_range 必须是 {start, end} 结构"]
    start = _as_date(time_range.get("start"))
    end = _as_date(time_range.get("end"))
    if start is None:
        return ["time_range.start 缺失或不是日期（格式 YYYY-MM-DD）"]
    if end is None:
        return ["time_range.end 缺失或不是日期（格式 YYYY-MM-DD）"]
    if start >= end:
        return [f"time_range 的起始 {start} 不早于结束 {end}"]
    return []


def _as_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not _DATE_RE.match(text):
        return None
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return None
    return text


# ---------------------------------------------------------------------------
# 条件解析
# ---------------------------------------------------------------------------


def resolve_conditions(
    payload: dict[str, Any], catalog: Catalog
) -> list[ResolvedCondition]:
    """把表单条件解析成结构化对象。调用前必须先过 validate_form_query。

    time_range 是 created_at 区间的简便写法，这里合并进条件列表；请求里已经
    写了同一区间时不去重追加，避免同一区间在谓词清单里出现两遍。
    """
    out: list[ResolvedCondition] = []
    for raw in payload.get("conditions") or []:
        if not isinstance(raw, dict):  # pragma: no cover - 校验已拦过
            continue
        entry = catalog.filter_by_name.get(str(raw.get("field") or "").strip())
        if entry is None:  # pragma: no cover - 校验已拦过
            continue
        cond = _resolve_one(raw, entry, catalog)
        if cond is not None:
            out.append(cond)

    time_range = payload.get("time_range")
    if isinstance(time_range, dict):
        start = _as_date(time_range.get("start"))
        end = _as_date(time_range.get("end"))
        if start and end:
            duplicated = any(
                c.field == TIME_FIELD and c.start == start and c.end == end for c in out
            )
            if not duplicated:
                out.append(
                    ResolvedCondition(
                        field=TIME_FIELD,
                        display=_display_of(catalog, TIME_FIELD, "下单时间"),
                        op="between",
                        table="orders",
                        column="created_at",
                        start=start,
                        end=end,
                    )
                )
    return out


def _resolve_one(
    raw: dict[str, Any], entry: dict[str, Any], catalog: Catalog
) -> ResolvedCondition | None:
    field_name = str(entry.get("field") or "")
    table, column = field_name.split(".", 1)
    op = str(raw.get("op") or "").strip()
    mapping = _enum_of(catalog, table, column)
    # 显示值（金卡）与库内值（gold）都能提交，统一成库内值再往下游走
    reverse = {v: k for k, v in mapping.items()}

    if op in ("eq", "in"):
        raws = raw.get("value")
        raws = raws if isinstance(raws, list) else [raws]
        values = tuple(_standardize(str(v or "").strip(), mapping, reverse) for v in raws if v)
        return ResolvedCondition(
            field=field_name,
            display=str(entry.get("display") or column),
            op=op,
            table=table,
            column=column,
            values=values,
            join=str(entry.get("join") or "") or None,
            note=str(entry.get("note") or "").strip() or None,
        )

    if op == "between":
        return ResolvedCondition(
            field=field_name,
            display=str(entry.get("display") or column),
            op=op,
            table=table,
            column=column,
            start=_as_date(raw.get("value")),
            end=_as_date(raw.get("value_to")),
            join=str(entry.get("join") or "") or None,
            note=str(entry.get("note") or "").strip() or None,
        )

    point = _as_date(raw.get("value"))
    return ResolvedCondition(
        field=field_name,
        display=str(entry.get("display") or column),
        op=op,
        table=table,
        column=column,
        start=point if op == "gte" else None,
        end=point if op == "lte" else None,
        join=str(entry.get("join") or "") or None,
        note=str(entry.get("note") or "").strip() or None,
    )


def _standardize(value: str, mapping: dict[str, str], reverse: dict[str, str]) -> str:
    if value in mapping:
        return value
    return reverse.get(value, value)


def _display_of(catalog: Catalog, field_name: str, fallback: str) -> str:
    entry = catalog.filter_by_name.get(field_name)
    return str((entry or {}).get("display") or fallback)


# ---------------------------------------------------------------------------
# 谓词与文案渲染
# ---------------------------------------------------------------------------


def render_predicate(cond: ResolvedCondition) -> str:
    """把条件渲染成 SQL 谓词。区间一律左闭右开，与语义层 time_terms 一致。

    gte / lte 保留 <= 的直观语义，只有 between 用左闭右开——用户在表单里选
    「截至 6 月 30 日」时，6 月 30 日当天应当算在内。
    """
    ref = cond.field
    if cond.op == "eq":
        return f"{ref} = '{cond.values[0]}'" if cond.values else ""
    if cond.op == "in":
        listed = ", ".join(f"'{v}'" for v in cond.values)
        return f"{ref} IN ({listed})" if listed else ""
    if cond.op == "between":
        return f"{ref} >= '{cond.start}' AND {ref} < '{cond.end}'"
    if cond.op == "gte":
        return f"{ref} >= '{cond.start}'"
    return f"{ref} <= '{cond.end}'"


def value_options(catalog: Catalog, field: str) -> list[dict[str, str]]:
    """字段的可选取值，取自 tables.yaml 的 enum。

    前端表单的取值下拉直接用它，不在 TS 里另写一份——取值体系只有语义层一个
    来源，界面才不会给出库里没有的选项。没有 enum 的字段（日期类）返回空列表，
    前端按 values_from 改渲染日期选择器。
    """
    if "." not in field:
        return []
    table, column = field.split(".", 1)
    mapping = _enum_of(catalog, table, column)
    return [{"value": k, "label": v} for k, v in mapping.items()]


def describe_condition(cond: ResolvedCondition, catalog: Catalog | None = None) -> str:
    """条件的中文描述，用于提交前回显与渲染问句。"""
    labels = value_labels(catalog, cond)
    if cond.op == "eq":
        return f"{cond.display}为{labels[0]}" if labels else f"{cond.display}已限定"
    if cond.op == "in":
        return f"{cond.display}属于{'、'.join(labels)}" if labels else f"{cond.display}已限定"
    if cond.op == "between":
        return f"{cond.display}从 {cond.start} 到 {cond.end}"
    if cond.op == "gte":
        return f"{cond.display}不早于 {cond.start}"
    return f"{cond.display}不晚于 {cond.end}"


__all__ = [
    "MAX_CONDITIONS",
    "MAX_TOP_N",
    "ResolvedCondition",
    "TIME_FIELD",
    "describe_condition",
    "render_predicate",
    "resolve_conditions",
    "resolve_dimension_def",
    "resolve_metric_def",
    "validate_form_query",
    "value_labels",
    "value_options",
]
