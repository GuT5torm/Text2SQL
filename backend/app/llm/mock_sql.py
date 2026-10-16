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


def build_sql(subject: str, catalog: Any) -> str:
    """按问句生成桩 SQL。subject 应当是问题原文，不含语义层上下文。"""
    metric = _best_metric(subject, catalog)
    if metric is None:
        return _FALLBACK_SQL

    dims = _matched_dimensions(subject, catalog)

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

    lines = ["SELECT " + ",\n       ".join(select_parts), "FROM orders o"]
    lines.extend(_join_clauses(aliases))
    lines.append("WHERE o.status NOT IN ('cancelled', 'pending')")
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
