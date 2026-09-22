"""FR-B-3 结论数字可回溯 + FR-D-5 SQL 时间区间一致性核对。

两个核对器都是纯函数：不调模型、不查库，单轮耗时微秒级（NFR ≤ 50ms）。

FR-B-3 的判定严格按 FRD 五条：
  1. 抽 token 时处理千分位、百分号、货币符号、万 / 亿；
  2. 与结果集数值单元格比，相对误差 ≤ 1% 或绝对误差 ≤ 0.01，
     百分比与小数视为同量纲（14.2% 同时按 14.2 与 0.142 匹配）；
  3. 白名单：序数（第 1 名 / 前三 / TOP3）、row_count、query_elapsed_ms、
     时间范围起止日期、findings 值、baselines 值；
  4. 未命中的数字进「待查」清单随轮次返回，不改正文一个字；
  5. 现有评测用例零误报。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.agent.baselines import TIME_COLUMN_NAMES

# ---------------------------------------------------------------------------
# FR-B-3：数字抽取与核对
# ---------------------------------------------------------------------------

# 数字核心：千分位整数 / 普通整数 / 小数
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"

# 抽取主正则：百分比 / 万亿 / 货币 / 裸数字，按优先级排列
_TOKEN_RE = re.compile(
    rf"(?P<pct>{_NUM})\s*(?:个百分点|[％%])"
    rf"|(?P<wan>{_NUM})\s*(?:万亿|亿|万)"
    rf"|(?:[¥￥$]\s*)(?P<money>{_NUM})(?:\s*元)?"
    rf"|(?P<yuan>{_NUM})\s*元"
    rf"|(?P<num>{_NUM})"
)

# 不参与核对的文本片段：日期、时刻、序数。核对前把这些区间整体替换成空格，
# 避免「2026 年」里的 2026、「第 1 名」里的 1 被当成结论数字。
_EXCLUDE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d{4}-\d{1,2}(?:-\d{1,2})?"),                       # 2026-08-01
    re.compile(r"\d{4}\s*年(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?)?"),    # 2026 年 8 月 1 日
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日?"),                     # 8 月 1 日
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),                         # 12:30[:00]
    re.compile(r"第\s*[0-9一二三四五六七八九十百千]+\s*[名次位期]?"),  # 第 1 名
    re.compile(r"前\s*[0-9一二三四五六七八九十百千]+\s*[大名次位]"),   # 前三 / 前 10 大
    re.compile(r"(?:TOP|Top|top)\s*\d+"),                            # TOP3
)

_REL_TOL = 0.01
_ABS_TOL = 0.01


def _to_float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _mask_excluded(text: str) -> str:
    for pattern in _EXCLUDE_RES:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _token_candidates(match: re.Match[str]) -> tuple[str, list[float]]:
    """把一个 token 归一成全部可能量纲。

    14.2% → [0.142, 14.2]：结果集里比率可能存 0.1428，也可能已经乘了 100。
    1.2万 → 12000；3.5 亿 → 350000000。
    """
    kind = match.lastgroup
    raw = match.group(kind)
    value = _to_float(raw)
    if value is None:
        return match.group(0), []
    if kind == "pct":
        return match.group(0), [value / 100.0, value]
    if kind == "wan":
        tail = match.group(0)[len(raw):].strip()
        multiplier = {"万": 1e4, "亿": 1e8, "万亿": 1e12}[tail]
        return match.group(0), [value * multiplier]
    return match.group(0), [value]


def _numeric_cells(rows: list[Any]) -> set[float]:
    """结果集中的全部数值单元格（dict / list 行都认）。"""
    values: set[float] = set()
    for row in rows or []:
        items = row.values() if isinstance(row, dict) else (row if isinstance(row, (list, tuple)) else [])
        for cell in items:
            if isinstance(cell, bool):
                continue
            if isinstance(cell, (int, float)):
                values.add(float(cell))
            elif isinstance(cell, str):
                parsed = _to_float(cell.strip())
                # 含星号的是脱敏值，不能当数字；纯数字字符串才收
                if parsed is not None and "*" not in cell:
                    values.add(parsed)
    return values


def _whitelist_values(
    *,
    rows: list[Any],
    row_count: int | None,
    elapsed_ms: float | None,
    time_range: dict[str, Any] | None,
    findings: list[dict[str, Any]] | None,
    baselines: dict[str, Any] | None,
    retry_count: int | None = None,
) -> set[float]:
    """FRD 白名单：不要求能在结果集单元格里找到的合法数字。"""
    allowed = _numeric_cells(rows)

    if row_count is not None:
        allowed.add(float(row_count))
    if elapsed_ms is not None:
        allowed.add(float(elapsed_ms))
        allowed.add(round(float(elapsed_ms), 1))
    # FR-F-2：结论里会出现「经过 N 次修正」这类服务端事实，与 row_count 同类，
    # 不是模型编的数字。不放进白名单会让每条修正过的轮次都报一次「待查」。
    if retry_count is not None:
        allowed.add(float(retry_count))

    # 时间范围起止日期：年 / 月 / 日各分量都可能出现在结论里
    for endpoint in ("start", "end"):
        raw = (time_range or {}).get(endpoint)
        if not raw:
            continue
        try:
            d = dt.date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        allowed.update({float(d.year), float(d.month), float(d.day)})

    # findings：value / baseline / deviation（比率同时按小数与百分数收）
    for f in findings or []:
        for field_name in ("value", "baseline"):
            v = f.get(field_name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                allowed.add(float(v))
        deviation = f.get("deviation")
        if isinstance(deviation, (int, float)) and not isinstance(deviation, bool):
            allowed.update({float(deviation), float(deviation) * 100.0})

    # baselines：本期值、环比 / 同比值与偏离度
    baselines = baselines or {}
    current = baselines.get("current_value")
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        allowed.add(float(current))
    for period in ("mom", "yoy"):
        item = baselines.get(period) or {}
        for sub in ("value",):
            v = item.get(sub)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                allowed.add(float(v))
        deviation = item.get("deviation")
        if isinstance(deviation, (int, float)) and not isinstance(deviation, bool):
            allowed.update({float(deviation), float(deviation) * 100.0})

    return allowed


def _matches(value: float, allowed: set[float]) -> bool:
    """相对误差 ≤ 1%，或对零附近绝对误差 ≤ 0.01。"""
    for candidate in allowed:
        if abs(value - candidate) <= _ABS_TOL:
            return True
        if candidate != 0 and abs(value - candidate) / abs(candidate) <= _REL_TOL:
            return True
    return False


def check_conclusion_numbers(
    answer: str | None,
    rows: list[Any],
    *,
    row_count: int | None = None,
    elapsed_ms: float | None = None,
    time_range: dict[str, Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
    baselines: dict[str, Any] | None = None,
    retry_count: int | None = None,
) -> list[dict[str, Any]]:
    """返回结论里未能回溯的数字清单（FR-B-3「待查」）。

    每项形如 {"text": "1,234", "value": 1234.0}。能核对上的数字不返回；
    调用方只负责在界面中性提示，绝不能据此改写结论正文。
    """
    if not answer:
        return []
    allowed = _whitelist_values(
        rows=rows,
        row_count=row_count,
        elapsed_ms=elapsed_ms,
        time_range=time_range,
        findings=findings,
        baselines=baselines,
        retry_count=retry_count,
    )
    unverified: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    cleaned = _mask_excluded(answer)
    for match in _TOKEN_RE.finditer(cleaned):
        token, candidates = _token_candidates(match)
        if not candidates:
            continue
        token_key = token.strip()
        if token_key in seen_texts:
            continue
        if not any(_matches(v, allowed) for v in candidates):
            seen_texts.add(token_key)
            unverified.append({"text": token_key, "value": candidates[0]})
    return unverified


# ---------------------------------------------------------------------------
# FR-D-5：SQL 时间区间与召回 time_range 的一致性
# ---------------------------------------------------------------------------

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_day(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _month_start(text: str) -> dt.date | None:
    m = _MONTH_RE.match(text.strip()[:7])
    if not m:
        return None
    return dt.date(int(m.group(1)), int(m.group(2)), 1)


def _next_month(day: dt.date) -> dt.date:
    return dt.date(day.year + (day.month // 12), day.month % 12 + 1, 1)


def _time_column(node: exp.Expression) -> exp.Column | None:
    """找比较表达式里唯一的时间列（可能裹在 date()/strftime()/CAST 里）。"""
    columns = [
        c for c in node.find_all(exp.Column) if (c.name or "").lower() in TIME_COLUMN_NAMES
    ]
    names = {c.name.lower() for c in columns}
    return columns[0] if len(names) == 1 else None


def _strftime_fmt(container: exp.Expression, column: exp.Column) -> str | None:
    """若时间列被 strftime(fmt, col) 包裹，取出 fmt 字面量；否则返回 None。

    SQLite 方言下 sqlglot 会把 strftime 表达成 TimeToStr（format 参数），
    匿名函数形式只在部分版本出现，两种都认。
    """
    for node in container.find_all(exp.TimeToStr):
        if column in list(node.find_all(exp.Column)):
            fmt = node.args.get("format")
            if isinstance(fmt, exp.Literal):
                return str(fmt.this)
    for anon in container.find_all(exp.Anonymous):
        if (anon.name or "").lower() != "strftime":
            continue
        if column not in list(anon.find_all(exp.Column)):
            continue
        first = anon.this
        if isinstance(first, exp.Literal):
            return str(first.this)
    return None


def _literal_at(expression: exp.Expression, column: exp.Column) -> exp.Literal | None:
    """取比较式另一侧的字面量（排除时间列自身这侧）。"""
    for side in (expression.left, expression.right):
        if column in list(side.find_all(exp.Column)):
            continue
        for lit in side.find_all(exp.Literal):
            return lit
    return None


def _apply_bound(
    bounds: dict[str, dt.date | None],
    op: type[exp.Expression],
    raw: str,
    fmt: str | None,
) -> None:
    """把一个比较折叠成 [lo, hi) 上的一条边界。AND 条件取交集：lo 取大、hi 取小。"""
    day: dt.date | None = None
    is_month = False
    if fmt and "%Y-%m" in fmt and "%d" not in fmt:
        day = _month_start(raw)
        is_month = day is not None
    if day is None:
        day = _parse_day(raw)
    if day is None:
        return

    if op is exp.GTE:
        candidate = day
        side = "lo"
    elif op is exp.GT:
        candidate = _next_month(day) if is_month else day + dt.timedelta(days=1)
        side = "lo"
    elif op is exp.LT:
        candidate = day
        side = "hi"
    else:  # exp.LTE
        candidate = _next_month(day) if is_month else day + dt.timedelta(days=1)
        side = "hi"

    current = bounds[side]
    if current is None:
        bounds[side] = candidate
    elif side == "lo":
        bounds[side] = max(current, candidate)
    else:
        bounds[side] = min(current, candidate)


def extract_sql_time_range(sql: str) -> tuple[dt.date | None, dt.date | None]:
    """从 SQL 的 WHERE 提取实际生效的时间区间（左闭右开）。

    支持 date 字面量比较与 strftime('%Y-%m', col) 月串比较；识别不出来的
    写法返回 (None, None)，调用方据此判「语句没有可识别的时间条件」。
    """
    try:
        tree = sqlglot.parse_one(sql or "", dialect="sqlite")
    except Exception:
        return None, None

    bounds: dict[str, dt.date | None] = {"lo": None, "hi": None}

    # BETWEEN 不是 exp.Binary 的子类，需要单独遍历。
    for between in tree.find_all(exp.Between):
        column = _time_column(between)
        if column is None:
            continue
        low_lit = between.args.get("low")
        high_lit = between.args.get("high")
        if not isinstance(low_lit, exp.Literal) or not isinstance(high_lit, exp.Literal):
            continue
        lo = _parse_day(str(low_lit.this))
        hi = _parse_day(str(high_lit.this))
        if lo and hi:
            if bounds["lo"] is None or lo > bounds["lo"]:
                bounds["lo"] = lo
            hi_excl = hi + dt.timedelta(days=1)
            if bounds["hi"] is None or hi_excl < bounds["hi"]:
                bounds["hi"] = hi_excl

    for cmp in tree.find_all(exp.Binary):
        if not isinstance(cmp, (exp.GTE, exp.GT, exp.LT, exp.LTE)):
            continue
        column = _time_column(cmp)
        if column is None:
            continue
        lit = _literal_at(cmp, column)
        if lit is None or not isinstance(lit.this, str):
            continue
        fmt = _strftime_fmt(cmp, column)
        _apply_bound(bounds, type(cmp), lit.this, fmt)

    return bounds["lo"], bounds["hi"]


def check_time_range_consistency(
    sql: str, retrieved_time_range: dict[str, Any] | None
) -> bool | None:
    """FR-D-5：SQL WHERE 时间区间是否等于或落在召回 time_range 之内。

    召回没有 time_range 时不核对，返回 None；SQL 完全没有可识别时间条件
    （或缺一边）而召回有区间，一律判不一致——这是 FRD 要求 100% 识别的
    高频问题（桩模板漏写时间过滤正是此情形）。
    """
    if not retrieved_time_range:
        return None
    start = _parse_day(str(retrieved_time_range.get("start") or ""))
    end = _parse_day(str(retrieved_time_range.get("end") or ""))
    if start is None or end is None:
        return None

    sql_lo, sql_hi = extract_sql_time_range(sql)
    if sql_lo is None or sql_hi is None:
        return False
    return sql_lo >= start and sql_hi <= end


__all__ = [
    "check_conclusion_numbers",
    "check_time_range_consistency",
    "extract_sql_time_range",
]
