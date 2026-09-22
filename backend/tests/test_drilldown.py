"""FR-L-2 下钻单测。

覆盖两件事：问句是不是「在上一轮槽位上加深一层」，以及边界是不是收住了——
没有可加深维度时给出原因而不是拼出一个跑不通的问句。
"""

from __future__ import annotations

import pytest

from app.agent.drilldown import build_drilldown
from app.semantic.loader import Catalog, load_catalog


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog()


class TestFromFinding:
    def test_deepens_one_level(self, catalog: Catalog) -> None:
        plan = build_drilldown(
            source="finding", metric="成交总额", dimension="大区",
            slice_value="华东", catalog=catalog,
        )
        # gmv 的维度顺序：region → channel → category_name → brand → user_level
        assert plan.next_dimension == "channel"
        assert plan.question == "华东的成交总额按下单渠道分布"
        assert plan.available is True

    def test_time_label_is_inherited(self, catalog: Catalog) -> None:
        plan = build_drilldown(
            source="finding", metric="退款率", dimension="大区",
            slice_value="华中", time_label="上个月", catalog=catalog,
        )
        assert plan.question == "上个月华中的退款率按下单渠道分布"

    def test_overall_finding_has_no_slice(self, catalog: Catalog) -> None:
        """整体型异常没有切片，问句里不能留下一个孤立的「的」字。"""
        plan = build_drilldown(source="finding", metric="成交总额", catalog=catalog)
        assert plan.question == "成交总额按销售大区分布"
        assert plan.slice_value is None

    def test_column_alias_is_recognised_as_dimension(self, catalog: Catalog) -> None:
        """findings 里的 dimension 是结果表列名（大区），不是配置名（region）。"""
        plan = build_drilldown(
            source="finding", metric="成交总额", dimension="大区",
            slice_value="华东", catalog=catalog,
        )
        assert plan.dimension == "大区"
        assert plan.next_dimension == "channel"

    def test_stops_at_the_last_dimension(self, catalog: Catalog) -> None:
        plan = build_drilldown(
            source="finding", metric="退款率", dimension="品类",
            slice_value="数码", catalog=catalog,
        )
        # refund_rate 的维度在 category_name 之后只剩 refund_reason
        assert plan.next_dimension == "refund_reason"
        exhausted = build_drilldown(
            source="finding", metric="退款率", dimension="退款原因",
            slice_value="质量问题", catalog=catalog,
        )
        assert exhausted.available is False
        assert exhausted.question == ""
        assert "无可加深的维度" in exhausted.reason

    def test_unknown_metric_falls_back_to_global_order(self, catalog: Catalog) -> None:
        plan = build_drilldown(
            source="finding", metric="未登记指标", dimension="大区",
            slice_value="华东", catalog=catalog,
        )
        assert plan.next_dimension == "channel"


class TestFromCell:
    def test_filters_without_deepening(self, catalog: Catalog) -> None:
        plan = build_drilldown(
            source="cell", metric="成交总额", dimension="大区",
            slice_value="华东", catalog=catalog,
        )
        assert plan.question == "华东的成交总额"
        assert plan.next_dimension is None
        assert "同口径" in plan.reason

    def test_time_label_is_inherited(self, catalog: Catalog) -> None:
        plan = build_drilldown(
            source="cell", metric="退款率", dimension="大区",
            slice_value="华中", time_label="今年以来", catalog=catalog,
        )
        assert plan.question == "今年以来华中的退款率"

    def test_missing_slice_value_is_rejected(self, catalog: Catalog) -> None:
        plan = build_drilldown(
            source="cell", metric="成交总额", dimension="大区", catalog=catalog
        )
        assert plan.available is False
        assert "维度取值" in plan.reason


def test_unknown_source_raises(catalog: Catalog) -> None:
    with pytest.raises(ValueError, match="下钻来源非法"):
        build_drilldown(source="other", metric="成交总额", catalog=catalog)  # type: ignore[arg-type]
