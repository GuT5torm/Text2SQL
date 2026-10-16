"""槽位覆盖校验：route 抽出的槽位是否真的落到了 SQL 上。

背景：安全守卫拦的是「语句能不能执行」，口径签名拦的是「聚合表达式对不对」，
两道关卡都过了，语句仍然可能答的不是问的那件事——问华东各品类的成交总额，
少写一个 `region = '华东'`，签名照样匹配、结果照样有数、全程无报错。这是
答非所问里最难发现的一种，因为系统在用户看来一切正常。

route 已经把问题拆成了 {指标, 维度, 过滤, 时间, 排序, TopN} 六类槽位，但此前
只有前端拿它做标注，retrieve 与 generate 都不读。本模块拿槽位去比对真实 SQL
的 AST，把「问到了但没写进语句」的部分列出来，让这类偏差从不可检测变成可检测。

放在 agent/ 而不是 semantic/：语义层的分层约束禁止它引用 app.agent，而时间
边界的提取必须复用 consistency.extract_sql_time_range，重写一份会让两处对同一
件事给出不同答案。本模块比较的是「模型的理解」与「模型写出的语句」，属于编排
侧的一致性核对，与 consistency.py 同类。

只做比对，不改写、不阻断。是否阻断由 validate 节点决定，本模块对两者都成立。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# 五类可核对的槽位。指标不在此列：指标对不对由口径签名（semantic/expression_check.py）
# 负责，槽位里的指标是中文显示名，直接拿它跟 SQL 比对会与签名校验重复且更不准。
GAP_KINDS: tuple[str, ...] = ("dimension", "filter", "time_range", "top_n", "order_by")


@dataclass(frozen=True)
class SlotGap:
    """一处「问到了但语句里没有」的偏差。

    expected 是槽位的要求（给人看的中文），detail 说明语句实际是什么样。
    两者分开是因为修复时需要知道「缺什么」，核对时需要知道「为什么判缺」。
    """

    kind: str
    expected: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "expected": self.expected, "detail": self.detail}

    def __str__(self) -> str:  # pragma: no cover - 日志与提示用
        return f"{self.expected}：{self.detail}"


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def check_slot_coverage(
    sql: str,
    slots: dict[str, Any] | None,
    *,
    catalog: Any = None,
    today: dt.date | None = None,
) -> list[SlotGap]:
    """比对槽位与语句，返回覆盖不上的部分。

    解析失败或槽位为空时返回空列表：语句解析不了是守卫的事，槽位为空说明
    route 没有抽出结构化信息，两种情况下都不该由本模块给出一个结论。
    """
    if not slots or not (sql or "").strip():
        return []

    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception as exc:  # pragma: no cover - 守卫已先拦过一遍
        logger.debug("槽位覆盖校验跳过，语句无法解析：%s", exc)
        return []

    gaps: list[SlotGap] = []
    gaps.extend(_dimension_gaps(tree, slots, catalog))
    gaps.extend(_filter_gaps(tree, slots, catalog))
    gaps.extend(_time_gaps(sql, slots, catalog, today))
    gaps.extend(_top_n_gaps(tree, slots))
    gaps.extend(_order_gaps(tree, slots))
    return gaps


# ---------------------------------------------------------------------------
# 维度
# ---------------------------------------------------------------------------


def _ref_of(expression: str) -> tuple[str | None, str]:
    """把维度表达式拆成 (表名, 列名)。

    `categories.name` → ("categories", "name")；`strftime('%Y-%m', orders.created_at)`
    这类派生维度取它内部的列 → ("orders", "created_at")。拆不出来时表名留 None，
    调用方退化为只比列名。
    """
    text = (expression or "").strip()
    if not text:
        return None, ""
    try:
        node = sqlglot.parse_one(text, dialect="sqlite")
    except Exception:
        node = None
    if node is not None:
        column = node if isinstance(node, exp.Column) else node.find(exp.Column)
        if column is not None:
            return (column.table or None), (column.name or "")
    if "." in text:
        left, _, right = text.rpartition(".")
        return (left.strip() or None), right.strip()
    return None, text


def _dimension_index(catalog: Any) -> dict[str, tuple[str | None, str]]:
    """维度叫法 → (表名, 列名)。display / name / synonyms 全收，按长度降序命中。"""
    index: dict[str, tuple[str | None, str]] = {}
    if catalog is None:
        return index
    for dim in getattr(catalog, "dimensions", None) or []:
        ref = _ref_of(str(dim.get("expression") or ""))
        if not ref[1]:
            continue
        for alias in [dim.get("display"), dim.get("name"), *(dim.get("synonyms") or [])]:
            key = _norm(str(alias or ""))
            if key:
                index[key] = ref
    return index


def _lookup_dimension(token: str, index: dict[str, tuple[str | None, str]]) -> tuple[str | None, str] | None:
    """槽位里的维度叫法可能带口语尾巴（「各区域」），先整体命中再退回包含匹配。"""
    key = _norm(token)
    if not key:
        return None
    if key in index:
        return index[key]
    for alias, ref in sorted(index.items(), key=lambda kv: -len(kv[0])):
        if len(alias) >= 2 and (alias in key or key in alias):
            return ref
    return None


def _alias_map(tree: exp.Expression) -> dict[str, str]:
    """别名 → 表名。SQL 里普遍写 `FROM orders o`，不解析别名就认不出 o.region。"""
    mapping: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue
        alias = (table.alias_or_name or name).lower()
        mapping[alias] = name
    return mapping


def _analysis_select(tree: exp.Expression) -> exp.Expression:
    """承载分组与排序的那个 SELECT。

    守卫在语句缺 LIMIT 时会套一层 `SELECT * FROM (...) LIMIT 1000`，此时分组与
    排序都在内层。只盯着最外层会把「写了分组但被包了一层」误判成没写。
    """
    selects: list[exp.Expression] = [tree] if isinstance(tree, exp.Select) else []
    selects.extend(tree.find_all(exp.Select))
    for select in selects:
        if select.args.get("group") or select.args.get("order"):
            return select
    return tree


def _projection_aliases(select: exp.Expression) -> dict[str, set[tuple[str | None, str]]]:
    """SELECT 别名 → 它背后的列。

    SQLite 允许 `GROUP BY 品类`，也就是按输出别名分组——桩实现与模型都这么写。
    不解析别名就会把「按品类分组」判成「没按品类分组」，而这类误报一旦发生，
    后续所有基于它的判断都不可信。
    """
    aliases = _alias_map(select)
    out: dict[str, set[tuple[str | None, str]]] = {}
    for projection in select.args.get("expressions") or []:
        if not isinstance(projection, exp.Alias):
            continue
        key = (projection.alias_or_name or "").lower()
        if not key:
            continue
        refs: set[tuple[str | None, str]] = set()
        for column in projection.this.find_all(exp.Column):
            table = (column.table or "").lower()
            if table:
                table = aliases.get(table, table)
            refs.add((table or None, (column.name or "").lower()))
        if refs:
            out[key] = refs
    return out


def _group_refs(tree: exp.Expression) -> tuple[set[tuple[str | None, str]], bool]:
    """语句实际分组用到的列，以及是否真的有 GROUP BY。

    没有 GROUP BY 时退回 SELECT 投影：问「各区域」却只 select 了 region 没分组的
    语句虽然不对，但那是另一种错误（分组缺失），本模块只判断「这个维度有没有进
    语句」，免得一条语句被两处各判一次。
    """
    aliases = _alias_map(tree)
    select = _analysis_select(tree)
    group = select.args.get("group")
    projection_aliases = _projection_aliases(select)
    if group is not None:
        nodes: list[exp.Expression] = list(group.expressions or [])
        has_group = True
    else:
        nodes = list(select.args.get("expressions") or [])
        has_group = False
    refs: set[tuple[str | None, str]] = set()
    for node in nodes:
        bare = None
        if isinstance(node, exp.Column) and not node.table:
            bare = (node.name or "").lower()
        if bare and bare in projection_aliases:
            refs |= projection_aliases[bare]
            continue
        for column in node.find_all(exp.Column):
            table = (column.table or "").lower()
            if table:
                table = aliases.get(table, table)
            refs.add((table or None, (column.name or "").lower()))
    return refs, has_group


def _filtered_columns(slots: dict[str, Any], catalog: Any) -> set[str]:
    """已被过滤条件限定到单一取值的列。

    用于排除冗余分组诉求：「华东区各品类的成交总额」里区域既是过滤条件又是
    维度槽位，而过滤到单个取值后再按它分组只会有唯一一组，不分组才是正常的
    写法。把这种情况算成缺失，会让一句完全正确的问句背上一条假告警。
    """
    index = _dimension_index(catalog)
    value_synonyms = list(getattr(catalog, "value_synonyms", None) or []) if catalog else []
    columns: set[str] = set()
    for raw in slots.get("filters") or []:
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("value") or "").strip()
        if not value:
            continue
        for vs in value_synonyms:
            if _norm(str(vs.get("term") or "")) == _norm(value):
                field = str(vs.get("field") or "").strip().lower()
                if field:
                    columns.add(field.rpartition(".")[2])
        field = str(raw.get("field") or "").strip()
        resolved = _lookup_dimension(field, index) if field else None
        if resolved:
            columns.add(resolved[1].lower())
    return columns


def _dimension_gaps(tree: exp.Expression, slots: dict[str, Any], catalog: Any) -> list[SlotGap]:
    index = _dimension_index(catalog)
    if not index:
        return []
    refs, has_group = _group_refs(tree)
    filtered = _filtered_columns(slots, catalog)
    gaps: list[SlotGap] = []
    for raw in slots.get("dimensions") or []:
        token = str(raw).strip()
        if not token:
            continue
        resolved = _lookup_dimension(token, index)
        if resolved is None:
            # 槽位里的叫法语义层没有对应维度，交给未收录维度检测去提示，
            # 这里不猜，免得把「支付方式」硬配到 channel 上还判它已覆盖。
            continue
        if resolved[1].lower() in filtered:
            continue
        if _ref_covered(resolved, refs):
            continue
        where = "GROUP BY 里没有" if has_group else "语句没有 GROUP BY，投影里也没有"
        gaps.append(
            SlotGap(
                kind="dimension",
                expected=f"按「{token}」分组",
                detail=f"{where} {resolved[0] + '.' if resolved[0] else ''}{resolved[1]}"
                + (f"，实际分组字段为 {_describe_refs(refs)}" if has_group else ""),
            )
        )
    return gaps


def _ref_covered(expected: tuple[str | None, str], refs: set[tuple[str | None, str]]) -> bool:
    """表名与列名都对上才算覆盖；任一侧缺表名时退化为只比列名。"""
    exp_table, exp_column = expected
    exp_column = exp_column.lower()
    exp_table = (exp_table or "").lower() or None
    for table, column in refs:
        if column != exp_column:
            continue
        if exp_table is None or table is None or table == exp_table:
            return True
    return False


def _describe_refs(refs: set[tuple[str | None, str]]) -> str:
    if not refs:
        return "无"
    return "、".join(sorted(f"{t + '.' if t else ''}{c}" for t, c in refs))[:80]


# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------


def _predicates(tree: exp.Expression) -> list[exp.Expression]:
    """WHERE 与 HAVING 的条件节点。数值阈值过滤落在 HAVING，两类都要看。"""
    nodes: list[exp.Expression] = []
    for where in tree.find_all(exp.Where):
        nodes.append(where)
    having_cls = getattr(exp, "Having", None)
    if having_cls is not None:
        for having in tree.find_all(having_cls):
            nodes.append(having)
    return nodes


def _literal_values(node: exp.Expression) -> set[str]:
    return {
        str(lit.this).strip()
        for lit in node.find_all(exp.Literal)
        if isinstance(lit.this, str)
    }


def _predicate_refs(node: exp.Expression, aliases: dict[str, str]) -> set[str]:
    """条件里出现的列名，用于判断取值是过滤在哪个字段上。"""
    out: set[str] = set()
    for column in node.find_all(exp.Column):
        table = (column.table or "").lower()
        if table:
            table = aliases.get(table, table)
        out.add(f"{table + '.' if table else ''}{column.name.lower()}")
    return out


def _filter_gaps(tree: exp.Expression, slots: dict[str, Any], catalog: Any) -> list[SlotGap]:
    predicates = _predicates(tree)
    aliases = _alias_map(tree)
    index = _dimension_index(catalog)
    value_synonyms = list((getattr(catalog, "value_synonyms", None) or [])) if catalog else []

    gaps: list[SlotGap] = []
    for raw in slots.get("filters") or []:
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field") or "").strip()
        value = str(raw.get("value") or "").strip()
        if not value:
            continue

        candidates = {value}
        for vs in value_synonyms:
            if _norm(str(vs.get("term") or "")) == _norm(value):
                candidates.add(str(vs.get("value") or ""))
        candidates = {c for c in candidates if c}

        expected_columns: set[str] = set()
        for vs in value_synonyms:
            if str(vs.get("value") or "") in candidates:
                expected_columns.add(str(vs.get("field") or "").lower())
        resolved = _lookup_dimension(field, index) if field else None
        if resolved is not None:
            expected_columns.add(f"{resolved[0] + '.' if resolved[0] else ''}{resolved[1].lower()}")

        if not predicates:
            gaps.append(
                SlotGap(
                    kind="filter",
                    expected=f"限定「{field or '该字段'}」为 {value}",
                    detail="语句里没有 WHERE / HAVING 条件",
                )
            )
            continue

        hit = False
        for node in predicates:
            values = _literal_values(node)
            if not (values & candidates):
                continue
            if not expected_columns:
                hit = True
                break
            refs = _predicate_refs(node, aliases)
            # 取值过滤通常写成 region = '华东'，条件里带上别的列也正常，
            # 只要目标字段出现在同一条件里就算过滤在它身上
            if any(any(c and c in r for c in expected_columns) for r in refs):
                hit = True
                break
        if not hit:
            gaps.append(
                SlotGap(
                    kind="filter",
                    expected=f"限定「{field or '该字段'}」为 {value}",
                    detail="语句的条件里没有这个取值" if expected_columns else "语句的条件里没有对应字段",
                )
            )
    return gaps


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


def _time_gaps(
    sql: str, slots: dict[str, Any], catalog: Any, today: dt.date | None
) -> list[SlotGap]:
    phrase = str(slots.get("time_range") or "").strip()
    if not phrase or catalog is None:
        return []

    # 两处导入放在函数里：它们只在这条分支用到，而本模块更常见的调用场景
    # （维度 / TopN / 排序核对）不必为此承担导入开销
    from app.agent.consistency import extract_sql_time_range
    from app.semantic.loader import resolve_time_term

    terms = list(getattr(catalog, "time_terms", None) or [])
    matched = None
    key = _norm(phrase)
    for term in sorted(terms, key=lambda t: -len(_norm(str(t.get("term") or "")))):
        label = _norm(str(term.get("term") or ""))
        if label and (label == key or label in key or key in label):
            matched = term
            break
    if matched is None:
        return []

    try:
        resolved = resolve_time_term(matched, today)
    except Exception as exc:  # pragma: no cover - 配置新增了未实现的 kind
        logger.debug("时间槽位换算失败，跳过核对：%s", exc)
        return []

    from app.agent.consistency import extract_sql_time_range

    sql_lo, sql_hi = extract_sql_time_range(sql)
    if sql_lo is None or sql_hi is None:
        return [
            SlotGap(
                kind="time_range",
                expected=f"统计区间为{resolved.label}（{resolved.resolved}）",
                detail="语句里没有可识别的时间过滤条件",
            )
        ]
    slot_lo = dt.date.fromisoformat(resolved.start)
    slot_hi = dt.date.fromisoformat(resolved.end)
    if sql_hi <= slot_lo or sql_lo >= slot_hi:
        return [
            SlotGap(
                kind="time_range",
                expected=f"统计区间为{resolved.label}（{resolved.resolved}）",
                detail=f"语句实际统计 {sql_lo.isoformat()} 至 {(sql_hi - dt.timedelta(days=1)).isoformat()}",
            )
        ]
    # 部分重叠不在此判缺：区间是否完全落在召回区间内是 FR-D-5 的事
    # （consistency.check_time_range_consistency），同一件事不判两次。
    return []


# ---------------------------------------------------------------------------
# TopN 与排序
# ---------------------------------------------------------------------------


def _limit_values(tree: exp.Expression) -> set[int]:
    """语句里全部 LIMIT 取值，含守卫包裹后的外层。

    守卫在语句缺 LIMIT 时会套一层 `SELECT * FROM (...) LIMIT 1000`，此时模型
    写的 LIMIT 5 在内层。只取顶层会把「写了 TopN 但守卫加了外层」误判成缺失。
    """
    values: set[int] = set()
    selects = [tree] if isinstance(tree, exp.Select) else []
    selects.extend(tree.find_all(exp.Select))
    for select in selects:
        limit = select.args.get("limit")
        if limit is None:
            continue
        for lit in limit.find_all(exp.Literal):
            try:
                values.add(int(str(lit.this)))
            except (TypeError, ValueError):
                continue
    return values


def _top_n_gaps(tree: exp.Expression, slots: dict[str, Any]) -> list[SlotGap]:
    raw = slots.get("top_n")
    try:
        top_n = int(str(raw).strip())
    except (TypeError, ValueError):
        return []
    if top_n <= 0:
        return []
    if top_n in _limit_values(tree):
        return []
    return [
        SlotGap(
            kind="top_n",
            expected=f"只取前 {top_n} 条",
            detail="语句的 LIMIT 不是这个条数" if _limit_values(tree) else "语句没有 LIMIT",
        )
    ]


def _order_gaps(tree: exp.Expression, slots: dict[str, Any]) -> list[SlotGap]:
    raw = slots.get("order_by")
    if not isinstance(raw, dict):
        return []
    direction = str(raw.get("direction") or "").strip().lower()
    if direction not in ("asc", "desc"):
        return []

    order = _analysis_select(tree).args.get("order")
    if order is None:
        return [
            SlotGap(
                kind="order_by",
                expected=f"按「{raw.get('field') or '度量'}」{'降序' if direction == 'desc' else '升序'}排列",
                detail="语句没有 ORDER BY",
            )
        ]
    flags = [bool(o.args.get("desc")) for o in (order.expressions or [])]
    # 未显式写 ASC/DESC 时 sqlglot 的 desc 为 False，即默认升序，与 asc 一致
    ok = any(flags) if direction == "desc" else any(not f for f in flags)
    if ok:
        return []
    return [
        SlotGap(
            kind="order_by",
            expected=f"按「{raw.get('field') or '度量'}」{'降序' if direction == 'desc' else '升序'}排列",
            detail="语句的排序方向相反" if flags else "语句没有 ORDER BY",
        )
    ]


# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """与语义层同一套归一化：去空白与标点、转小写。

    从 loader 直接导入会带出整个语义层，这里只用到这一个规则，重复三行比
    多一层依赖更划算；两侧规则若不一致，症状是维度叫法匹配不上。
    """
    text = re.sub(r"[\s\u3000]+", "", text or "")
    text = re.sub(r"[，。？！、；：,\.\?!;:'\"（）\(\)\[\]【】]", "", text)
    return text.lower()


__all__ = ["GAP_KINDS", "SlotGap", "check_slot_coverage"]
