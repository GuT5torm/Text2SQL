"""第一批（FR-M-1 / FR-M-2 / FR-M-3）的单测。

覆盖五个落点：
  1. 粒度判定与窗口换算：纯函数，不依赖数据库
  2. 不完整周期识别：complete / partial / future 三种状态与边界
  3. 对比基准计算：真实业务库，验证环比、同比与偏离度
  4. 缺失场景：无时间范围、无时间谓词、对比期无数据，输出「无对比期数据」
  5. 规则表：按指标名索引、未配置取 default、粒度下限生效

第 3 组用真实业务库。基准计算是服务端确定性规则，桩模式下也能验证，
这与 FRD「第三轮 23 条零依赖真实模型」的判断一致。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.agent.baselines import (
    _deviation,
    _granularity_rank,
    _infer_granularity,
    _last_year_window,
    _period_completeness,
    _prior_window,
    compute_baselines,
)
from app.semantic.loader import Catalog, load_catalog

# 与业务库的实际覆盖区间对齐：数据从 2025-01 起，2026-09 为当前月。
TODAY = dt.date(2026, 9, 16)

GMV_SQL = """
SELECT c.name AS 区域, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS 成交总额
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
JOIN categories c ON c.id = p.category_id
WHERE o.status NOT IN ('cancelled', 'pending')
  AND o.created_at >= '2026-08-01' AND o.created_at < '2026-09-01'
GROUP BY c.name
ORDER BY 成交总额 DESC
LIMIT 1000
"""


def _range(start: str, end: str, label: str = "指定区间") -> dict[str, Any]:
    return {"start": start, "end": end, "label": label}


def _sql_for(start: str, end: str, alias: str = "值") -> str:
    """按窗口生成一条带时间谓词的聚合语句。"""
    return (
        f"SELECT o.region AS 区域, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS {alias} "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "WHERE o.status NOT IN ('cancelled', 'pending') "
        f"AND o.created_at >= '{start}' AND o.created_at < '{end}' "
        "GROUP BY o.region LIMIT 1000"
    )


# ---------------------------------------------------------------------------
# 1. 粒度判定与窗口换算
# ---------------------------------------------------------------------------


class TestInferGranularity:
    """粒度按窗口跨度自适配。边界值取阈值本身，逐条对应 baselines 的常量。"""

    @pytest.mark.parametrize(
        "span_days, expected",
        [
            (1, "day"),
            (2, "day"),  # DAY_THRESHOLD_DAYS
            (3, "week"),
            (7, "week"),
            (14, "week"),  # WEEK_THRESHOLD_DAYS
            (15, "month"),
            (30, "month"),
            (90, "month"),  # MONTH_THRESHOLD_DAYS
            (91, "year"),
            (365, "year"),
        ],
    )
    def test_thresholds(self, span_days: int, expected: str) -> None:
        assert _infer_granularity(span_days) == expected

    def test_granularity_rank_is_fine_to_coarse(self) -> None:
        assert _granularity_rank("day") < _granularity_rank("week")
        assert _granularity_rank("week") < _granularity_rank("month")
        assert _granularity_rank("month") < _granularity_rank("year")

    def test_granularity_rank_unknown_is_negative(self) -> None:
        assert _granularity_rank("quarter") == -1


class TestPriorWindow:
    """环比窗口：相邻等长周期。"""

    def test_month_window_shifts_one_month(self) -> None:
        assert _prior_window(dt.date(2026, 8, 1), dt.date(2026, 9, 1), "month") == (
            dt.date(2026, 7, 1),
            dt.date(2026, 8, 1),
        )

    def test_month_window_crosses_year(self) -> None:
        assert _prior_window(dt.date(2026, 1, 1), dt.date(2026, 2, 1), "month") == (
            dt.date(2025, 12, 1),
            dt.date(2026, 1, 1),
        )

    def test_day_window_shifts_equal_span(self) -> None:
        assert _prior_window(dt.date(2026, 9, 10), dt.date(2026, 9, 13), "day") == (
            dt.date(2026, 9, 7),
            dt.date(2026, 9, 10),
        )

    def test_prior_window_is_contiguous_with_current(self) -> None:
        """环比的窗口必须紧邻本期，中间不留空隙也不重叠。"""
        start, end = dt.date(2026, 5, 1), dt.date(2026, 8, 1)
        mom_start, mom_end = _prior_window(start, end, "month")
        assert mom_end == start


class TestLastYearWindow:
    """同比窗口：去年同期。"""

    def test_shifts_one_year(self) -> None:
        assert _last_year_window(dt.date(2026, 8, 1), dt.date(2026, 9, 1)) == (
            dt.date(2025, 8, 1),
            dt.date(2025, 9, 1),
        )

    def test_february_29_falls_back_to_28(self) -> None:
        """闰日在上一年不存在，Excel 式口径按 2-28 处理，这里同样。"""
        start, end = _last_year_window(dt.date(2024, 2, 29), dt.date(2024, 3, 1))
        assert start == dt.date(2023, 2, 28)


# ---------------------------------------------------------------------------
# 2. 不完整周期
# ---------------------------------------------------------------------------


class TestPeriodCompleteness:
    """FR-M-2 的判定：周期自然结束日超出数据截止日即为不完整。"""

    def test_past_month_is_complete(self) -> None:
        state = _period_completeness(dt.date(2026, 8, 1), dt.date(2026, 9, 1), TODAY)
        assert state["state"] == "complete"

    def test_current_month_is_partial(self) -> None:
        state = _period_completeness(dt.date(2026, 9, 1), dt.date(2026, 10, 1), TODAY)
        assert state["state"] == "partial"
        assert state["label"] == "本期未完整"

    def test_last_day_of_month_is_complete(self) -> None:
        """9-30 是自然结束日，today 恰为 9-30 时算完整。"""
        state = _period_completeness(dt.date(2026, 9, 1), dt.date(2026, 10, 1), dt.date(2026, 9, 30))
        assert state["state"] == "complete"

    def test_one_day_before_month_end_is_partial(self) -> None:
        state = _period_completeness(dt.date(2026, 9, 1), dt.date(2026, 10, 1), dt.date(2026, 9, 29))
        assert state["state"] == "partial"

    def test_future_period(self) -> None:
        state = _period_completeness(dt.date(2026, 10, 1), dt.date(2026, 11, 1), TODAY)
        assert state["state"] == "future"

    def test_boundary_day_window(self) -> None:
        """日粒度：窗口自然结束日就是今天时算完整。"""
        state = _period_completeness(dt.date(2026, 9, 15), dt.date(2026, 9, 16), TODAY)
        assert state["state"] == "complete"


# ---------------------------------------------------------------------------
# 3. 对比基准计算（真实业务库）
# ---------------------------------------------------------------------------


class TestComputeBaselines:
    """端到端算基准。断言落在结构与口径上，不锁死具体金额——造数脚本
    重跑后金额会变，锁数字会让用例变成维护负担。"""

    def test_month_range_yields_mom_and_yoy(self, business_repo: Any, catalog: Catalog) -> None:
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2026-08-01", "2026-09-01", "上个月"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["metric"] == "成交总额"
        assert result["granularity"] == "month"
        assert result["partial_period"] is False
        assert result["current_value"] is not None
        assert result["missing_reasons"] == []

        mom = result["mom"]
        assert mom is not None and mom["start"] == "2026-07-01" and mom["end"] == "2026-08-01"
        assert mom["comparable"] is True
        assert mom["deviation"] == pytest.approx(
            (result["current_value"] - mom["value"]) / mom["value"]
        )

        yoy = result["yoy"]
        assert yoy is not None and yoy["start"] == "2025-08-01" and yoy["end"] == "2025-09-01"
        assert yoy["comparable"] is True

    def test_current_period_reports_completeness(self, business_repo: Any, catalog: Catalog) -> None:
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2026-08-01", "2026-09-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        period = result["current_period"]
        assert period["state"] == "complete"
        assert period["complete"] is True
        assert period["label"] == "指定区间"

    def test_partial_period_blocks_deviation(self, business_repo: Any, catalog: Catalog) -> None:
        """FR-M-2：本期未走完时值仍给出，但偏离度为空、comparable 为 False。"""
        result = compute_baselines(
            sql=_sql_for("2026-09-01", "2026-10-01"),
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2026-09-01", "2026-10-01", "本月"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["partial_period"] is True
        assert result["note"] == "本期未完整，对比基准仅供参考"
        for key in ("mom", "yoy"):
            item = result[key]
            assert item is not None, f"{key} 应给出值供前端标注"
            assert item["deviation"] is None, f"{key} 的偏离度不得参与结论生成"
            assert item["comparable"] is False
        assert any("不参与结论生成" in r for r in result["missing_reasons"])

    def test_ratio_metric_uses_overall_ratio(self, business_repo: Any, catalog: Catalog) -> None:
        """比率型指标：本期与对比期必须用同一算法。

        若本期按「各分组比率求和」而对比期按「整体比率」计算，退款率会变成
        0.348 比 0.051 这种失真对比。整体比率的取值必然落在 0 与 1 之间，
        求和结果则会随分组数增长，用这个边界把两条路径区分开。
        """
        refund_sql = (
            "SELECT o.region AS 大区, "
            "ROUND(COUNT(DISTINCT r.order_id) * 1.0 / COUNT(DISTINCT o.id), 4) AS 退款率 "
            "FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
            "WHERE o.created_at >= '2025-01-01' AND o.created_at < '2026-01-01' "
            "GROUP BY o.region LIMIT 1000"
        )
        result = compute_baselines(
            sql=refund_sql,
            rows=[],
            metric_used=["退款率"],
            retrieved_time_range=_range("2025-01-01", "2026-01-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["current_value"] is not None
        assert 0.0 <= result["current_value"] <= 1.0

    def test_multi_table_sql_injects_qualified_time_column(
        self, business_repo: Any, catalog: Catalog
    ) -> None:
        """多表语句里 created_at 有歧义，注入谓词时必须带表别名。"""
        sql = (
            "SELECT o.region AS 大区, "
            "ROUND(COUNT(DISTINCT r.order_id) * 1.0 / COUNT(DISTINCT o.id), 4) AS 退款率 "
            "FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
            "GROUP BY o.region LIMIT 1000"
        )
        result = compute_baselines(
            sql=sql,
            rows=[],
            metric_used=["退款率"],
            retrieved_time_range=_range("2026-01-01", "2026-07-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["current_value"] is not None
        assert "本期聚合查询失败" not in result["missing_reasons"]

    def test_complete_period_keeps_deviation(self, business_repo: Any, catalog: Catalog) -> None:
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2026-08-01", "2026-09-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["mom"]["deviation"] is not None
        assert result["note"] == ""


# ---------------------------------------------------------------------------
# 4. 缺失场景
# ---------------------------------------------------------------------------


class TestMissingScenarios:
    """FR-M-1：无法计算时输出「无对比期数据」，不是留空。"""

    def test_no_time_range(self, business_repo: Any, catalog: Catalog) -> None:
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=None,
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["mom"] is None and result["yoy"] is None
        assert result["note"] == "无对比期数据"
        assert result["missing_reasons"]

    def test_bad_time_range(self, business_repo: Any, catalog: Catalog) -> None:
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range={"start": "not-a-date", "end": "2026-09-01"},
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["note"] == "无对比期数据"
        assert any("解析失败" in r for r in result["missing_reasons"])

    def test_zero_span(self, business_repo: Any, catalog: Catalog) -> None:
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2026-08-01", "2026-08-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["note"] == "无对比期数据"
        assert "时间范围跨度为零" in result["missing_reasons"]

    def test_no_aggregate_column(self, business_repo: Any, catalog: Catalog) -> None:
        """SELECT 里没有聚合函数时，本期标量取不到，基准整体不给。"""
        result = compute_baselines(
            sql="SELECT o.id FROM orders o WHERE o.created_at >= '2026-08-01' AND o.created_at < '2026-09-01'",
            rows=[],
            metric_used=[],
            retrieved_time_range=_range("2026-08-01", "2026-09-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["current_value"] is None
        assert "无法识别聚合列（本期聚合跳过）" in result["missing_reasons"]

    def test_invalid_sql_does_not_raise(self, business_repo: Any, catalog: Catalog) -> None:
        """SQL 无法解析时不抛异常，走 missing_reasons。enrich 节点依赖这一点。"""
        result = compute_baselines(
            sql="SELECT FROM WHERE (((",
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2026-08-01", "2026-09-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["note"] == "无对比期数据"

    def test_empty_period_yields_missing_data(self, business_repo: Any, catalog: Catalog) -> None:
        """对比期落在建库之前：值取不到，记为缺失而不是 0。"""
        result = compute_baselines(
            sql=_sql_for("2025-01-01", "2025-02-01"),
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2025-01-01", "2025-02-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert any("环比期数据缺失" in r for r in result["missing_reasons"])


class TestDeviation:
    """偏离度的边界：分母为零与本期不完整都返回 None，不返回 0 或 inf。"""

    def test_zero_base_is_none(self) -> None:
        assert _deviation(100.0, 0.0, False) is None

    def test_partial_is_none(self) -> None:
        assert _deviation(100.0, 50.0, True) is None

    def test_none_inputs(self) -> None:
        assert _deviation(None, 50.0, False) is None
        assert _deviation(100.0, None, False) is None

    def test_normal_case(self) -> None:
        assert _deviation(150.0, 100.0, False) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 5. FR-M-3 规则表
# ---------------------------------------------------------------------------


class TestBaselineRules:
    """规则表按指标名索引，未配置取默认规则。"""

    def test_default_rule_for_unconfigured_metric(self) -> None:
        catalog = load_catalog()
        rule = catalog.baseline_rule("成交总额")
        assert rule["min_granularity"] == "day"
        assert rule["mom"] is True and rule["yoy"] is True

    def test_metric_specific_rule_overrides_default(self) -> None:
        catalog = load_catalog()
        assert catalog.baseline_rule("客单价")["min_granularity"] == "month"
        assert catalog.baseline_rule("退款率")["min_granularity"] == "week"

    def test_lookup_accepts_both_name_and_display(self) -> None:
        catalog = load_catalog()
        assert catalog.baseline_rule("aov") == catalog.baseline_rule("客单价")

    def test_unknown_metric_falls_back_to_default(self) -> None:
        catalog = load_catalog()
        assert catalog.baseline_rule("不存在的指标") == catalog.baseline_rule(None)

    def test_default_present(self) -> None:
        assert "default" in load_catalog().baseline_rules

    def test_min_granularity_skips_noisy_baseline(self, business_repo: Any, catalog: Catalog) -> None:
        """客单价在周粒度下按规则不输出基准，且给出原因而不是静默留空。"""
        result = compute_baselines(
            sql=_sql_for("2026-09-07", "2026-09-14"),
            rows=[],
            metric_used=["客单价"],
            retrieved_time_range=_range("2026-09-07", "2026-09-14"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["granularity"] == "week"
        assert result["mom"] is None and result["yoy"] is None
        assert result["note"] == "无对比期数据"
        assert any("粒度下限" in r for r in result["missing_reasons"])
        assert result["rule"]["min_granularity"] == "month"

    def test_same_metric_passes_at_allowed_granularity(
        self, business_repo: Any, catalog: Catalog
    ) -> None:
        """同一指标在月粒度下放行，验证跳过是粒度判定而不是指标被整体关闭。"""
        result = compute_baselines(
            sql=_sql_for("2026-08-01", "2026-09-01"),
            rows=[],
            metric_used=["客单价"],
            retrieved_time_range=_range("2026-08-01", "2026-09-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["granularity"] == "month"
        assert result["mom"] is not None

    def test_rule_flag_disables_baseline(self, business_repo: Any, catalog: Catalog) -> None:
        """规则里关掉 mom 时不执行环比查询，只给原因。"""
        catalog = load_catalog()
        catalog.baseline_rules["gmv"] = {**catalog.baseline_rule("gmv"), "mom": False}
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["gmv"],
            retrieved_time_range=_range("2026-08-01", "2026-09-01"),
            data_freshness=None,
            catalog=catalog,
            repo=business_repo,
            today=TODAY,
        )
        assert result["mom"] is None
        assert result["yoy"] is not None
        assert any("不计算环比" in r for r in result["missing_reasons"])

    def test_catalog_without_rules_still_computes(self, business_repo: Any) -> None:
        """规则表缺失时按「放开」处理，不因为配置缺失把功能整体关掉。"""
        bare = Catalog(
            metrics=[],
            tables=[],
            dimensions=[],
            synonyms=[],
            value_synonyms=[],
            time_terms=[],
        )
        result = compute_baselines(
            sql=GMV_SQL,
            rows=[],
            metric_used=["成交总额"],
            retrieved_time_range=_range("2026-08-01", "2026-09-01"),
            data_freshness=None,
            catalog=bare,
            repo=business_repo,
            today=TODAY,
        )
        assert result["mom"] is not None and result["yoy"] is not None
