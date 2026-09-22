"""FR-L-1 异常判定单测。

覆盖三条判定规则各自的命中与不命中、严重度分级、去重、以及「不完整周期
不参与判定」这一条与 FR-M-2 的衔接。全部是纯函数，不依赖数据库与模型——
判定规则本来就该是确定性的，依赖外部反而测不出判据本身。
"""

from __future__ import annotations

from typing import Any

from app.agent.findings import MAX_FINDINGS, detect_findings

DEFAULT_RULES: dict[str, Any] = {
    "deviation_threshold": 0.20,
    "rank_ratio": 0.20,
    "min_slices": 4,
    "severity_ratio": 2.0,
    "severity": "提示",
}

REFUND_RULES: dict[str, Any] = {
    **DEFAULT_RULES,
    "absolute": {"threshold": 0.10, "direction": "above", "severity": "关注"},
}

ROWS = [
    {"大区": "华东", "成交总额": 15789290.26},
    {"大区": "华南", "成交总额": 8214000.10},
    {"大区": "华北", "成交总额": 7116766.21},
    {"大区": "华中", "成交总额": 5400000.00},
    {"大区": "西南", "成交总额": 4300000.00},
    {"大区": "西北", "成交总额": 3600000.00},
    {"大区": "东北", "成交总额": 3051176.82},
]


def _baselines(*, mom: dict[str, Any] | None = None, yoy: dict[str, Any] | None = None,
               current: float | None = 100.0, partial: bool = False) -> dict[str, Any]:
    return {
        "metric": "成交总额",
        "granularity": "month",
        "current_value": current,
        "current_period": {"start": "2026-08-01", "end": "2026-09-01", "label": "上个月"},
        "mom": mom,
        "yoy": yoy,
        "partial_period": partial,
        "missing_reasons": [],
    }


class TestDeviationRule:
    def test_hit_when_deviation_exceeds_threshold(self) -> None:
        baselines = _baselines(mom={"label": "环比", "value": 100.0, "deviation": 0.30,
                                    "comparable": True})
        found = detect_findings(rows=[], columns=[], metric="成交总额",
                                baselines=baselines, rules=DEFAULT_RULES)
        assert len(found) == 1
        assert found[0]["rule"] == "deviation"
        assert found[0]["dimension"] is None and found[0]["slice"] is None
        assert found[0]["value"] == 100.0 and found[0]["baseline"] == 100.0
        assert found[0]["deviation"] == 0.3

    def test_miss_when_deviation_below_threshold(self) -> None:
        baselines = _baselines(mom={"label": "环比", "value": 100.0, "deviation": 0.10,
                                    "comparable": True})
        assert detect_findings(rows=[], columns=[], metric="成交总额",
                               baselines=baselines, rules=DEFAULT_RULES) == []

    def test_incomparable_mom_falls_back_to_yoy(self) -> None:
        """不完整周期下环比 comparable=false，应改用同比而不是直接放弃。"""
        baselines = _baselines(
            mom={"label": "环比", "value": 100.0, "deviation": 0.30, "comparable": False},
            yoy={"label": "同比", "value": 80.0, "deviation": 0.25, "comparable": True},
            partial=True,
        )
        found = detect_findings(rows=[], columns=[], metric="成交总额",
                                baselines=baselines, rules=DEFAULT_RULES)
        assert len(found) == 1
        assert found[0]["baseline"] == 80.0
        assert "同比" in found[0]["reason"]

    def test_partial_period_never_yields_deviation_finding(self) -> None:
        """FR-M-2 的隔离在此收口：两个基准都不可比时不给偏离类异常。"""
        baselines = _baselines(
            mom={"label": "环比", "value": 100.0, "deviation": 0.30, "comparable": False},
            yoy={"label": "同比", "value": 80.0, "deviation": 0.25, "comparable": False},
            partial=True,
        )
        assert detect_findings(rows=[], columns=[], metric="成交总额",
                               baselines=baselines, rules=DEFAULT_RULES) == []

    def test_severity_upgrades_at_double_threshold(self) -> None:
        mild = _baselines(mom={"label": "环比", "value": 100.0, "deviation": 0.25,
                               "comparable": True})
        severe = _baselines(mom={"label": "环比", "value": 100.0, "deviation": 0.45,
                                 "comparable": True})
        assert detect_findings(rows=[], columns=[], metric="成交总额",
                               baselines=mild, rules=DEFAULT_RULES)[0]["severity"] == "提示"
        assert detect_findings(rows=[], columns=[], metric="成交总额",
                               baselines=severe, rules=DEFAULT_RULES)[0]["severity"] == "关注"

    def test_negative_deviation_is_reported_as_lower(self) -> None:
        baselines = _baselines(mom={"label": "环比", "value": 200.0, "deviation": -0.35,
                                    "comparable": True})
        found = detect_findings(rows=[], columns=[], metric="成交总额",
                                baselines=baselines, rules=DEFAULT_RULES)
        assert "低于" in found[0]["reason"]


class TestRankRule:
    def test_flags_both_ends(self) -> None:
        found = detect_findings(rows=ROWS, columns=["大区", "成交总额"], metric="成交总额",
                                baselines=None, rules=DEFAULT_RULES)
        assert {f["slice"] for f in found} == {"华东", "东北"}
        assert all(f["rule"] == "rank" for f in found)

    def test_baseline_is_median_of_same_dimension(self) -> None:
        found = detect_findings(rows=ROWS, columns=["大区", "成交总额"], metric="成交总额",
                                baselines=None, rules=DEFAULT_RULES)
        top = next(f for f in found if f["slice"] == "华东")
        # 7 个切片的中位数是第 4 个：华中 5_400_000
        assert top["baseline"] == 5400000.0
        assert top["deviation"] > 0

    def test_skipped_when_slices_below_min(self) -> None:
        rows = ROWS[:3]
        assert detect_findings(rows=rows, columns=["大区", "成交总额"], metric="成交总额",
                               baselines=None, rules=DEFAULT_RULES) == []

    def test_skipped_when_values_are_close(self) -> None:
        rows = [{"渠道": c, "订单量": 100 + i} for i, c in enumerate("ABCD")]
        assert detect_findings(rows=rows, columns=["渠道", "订单量"], metric="订单量",
                               baselines=None, rules=DEFAULT_RULES) == []

    def test_numeric_dimension_column_is_not_taken_as_value(self) -> None:
        """按月份分组时维度列本身是数值，取错列会把 202608 当成指标值。"""
        rows = [
            {"月份": 202605, "成交总额": 100.0},
            {"月份": 202606, "成交总额": 300.0},
            {"月份": 202607, "成交总额": 200.0},
            {"月份": 202608, "成交总额": 900.0},
        ]
        found = detect_findings(rows=rows, columns=["月份", "成交总额"], metric="成交总额",
                                baselines=None, rules=DEFAULT_RULES)
        assert found and all(f["dimension"] == "月份" for f in found)
        assert all(isinstance(f["slice"], int) for f in found)


class TestAbsoluteRule:
    def test_flags_slices_above_threshold(self) -> None:
        # 四个切片彼此接近，排名规则不触发，命中的只有绝对阈值
        rows = [
            {"大区": "华东", "退款率": 0.110},
            {"大区": "华南", "退款率": 0.090},
            {"大区": "华北", "退款率": 0.085},
            {"大区": "华中", "退款率": 0.080},
        ]
        found = detect_findings(rows=rows, columns=["大区", "退款率"], metric="退款率",
                                baselines=None, rules=REFUND_RULES)
        assert [f["slice"] for f in found] == ["华东"]
        assert found[0]["rule"] == "absolute"
        assert found[0]["severity"] == "关注"
        assert found[0]["baseline"] == 0.10
        assert found[0]["deviation"] is None

    def test_not_applied_without_config(self) -> None:
        rows = [{"大区": "华东", "退款率": 0.5}]
        assert detect_findings(rows=rows, columns=["大区", "退款率"], metric="退款率",
                               baselines=None, rules=DEFAULT_RULES) == []

    def test_below_direction(self) -> None:
        rules = {**DEFAULT_RULES, "absolute": {"threshold": 0.50, "direction": "below"}}
        rows = [{"品类": "甲", "毛利率": 0.20}, {"品类": "乙", "毛利率": 0.80}]
        found = detect_findings(rows=rows, columns=["品类", "毛利率"], metric="毛利率",
                                baselines=None, rules=rules)
        assert [f["slice"] for f in found] == ["甲"]


class TestDedupeAndCap:
    def test_same_slice_keeps_one_finding(self) -> None:
        """排名与绝对阈值同时命中「华东」时只留一条，一个切片不该出现两个标记。"""
        rows = [
            {"大区": "华东", "退款率": 0.30},
            {"大区": "华南", "退款率": 0.12},
            {"大区": "华北", "退款率": 0.11},
            {"大区": "华中", "退款率": 0.10},
        ]
        found = detect_findings(rows=rows, columns=["大区", "退款率"], metric="退款率",
                                baselines=None, rules=REFUND_RULES)
        slices = [f["slice"] for f in found]
        assert len(slices) == len(set(slices))
        east = next(f for f in found if f["slice"] == "华东")
        # 同为「关注」时业务阈值优先于统计排名
        assert east["rule"] == "absolute"

    def test_lower_severity_loses_to_higher(self) -> None:
        """严重度不同时留高的那条，与规则种类无关。"""
        rows = [
            {"大区": "华东", "退款率": 0.30},
            {"大区": "华南", "退款率": 0.12},
            {"大区": "华北", "退款率": 0.11},
            {"大区": "华中", "退款率": 0.10},
        ]
        # 绝对阈值给「关注」，排名规则按默认只能给「提示」
        rules = {**REFUND_RULES, "absolute": {"threshold": 0.10, "direction": "above",
                                              "severity": "关注"}}
        found = detect_findings(rows=rows, columns=["大区", "退款率"], metric="退款率",
                                baselines=None, rules=rules)
        assert all(f["severity"] == "关注" for f in found if f["slice"] != "华中")

    def test_findings_capped(self) -> None:
        rows = [{"城市": f"城市{i}", "成交总额": 100.0 * (i + 1) ** 3} for i in range(30)]
        found = detect_findings(rows=rows, columns=["城市", "成交总额"], metric="成交总额",
                                baselines=None, rules=DEFAULT_RULES)
        assert len(found) == MAX_FINDINGS

    def test_severe_first(self) -> None:
        rows = [{"大区": f"区{i}", "退款率": 0.05 + i * 0.03} for i in range(6)]
        found = detect_findings(rows=rows, columns=["大区", "退款率"], metric="退款率",
                                baselines=None, rules=REFUND_RULES)
        assert found[0]["severity"] == "关注"


class TestFieldContract:
    def test_every_finding_carries_the_declared_fields(self) -> None:
        """FRD 列出的八个字段必须齐备，缺一个前端就渲染不出可点击标记。"""
        required = {
            "metric", "dimension", "slice", "value", "baseline",
            "deviation", "severity", "reason",
        }
        found = detect_findings(
            rows=ROWS, columns=["大区", "成交总额"], metric="成交总额",
            baselines=_baselines(mom={"label": "环比", "value": 100.0,
                                      "deviation": 0.3, "comparable": True}),
            rules=DEFAULT_RULES,
        )
        assert found
        for f in found:
            assert required <= set(f)

    def test_missing_inputs_yield_empty_list(self) -> None:
        assert detect_findings(rows=[], columns=[], metric=None,
                               baselines=None, rules=None) == []
