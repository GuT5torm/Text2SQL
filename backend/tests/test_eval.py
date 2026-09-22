"""评测脚本的单测。

覆盖三件事：

1. EXPECT_FIELDS 白名单校验——合法字段不报错、非法字段在加载期就 SystemExit
2. 新断言判定函数（dimensions / granularity / sql_contains / must_join）按
   预期工作，含边界与漏匹配
3. 报告结构自洽——cases 长度 = total + skipped，skipped 字段提到外层

不动 run_eval.py 的链路，只覆盖其可独立测试的部分。
"""

from __future__ import annotations

import sys
from pathlib import Path

# eval/ 是 backend/ 下的同级目录，不是 app/ 子模块。把 backend/ 加进 sys.path
# 后以 eval.run_eval 模块名引用，避免在 app/ 下再建一层薄包装。
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

from eval import run_eval  # noqa: E402


# ---------------------------------------------------------------------------
# 白名单校验
# ---------------------------------------------------------------------------


def _stub_suite(group_name: str, expect: dict) -> dict:
    return {
        "version": 1,
        "groups": {
            group_name: {
                "cases": [{"id": "stub-001", "question": "x", "expect": expect}]
            }
        },
    }


@pytest.mark.parametrize(
    "group,expect",
    [
        # recall 组：现有字段全部合法
        ("recall", {"metrics": ["gmv"], "dimensions": ["region"], "time_range": {"label": "x", "start": "2026-01-01", "end": "2026-02-01"}, "value_hints": [], "empty": False}),
        # pipeline 组：含 FR-K-1 新增字段
        ("pipeline", {"status": "ok", "min_rows": 1, "chart_type": "bar", "dimensions": ["city"], "granularity": "day", "sql_contains": ["users"], "must_join": ["users"], "filters": [], "order_by": "desc", "top_n": 5}),
        # safety 组：现有字段
        ("safety", {"inject_sql": "SELECT 1", "blocked": True, "status": "error", "max_rows": 1000}),
        # datalineage 组：FR-D-1 / FR-D-2 字段（第三批）
        ("datalineage", {"status": "ok", "min_rows": 1, "source_tables": ["orders", "refunds"], "freshness_present": True, "freshness_orders_cutoff_iso": True, "freshness_refunds_cutoff_iso": True}),
        # baseline 组：FR-M-1 至 FR-M-3 字段
        ("baseline", {"status": "ok", "min_rows": 1, "granularity": "month", "mom_present": True, "yoy_present": False, "mom_comparable": True, "partial_period": False, "note": "", "missing_contains": ["x"], "max_queries": 6}),
        # drilldown 组：FR-L-1 至 FR-L-3 字段
        ("drilldown", {"question": "华东的成交总额按下单渠道分布", "next_dimension": "channel", "only_one_level": True, "available": True, "findings_min": 1, "findings_rules": ["rank"], "findings_absent": ["deviation"], "findings_fields": ["metric", "slice"], "deepens_to": ["渠道"]}),
    ],
)
def test_validate_expect_fields_accepts_known_fields(group: str, expect: dict) -> None:
    """合法字段名应当被白名单接受，不抛异常。"""
    run_eval._validate_expect_fields(_stub_suite(group, expect))


def test_validate_expect_fields_rejects_unknown_field() -> None:
    """非法字段名应当在加载期直接 SystemExit，避免断言形同虚设。"""
    suite = _stub_suite("pipeline", {"status": "ok", "bogus_field": "x"})
    with pytest.raises(SystemExit) as exc:
        run_eval._validate_expect_fields(suite)
    assert "bogus_field" in str(exc.value)


def test_validate_expect_fields_unknown_group_passes() -> None:
    """未列入白名单的组别（如未来才加的新组）按现状放过。

    校验的目的不是锁死所有组，而是保证已登记组别的 expect 字段有定义可循。
    """
    suite = _stub_suite("future_group", {"anything": 1})
    run_eval._validate_expect_fields(suite)  # 不抛异常


# ---------------------------------------------------------------------------
# 新断言判定函数
# ---------------------------------------------------------------------------


def test_sql_contains_case_insensitive() -> None:
    """子串匹配不区分大小写。"""
    sql = "SELECT u.id, u.name FROM users u"
    assert run_eval._sql_contains(sql, ["USERS", "name"]) == []
    assert run_eval._sql_contains(sql, ["missing"]) == ["missing"]


def test_must_join_detects_left_inner_outer() -> None:
    """所有 JOIN 形态都应被识别。"""
    sql = """
        SELECT *
        FROM orders o
        LEFT JOIN users u ON u.id = o.user_id
        INNER JOIN products p ON p.id = o.product_id
    """
    assert run_eval._must_join(sql, ["users", "products"]) == []
    assert run_eval._must_join(sql, ["refunds"]) == ["refunds"]


def test_must_join_substring_not_false_positive() -> None:
    """'joined' 这种含 'join' 子串但不是 JOIN 子句的文本不应误判。

    实现采用 'join <table>' 形式而非单 'join'，可避免这类假阳性。
    """
    sql = "SELECT * FROM joined_log WHERE id = 1"
    # 'joined_log' 不是任何 JOIN 的目标，不应被认作 JOIN users
    assert run_eval._must_join(sql, ["users"]) == ["users"]


def test_has_granularity_matches_strftime_patterns() -> None:
    """五种粒度的特征模式都能识别。"""
    day_sql = "SELECT strftime('%Y-%m-%d', o.created_at) AS 日期 FROM orders o"
    week_sql = "SELECT strftime('%Y-%W', o.created_at) AS 周 FROM orders o"
    month_sql = "SELECT strftime('%Y-%m', o.created_at) AS 月 FROM orders o"
    quarter_sql = "SELECT (cast(strftime('%m', o.created_at) AS INTEGER) + 2) / 3 AS 季 FROM orders o"
    year_sql = "SELECT strftime('%Y', o.created_at) AS 年 FROM orders o"

    assert run_eval._has_granularity(day_sql, "day") is True
    assert run_eval._has_granularity(week_sql, "week") is True
    assert run_eval._has_granularity(month_sql, "month") is True
    assert run_eval._has_granularity(quarter_sql, "quarter") is True
    assert run_eval._has_granularity(year_sql, "year") is True


def test_has_granularity_rejects_wrong_granularity() -> None:
    """month SQL 不能匹配 day 期望。"""
    sql = "SELECT strftime('%Y-%m', o.created_at) AS 月份 FROM orders o"
    assert run_eval._has_granularity(sql, "day") is False


def test_has_granularity_unknown_returns_false() -> None:
    """未知粒度名安全降级为 False，不抛异常。"""
    assert run_eval._has_granularity("SELECT 1", "fortnight") is False


def test_has_dimensions_matches_sql_or_columns() -> None:
    """维度在 SQL 或结果集列名里出现即视为写入。"""
    sql = "SELECT region, SUM(amount) FROM orders GROUP BY region"
    cols = ["region", "SUM(amount)"]
    assert run_eval._has_dimensions(sql, cols, ["region"]) == []
    assert run_eval._has_dimensions(sql, cols, ["city"]) == ["city"]


def test_has_source_tables_subset_case_insensitive() -> None:
    """source_tables 判定为集合子集，不区分大小写。"""
    actual = ["orders", "refunds", "order_items"]
    assert run_eval._has_source_tables(actual, ["orders", "refunds"]) == []
    assert run_eval._has_source_tables(actual, ["ORDERS", "users"]) == ["users"]


def test_has_source_tables_dedupes_expected() -> None:
    """期望列表里有重复项也按一次处理。"""
    assert run_eval._has_source_tables(["orders"], ["orders", "orders"]) == []


def test_is_iso_date_accepts_canonical_form() -> None:
    """_is_iso_date 接受标准 YYYY-MM-DD。"""
    assert run_eval._is_iso_date("2026-09-15") is True


@pytest.mark.parametrize("bad", [None, 12345, "2026/09/15", "2026-9-15", "", "2026-09-32"])
def test_is_iso_date_rejects_bad_values(bad) -> None:
    """非字符串、错格式、错日期都被拒。"""
    assert run_eval._is_iso_date(bad) is False


# ---------------------------------------------------------------------------
# 报告结构
# ---------------------------------------------------------------------------


def test_outcome_to_dict_lifts_skipped() -> None:
    """skipped 字段应在外层，observed 里不再保留 skipped 键（语义重复）。"""
    outcome = run_eval.CaseOutcome(
        case_id="x",
        question="q",
        intent="i",
        passed=False,
        reasons=[],
        observed={"skipped": "桩实现走不到该分支"},
        skipped=True,
    )
    record = run_eval._outcome_to_dict(outcome)
    assert record["skipped"] is True
    assert record["passed"] is False


def test_assert_invariants_passes_when_lengths_match() -> None:
    """cases 长度 == total + skipped 时通过。"""
    summary = {"total": 9, "passed": 6, "skipped": 1}
    outcomes = [run_eval.CaseOutcome(case_id=str(i), question="", intent="", passed=True) for i in range(9)]
    outcomes.append(run_eval.CaseOutcome(case_id="x", question="", intent="", passed=False, skipped=True))
    run_eval._assert_invariants("pipeline", summary, outcomes)


def test_assert_invariants_fails_on_length_mismatch() -> None:
    """cases 长度与 total + skipped 不一致时 SystemExit。"""
    summary = {"total": 9, "passed": 6, "skipped": 1}
    # 期望 10 条（9+1），只造 8 条，触发不变量失败
    outcomes = [run_eval.CaseOutcome(case_id=str(idx), question="", intent="", passed=True) for idx in range(8)]
    with pytest.raises(SystemExit) as exc:
        run_eval._assert_invariants("pipeline", summary, outcomes)
    assert "cases 长度" in str(exc.value)
