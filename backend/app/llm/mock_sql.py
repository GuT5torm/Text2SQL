"""离线桩实现的 SQL 生成。

桩实现原先按关键词映射到写死的整条语句，其中只有成交总额与退款率两种聚合
表达式。问毛利、问订单量、问客单价、问成交用户数都会落到同一个成交总额模板上，
表现为「口径提示写毛利、结果列写成交总额、系统不报错」。

本模块改为从语义层派生：指标决定聚合表达式，维度决定分组列与 JOIN，两者都取自
metrics.yaml / tables.yaml。桩实现仍然是确定性的、不调用模型的，只是口径跟着
语义层走——语义层改了指标定义，桩实现自动跟上，两边不会各自漂移。

定位不变：它是「拿到密钥之前把链路跑通」与单元测试用的替身，产出不代表真实模型
能力，不要据此判断准确率。它不处理过滤值、不改写问法，这些留给真实模型。
"""

from __future__ import annotations

import re
from typing import Any

# 表名到短别名的映射。语义层指标 expression 的写法并不统一：成交总额写
# oi.unit_price，订单量却写 orders.id。拼装前统一成别名，随后按表达式里出现的
# 别名反推该 JOIN 哪几张表。
_ALIASES: dict[str, str] = {
    "orders": "o",
    "order_items": "oi",
    "products": "p",
    "categories": "c",
    "users": "u",
    "refunds": "r",
}

# 维度的分组列名。原模板用的是「大区」「渠道」这类短名，保持一致，
# 避免列名变化波及下钻与图表环节。
_DIM_LABELS: dict[str, str] = {
    "order_date": "日期",
    "order_month": "月份",
    "order_quarter": "季度",
    "region": "大区",
    "channel": "渠道",
    "category_name": "品类",
    "brand": "品牌",
    "user_level": "会员等级",
    "city": "城市",
    "refund_reason": "退款原因",
}

# 时间维度：排序方向与聚合维度相反，走势要按时间正序
_TIME_DIMS = {"order_date", "order_month", "order_quarter"}

_TOPN_RE = re.compile(r"(?:前|top)\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})", re.IGNORECASE)
_GREATER_WORDS = ("大于", "超过", "高于", "多于", "超出")
_LESS_WORDS = ("小于", "低于", "少于", "不足")
_RANK_WORDS = ("排名", "排行", "名次")

# 命中指标为零时的兜底：沿用改造前的默认形态（月度成交总额），
# 让「什么都没命中」这条路与既有用例保持一致。
_FALLBACK_SQL = """SELECT strftime('%Y-%m', o.created_at) AS 月份,
       SUM(oi.quantity * oi.unit_price) AS 成交总额
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
WHERE o.status NOT IN ('cancelled', 'pending')
GROUP BY 月份
ORDER BY 月份
LIMIT 1000"""


def _aliasize(expression: str) -> str:
    """把表达式里的表名换成短别名。"""
    out = expression
    for table, alias in _ALIASES.items():
        out = out.replace(f"{table}.", f"{alias}.")
    return out


def _aliases_in(expression: str) -> set[str]:
    return {alias for alias in _ALIASES.values() if f"{alias}." in expression}


def _join_clauses(aliases: set[str]) -> list[str]:
    """按出现的别名生成 JOIN 子句。

    依赖方向是单向的：品类依赖商品、商品依赖订单明细。只写了
    categories.name 而指标不涉及商品时，仍必须补出中间那两张表，
    否则 ON 子句里的别名无从属。
    """
    needed = set(aliases)
    if "c" in needed:
        needed |= {"p", "oi"}
    if "p" in needed:
        needed.add("oi")

    joins: list[str] = []
    if "oi" in needed:
        joins.append("JOIN order_items oi ON oi.order_id = o.id")
    if "p" in needed:
        joins.append("JOIN products p ON p.id = oi.product_id")
    if "c" in needed:
        joins.append("JOIN categories c ON c.id = p.category_id")
    if "u" in needed:
        joins.append("JOIN users u ON u.id = o.user_id")
    if "r" in needed:
        joins.append("LEFT JOIN refunds r ON r.order_id = o.id")
    return joins


def _phrases(entry: dict[str, Any]) -> list[str]:
    return [
        str(entry.get("display") or ""),
        str(entry.get("name") or ""),
        *[str(s) for s in (entry.get("synonyms") or [])],
    ]


def _best_metric(subject: str, catalog: Any) -> dict[str, Any] | None:
    """按指标声明的说法匹配，最长者优先。

    取最长而非先命中，是因为「销售额」与「销量」只差一字却指向不同指标，
    按定义顺序先命中的话，「各品类销售额」会被判成销量。
    """
    lowered = subject.lower()
    best: tuple[int, dict[str, Any]] | None = None
    for metric in catalog.metrics:
        for phrase in _phrases(metric):
            if phrase and phrase.lower() in lowered:
                if best is None or len(phrase) > best[0]:
                    best = (len(phrase), metric)
    return best[1] if best else None


def _matched_dimensions(subject: str, catalog: Any) -> list[dict[str, Any]]:
    """按维度声明的说法匹配，长词优先，最多取两个。

    长度下限设为 2：「下单月份」的词表里有单字「月」，而「上个月各区域的成交总额」
    含「月」——不设下限的话每个带月份的提问都会被额外按月份分组。

    未登记的说法天然匹配不上，此时不分组、按全量口径出数，这正是
    「本库无此维度」该有的表现，不需要在这里特判。
    """
    lowered = subject.lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for dim in catalog.dimensions:
        longest = max(
            (len(p) for p in _phrases(dim) if len(p) >= 2 and p.lower() in lowered),
            default=0,
        )
        if longest:
            scored.append((longest, dim))
    scored.sort(key=lambda item: -item[0])
    return [dim for _score, dim in scored[:2]]


_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def _parse_cn_number(text: str) -> int | None:
    """解析「十万」「一百万」这类中文数量词。"""
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= 10000:
                base = section + number or 1
                section = base * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        else:
            return None
    return total + section + number


def _threshold(subject: str) -> int | None:
    """从问句里取出比较阈值。

    桩实现不做过滤值解析，唯独阈值必须取：写死一档会让
    「退款率高于 0%」与「退款率高于 999999」得到同一个 HAVING，两条评测用例
    一真一假必须能区分开。
    """
    window = subject
    for words in (_GREATER_WORDS, _LESS_WORDS):
        for word in words:
            idx = subject.find(word)
            if idx >= 0:
                window = subject[idx + len(word) : idx + len(word) + 12]
                break
        else:
            continue
        break
    else:
        return None

    match = re.search(r"(\d+(?:\.\d+)?)", window)
    if match:
        return int(float(match.group(1)))
    match = re.search(r"([零一二两三四五六七八九十百千万亿]{1,8})", window)
    if match:
        return _parse_cn_number(match.group(1))
    return None


def _top_n(subject: str) -> int | None:
    match = _TOPN_RE.search(subject)
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    return _parse_cn_number(raw)


def build_sql(
    subject: str,
    catalog: Any,
    conditions: list[dict[str, Any]] | None = None,
    dimensions: list[str] | None = None,
) -> str:
    """按问句生成桩 SQL。subject 应当是问题原文，不含语义层上下文。

    conditions 与 dimensions 来自表单路径：那一路的指标与维度不写在问句里由
    关键词去猜，而是确定的。桩实现原本不写任何过滤条件，表单条件下会让强断言
    在桩模式里必然全红——那是桩的短板不是断言的失效，因此这里补上，让桩模式下
    也能验证「条件是否真的落进语句」。
    """
    metric = _best_metric(subject, catalog)
    if metric is None:
        return _FALLBACK_SQL

    dims = _resolve_dimensions(dimensions, catalog) if dimensions else _matched_dimensions(
        subject, catalog
    )

    expression = _aliasize(str(metric.get("expression") or ""))
    label = str(metric.get("display") or metric.get("name") or "值")

    select_parts: list[str] = []
    group_parts: list[str] = []
    for dim in dims:
        dim_expr = _aliasize(str(dim.get("expression") or ""))
        dim_label = _DIM_LABELS.get(str(dim.get("name")), str(dim.get("display") or ""))
        select_parts.append(f"{dim_expr} AS {dim_label}")
        group_parts.append(dim_label)

    select_parts.append(f"{expression} AS {label}")

    ranked = any(word in subject for word in _RANK_WORDS)
    if ranked:
        select_parts.append(f"RANK() OVER (ORDER BY {expression} DESC) AS 排名")

    aliases = _aliases_in(expression)
    for dim in dims:
        aliases |= _aliases_in(_aliasize(str(dim.get("expression") or "")))

    where_lines = ["WHERE o.status NOT IN ('cancelled', 'pending')"]
    for cond in conditions or []:
        clause = _render_condition(cond)
        if not clause:
            continue
        where_lines.append(clause)
        table = str(cond.get("field") or "").split(".", 1)[0]
        alias = _ALIASES.get(table)
        if alias:
            aliases.add(alias)

    lines = ["SELECT " + ",\n       ".join(select_parts), "FROM orders o"]
    lines.extend(_join_clauses(aliases))
    lines.append("\n  AND ".join(where_lines))
    if group_parts:
        lines.append("GROUP BY " + ", ".join(group_parts))

    greater = any(word in subject for word in _GREATER_WORDS)
    less = any(word in subject for word in _LESS_WORDS)
    threshold = _threshold(subject)
    if threshold is not None and (greater or less):
        lines.append(f"HAVING {label} {'>' if greater else '<'} {threshold}")

    if ranked:
        lines.append("ORDER BY 排名")
    elif dims and all(str(d.get("name")) in _TIME_DIMS for d in dims):
        lines.append("ORDER BY " + ", ".join(group_parts))
    elif less:
        lines.append(f"ORDER BY {label} ASC")
    else:
        lines.append(f"ORDER BY {label} DESC")

    top = _top_n(subject)
    lines.append(f"LIMIT {top if top else 1000}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 表单路径：回读提示词里的条件与维度
#
# 桩实现不接 state，只看提示词。表单条件与分组维度都渲染在 generate 提示词的
# 「必须满足的查询条件」一节里，这里从文本回读——与桩实现一贯的做法一致：它
# 假装自己读了提示词，实际按固定规则解析。
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"##\s*必须满足的查询条件.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
_PREDICATE_RE = re.compile(r"^\d+\.\s+`(.+?)`\s*$", re.MULTILINE)
# 分组维度行形如 `- 商品类目：`categories.name``；条件约束行与 JOIN 行都是
# 「反引号 + ：」开头，取不到「中文名：表达式」这一形态，不会误匹配。
_DIMENSION_RE = re.compile(r"^-\s*[^`]+：`([^`]+)`\s*$", re.MULTILINE)
_ATOM_RE = re.compile(r"([a-z_]+)\.([a-z_]+)\s*(>=|<=|=|>|<|IN)\s*(.+)", re.IGNORECASE)


def _resolve_dimensions(tokens: list[str], catalog: Any) -> list[dict[str, Any]]:
    """按配置名或表达式取维度定义，取不到的跳过。"""
    out: list[dict[str, Any]] = []
    for token in tokens:
        text = str(token).strip()
        if not text:
            continue
        dim = (getattr(catalog, "dimension_by_name", None) or {}).get(text)
        if dim is None:
            dim = next(
                (
                    d
                    for d in getattr(catalog, "dimensions", None) or []
                    if str(d.get("expression") or "").strip() == text
                ),
                None,
            )
        if dim is not None:
            out.append(dim)
    return out


def _render_condition(cond: dict[str, Any]) -> str:
    """把一条表单条件渲染成 SQL 谓词，表名换成短别名。"""
    field = _aliasize(str(cond.get("field") or ""))
    if "." not in field:
        return ""
    op = str(cond.get("op") or "")
    values = [str(v) for v in (cond.get("values") or []) if v]
    if op in ("eq", "in") and values:
        if len(values) == 1:
            return f"{field} = '{values[0]}'"
        return f"{field} IN (" + ", ".join(f"'{v}'" for v in values) + ")"
    start = str(cond.get("start") or "")
    end = str(cond.get("end") or "")
    if op == "between" and start and end:
        return f"{field} >= '{start}' AND {field} < '{end}'"
    if op == "gte" and start:
        return f"{field} >= '{start}'"
    if op == "lte" and end:
        return f"{field} <= '{end}'"
    return ""


def _parse_predicates(predicates: list[str]) -> list[dict[str, Any]]:
    """谓词文本 → 条件字典。同一字段的多个原子（>= 与 <）合并成一条 between。"""
    collected: dict[str, dict[str, Any]] = {}
    for predicate in predicates:
        for atom in re.split(r"\s+AND\s+", predicate, flags=re.IGNORECASE):
            match = _ATOM_RE.search(atom.strip())
            if match is None:
                continue
            table, column, op, rest = (
                match.group(1),
                match.group(2),
                match.group(3).upper(),
                match.group(4),
            )
            field = f"{table}.{column}"
            slot = collected.setdefault(
                field,
                {"field": field, "op": "", "values": [], "start": None, "end": None},
            )
            literals = re.findall(r"'([^']*)'", rest)
            if op in ("=", "IN"):
                slot["values"] = literals
                slot["op"] = "in" if op == "IN" or len(literals) > 1 else "eq"
            elif op in (">=", ">"):
                slot["start"] = literals[0] if literals else None
                slot["op"] = "between" if slot["end"] else "gte"
            else:
                slot["end"] = literals[0] if literals else None
                slot["op"] = "between" if slot["start"] else "lte"
    return [c for c in collected.values() if c["op"]]


def parse_form_block(prompt: str) -> tuple[list[dict[str, Any]], list[str]]:
    """从 generate 提示词回读表单条件与分组维度。非表单路径返回两个空列表。"""
    section = _SECTION_RE.search(prompt or "")
    if section is None:
        return [], []
    body = section.group(0)
    conditions = _parse_predicates(_PREDICATE_RE.findall(body))
    dimensions = [m.group(1).strip() for m in _DIMENSION_RE.finditer(body)]
    dimensions = [d for d in dimensions if "." in d]
    return conditions, dimensions
