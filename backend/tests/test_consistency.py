"""FR-B-3 结论数字核对 与 FR-D-5 时间区间一致性的纯函数用例。"""

from __future__ import annotations

import datetime as dt

from app.agent.consistency import (
    check_conclusion_numbers,
    check_time_range_consistency,
    extract_sql_time_range,
)

TR = {"start": "2026-08-01", "end": "2026-09-01", "label": "上个月", "resolved": "2026-08-01 至 2026-08-31"}


def _pending_texts(answer: str, **kwargs: object) -> list[str]:
    rows = kwargs.pop("rows", [])
    return [
        item["text"]
        for item in check_conclusion_numbers(answer, rows, **kwargs)  # type: ignore[arg-type]
    ]


# ---------------------------------------------------------------------------
# FR-B-3：容差与量纲
# ---------------------------------------------------------------------------


def test_percent_shares_dimension_with_decimal_ratio() -> None:
    # 结果集里比率存 0.1428，结论写 14.2%，两个量纲都要能对上
    assert _pending_texts("环比下降 14.2%", rows=[{"v": 0.1428}]) == []


def test_relative_tolerance_boundary() -> None:
    assert _pending_texts("数值为 100.5", rows=[{"v": 100}]) == []  # 0.5% 在 1% 内
    pending = _pending_texts("数值为 102", rows=[{"v": 100}])
    assert pending == ["102"]  # 2% 超出容差


def test_absolute_tolerance_near_zero() -> None:
    assert _pending_texts("比率 0.005", rows=[{"v": 0.0}]) == []


def test_wan_and_yi_multipliers() -> None:
    assert _pending_texts("成交额 1.2万", rows=[{"v": 12000}]) == []
    assert _pending_texts("成交额 3.5亿", rows=[{"v": 350000000}]) == []
    assert _pending_texts("成交额 1.2万亿", rows=[{"v": 1.2e12}]) == []


def test_thousands_separator() -> None:
    assert _pending_texts("销量 1,234 件", rows=[{"v": 1234}]) == []


def test_currency_and_yuan_tokens() -> None:
    assert _pending_texts("客单价 ¥199.5", rows=[{"v": 199.5}]) == []
    assert _pending_texts("成本 12元", rows=[{"v": 12}]) == []


def test_row_count_and_elapsed_whitelisted() -> None:
    answer = "查询返回 7 行结果，耗时 23.5 ms"
    assert _pending_texts(answer, rows=[], row_count=7, elapsed_ms=23.5) == []


def test_findings_and_baselines_whitelisted() -> None:
    answer = "华东为 12345.0，环比偏离 8.3%"
    findings = [{"value": 12345.0, "baseline": 11400.0, "deviation": 0.083}]
    baselines = {"current_value": 12345.0, "mom": {"value": 11400.0, "deviation": 0.083}}
    assert _pending_texts(answer, rows=[], findings=findings, baselines=baselines) == []


def test_external_number_is_flagged_once() -> None:
    # 99999 在任何白名单里都找不到；同文出现两次只登记一次
    pending = _pending_texts("行业均值 99999，对照值也是 99999", rows=[{"v": 12.3}])
    assert pending == ["99999"]


def test_dates_and_ordinals_excluded() -> None:
    answer = "2026 年 8 月表现回顾：华东排第 1 名，华南进入前三，TOP3 门槛见下表"
    assert _pending_texts(answer, rows=[], time_range=TR) == []


def test_masked_cells_are_not_numeric() -> None:
    # 脱敏后的字符串不能被当成可核对数字
    assert _pending_texts("联系电话 13800001234", rows=[{"phone": "138****1234"}]) == ["13800001234"]


# ---------------------------------------------------------------------------
# FR-D-5：时间区间五形态
# ---------------------------------------------------------------------------


def test_time_range_gte_lt_day_literals_match() -> None:
    sql = (
        "SELECT * FROM orders WHERE created_at >= '2026-08-01' "
        "AND created_at < '2026-09-01' LIMIT 100"
    )
    assert extract_sql_time_range(sql) == (dt.date(2026, 8, 1), dt.date(2026, 9, 1))
    assert check_time_range_consistency(sql, TR) is True


def test_time_range_between_inclusive_folded() -> None:
    sql = (
        "SELECT * FROM orders WHERE created_at BETWEEN '2026-08-01' "
        "AND '2026-08-31' LIMIT 100"
    )
    assert extract_sql_time_range(sql) == (dt.date(2026, 8, 1), dt.date(2026, 9, 1))
    assert check_time_range_consistency(sql, TR) is True


def test_time_range_strftime_month_folding() -> None:
    # strftime('%Y-%m') 的 >= '2026-08' 与 < '2026-09' 要按整月折叠
    sql = (
        "SELECT strftime('%Y-%m', created_at) AS m FROM orders "
        "WHERE strftime('%Y-%m', created_at) >= '2026-08' "
        "AND strftime('%Y-%m', created_at) < '2026-09' LIMIT 100"
    )
    assert extract_sql_time_range(sql) == (dt.date(2026, 8, 1), dt.date(2026, 9, 1))
    assert check_time_range_consistency(sql, TR) is True


def test_missing_time_predicate_is_inconsistent() -> None:
    sql = "SELECT region, COUNT(*) FROM orders GROUP BY region LIMIT 100"
    assert extract_sql_time_range(sql) == (None, None)
    assert check_time_range_consistency(sql, TR) is False


def test_single_sided_predicate_is_inconsistent() -> None:
    sql = "SELECT * FROM orders WHERE created_at >= '2026-08-01' LIMIT 100"
    assert check_time_range_consistency(sql, TR) is False


def test_sql_range_wider_than_retrieved_is_inconsistent() -> None:
    sql = (
        "SELECT * FROM orders WHERE created_at >= '2026-07-01' "
        "AND created_at < '2026-09-01' LIMIT 100"
    )
    assert check_time_range_consistency(sql, TR) is False


def test_sql_narrower_range_is_consistent() -> None:
    # SQL 只查召回区间内的一个子集，不算越界
    sql = (
        "SELECT * FROM orders WHERE created_at >= '2026-08-10' "
        "AND created_at < '2026-08-20' LIMIT 100"
    )
    assert check_time_range_consistency(sql, TR) is True


def test_no_retrieved_time_range_returns_none() -> None:
    sql = "SELECT * FROM orders LIMIT 100"
    assert check_time_range_consistency(sql, None) is None
    assert check_time_range_consistency(sql, {}) is None
