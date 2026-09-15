"""语义层用例：加载校验、时间换算、规则召回、上下文渲染。

语义层是纯函数模块（不连库、不调模型），因此这一层可以做到无外部依赖全覆盖。
时间换算的用例全部用固定日期，避免「今天」变化导致断言漂移。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.semantic.loader import (
    Catalog,
    MAX_METRICS,
    SemanticError,
    TimeRange,
    _shift_months,
    _validate,
    describe_catalog,
    load_catalog,
    recall,
    render_context,
    resolve_time_term,
)

TODAY = dt.date(2026, 9, 15)  # 周二，用于验证周从周一起算


# ---------------------------------------------------------------------------
# 最小可用配置，用于校验逻辑的正反例
# ---------------------------------------------------------------------------

_MIN_TABLES = {
    "tables": [
        {
            "name": "orders",
            "display": "订单表",
            "fields": [
                {"name": "id", "type": "INTEGER"},
                {"name": "region", "type": "TEXT"},
                {"name": "created_at", "type": "TEXT"},
            ],
        }
    ],
    "dimensions": [
        {
            "name": "order_month",
            "display": "下单月份",
            "expression": "strftime('%Y-%m', orders.created_at)",
            "tables": ["orders"],
        }
    ],
}
_MIN_METRICS = {
    "metrics": [
        {
            "name": "gmv",
            "display": "成交总额",
            "expression": "SUM(orders.amount)",
            "tables": ["orders"],
            "dimensions": ["order_month"],
            "examples": [{"q": "上个月成交总额", "sql": "SELECT 1"}],
        }
    ]
}
_MIN_SYNONYMS = {
    "synonyms": [{"term": "流水", "maps_to": "gmv", "type": "metric"}],
    "value_synonyms": [{"term": "江浙沪", "field": "orders.region", "value": "华东"}],
    "time_terms": [{"term": "上个月", "kind": "last_month"}],
}


def _write_catalog(
    directory: Path,
    tables: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    synonyms: dict[str, Any] | None = None,
) -> None:
    for filename, payload in (
        ("tables.yaml", tables if tables is not None else _MIN_TABLES),
        ("metrics.yaml", metrics if metrics is not None else _MIN_METRICS),
        ("synonyms.yaml", synonyms if synonyms is not None else _MIN_SYNONYMS),
    ):
        (directory / filename).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# 加载与一致性校验
# ---------------------------------------------------------------------------


def test_load_minimal_catalog(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    catalog = load_catalog(tmp_path)
    assert catalog.table_names() == {"orders"}
    assert "gmv" in catalog.metric_by_name
    assert catalog.all_field_refs()["orders"] == {"id", "region", "created_at"}


def test_load_catalog_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SemanticError, match="语义层文件不存在"):
        load_catalog(tmp_path)


def test_load_catalog_rejects_non_mapping(tmp_path: Path) -> None:
    (tmp_path / "tables.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    (tmp_path / "metrics.yaml").write_text("metrics: []\n", encoding="utf-8")
    (tmp_path / "synonyms.yaml").write_text("synonyms: []\n", encoding="utf-8")
    with pytest.raises(SemanticError, match="顶层必须是映射"):
        load_catalog(tmp_path)


def test_validate_reports_metric_referencing_unknown_table(tmp_path: Path) -> None:
    _write_catalog(tmp_path, metrics={"metrics": [{"name": "gmv", "expression": "SUM(x)", "tables": ["nope"]}]})
    with pytest.raises(SemanticError, match="引用了未登记的表 nope"):
        load_catalog(tmp_path)


def test_validate_reports_duplicate_field(tmp_path: Path) -> None:
    tables = {
        "tables": [
            {
                "name": "orders",
                "display": "订单表",
                "fields": [{"name": "id", "type": "INTEGER"}, {"name": "id", "type": "TEXT"}],
            }
        ]
    }
    _write_catalog(tmp_path, tables=tables)
    with pytest.raises(SemanticError, match="字段 id 重复定义"):
        load_catalog(tmp_path)


def test_validate_reports_bad_synonym_target(tmp_path: Path) -> None:
    _write_catalog(tmp_path, synonyms={"synonyms": [{"term": "流水", "maps_to": "nope", "type": "metric"}]})
    with pytest.raises(SemanticError, match="指向不存在的指标 nope"):
        load_catalog(tmp_path)


def test_validate_reports_bad_value_synonym(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        synonyms={"value_synonyms": [{"term": "江浙沪", "field": "orders.nope", "value": "华东"}]},
    )
    with pytest.raises(SemanticError, match="指向不存在的字段"):
        load_catalog(tmp_path)


def test_validate_lists_all_problems_at_once() -> None:
    """配置错误应当一次列全，而不是改一个报一个。"""
    catalog = Catalog(metrics=[], tables=[], dimensions=[], synonyms=[], value_synonyms=[], time_terms=[])
    problems = _validate(catalog)
    assert any("没有任何指标定义" in p for p in problems)
    assert any("没有任何表定义" in p for p in problems)
    assert len(problems) >= 2


def test_real_catalog_is_consistent(catalog: Catalog) -> None:
    """真实配置必须能加载——这条失败意味着三个 YAML 之间有引用断裂。"""
    assert "orders" in catalog.table_names()
    assert "gmv" in catalog.metric_by_name
    assert _validate(catalog) == []


# ---------------------------------------------------------------------------
# 相对时间换算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "start", "end"),
    [
        ("today", "2026-09-15", "2026-09-16"),
        ("yesterday", "2026-09-14", "2026-09-15"),
        ("this_week", "2026-09-14", "2026-09-21"),  # 周一起算
        ("last_week", "2026-09-07", "2026-09-14"),
        ("this_month", "2026-09-01", "2026-10-01"),
        ("last_month", "2026-08-01", "2026-09-01"),
        ("this_quarter", "2026-07-01", "2026-10-01"),
        ("last_quarter", "2026-04-01", "2026-07-01"),
        ("this_year", "2026-01-01", "2027-01-01"),
        ("last_year", "2025-01-01", "2026-01-01"),
    ],
)
def test_resolve_time_term(kind: str, start: str, end: str) -> None:
    rng = resolve_time_term({"kind": kind, "term": kind}, TODAY)
    assert (rng.start, rng.end) == (start, end)


def test_resolve_time_term_last_quarter_crosses_year() -> None:
    """一季度时上季度落在上一年的第四季度。"""
    rng = resolve_time_term({"kind": "last_quarter", "term": "上季度"}, dt.date(2026, 2, 15))
    assert (rng.start, rng.end) == ("2025-10-01", "2026-01-01")


def test_resolve_time_term_last_n_days_includes_today() -> None:
    rng = resolve_time_term({"kind": "last_n_days", "term": "近7天", "n": 7}, TODAY)
    assert (rng.start, rng.end) == ("2026-09-09", "2026-09-16")


def test_resolve_time_term_unknown_kind() -> None:
    with pytest.raises(SemanticError, match="未实现的时间类型"):
        resolve_time_term({"kind": "no_such_kind", "term": "某时"}, TODAY)


def test_shift_months_clamps_to_month_end() -> None:
    """1 月 31 日往前推一个月是 12 月 31 日，往前推到 2 月要退到 28 日。"""
    assert _shift_months(dt.date(2026, 3, 31), -1) == dt.date(2026, 2, 28)
    assert _shift_months(dt.date(2026, 1, 31), -1) == dt.date(2025, 12, 31)
    assert _shift_months(dt.date(2024, 1, 31), -1) == dt.date(2023, 12, 31)


def test_time_range_filter_is_half_open() -> None:
    rng = TimeRange(start="2026-08-01", end="2026-09-01", label="上个月", resolved="")
    assert rng.as_filter() == "orders.created_at >= '2026-08-01' AND orders.created_at < '2026-09-01'"
    assert rng.as_filter("o.created_at").startswith("o.created_at")


# ---------------------------------------------------------------------------
# 规则召回
# ---------------------------------------------------------------------------


def test_recall_picks_metric_and_time(catalog: Catalog) -> None:
    hit = recall("上个月各区域的成交总额", catalog, TODAY)
    assert [m["name"] for m in hit.metrics] == ["gmv"]
    assert hit.time_range is not None
    assert (hit.time_range.label, hit.time_range.start, hit.time_range.end) == (
        "上个月",
        "2026-08-01",
        "2026-09-01",
    )
    assert "region" in {d["name"] for d in hit.dimensions}
    assert "orders" in {t["name"] for t in hit.tables}


def test_recall_picks_refund_metric_with_related_tables(catalog: Catalog) -> None:
    hit = recall("各品类的退款率", catalog, TODAY)
    assert [m["name"] for m in hit.metrics] == ["refund_rate"]
    assert {"refunds", "orders"} <= {t["name"] for t in hit.tables}


def test_recall_maps_colloquial_value(catalog: Catalog) -> None:
    hit = recall("江浙沪的销量", catalog, TODAY)
    assert any("华东" in hint for hint in hit.value_hints)
    assert any("江浙沪" in hint for hint in hit.value_hints)


def test_recall_prefers_longer_time_term(catalog: Catalog) -> None:
    """「去年同期」不能被更短的「去年」抢先匹配。"""
    hit = recall("去年同期的成交总额", catalog, TODAY)
    assert hit.time_range is not None
    assert hit.time_range.label == "去年同期"


def test_recall_respects_metric_limit(catalog: Catalog) -> None:
    hit = recall("成交总额、销量、订单量、客单价、退款率分别是多少", catalog, TODAY)
    assert 0 < len(hit.metrics) <= MAX_METRICS


def test_recall_returns_empty_for_off_topic_question(catalog: Catalog) -> None:
    hit = recall("帮我写一首诗", catalog, TODAY)
    assert hit.is_empty


def test_recall_examples_come_from_hit_metrics(catalog: Catalog) -> None:
    hit = recall("上个月各区域的成交总额", catalog, TODAY)
    assert hit.examples
    assert all(ex in [e for m in hit.metrics for e in (m.get("examples") or [])] for ex in hit.examples)


# ---------------------------------------------------------------------------
# 召回精度：四条来自评测基线的回归
# ---------------------------------------------------------------------------


def test_recall_longer_phrase_shadows_shorter_one(catalog: Catalog) -> None:
    """「卖了多少钱」里含「卖了多少」，不该因此把销量一起召回。"""
    hit = recall("本周卖了多少钱", catalog, TODAY)
    assert [m["name"] for m in hit.metrics] == ["gmv"]


def test_recall_multi_metric_question_keeps_all(catalog: Catalog) -> None:
    """多个互不覆盖的说法同时出现时，支配规则不能误删其中一个。"""
    hit = recall("上个月各区域的成交额、销量和订单量", catalog, TODAY)
    assert {m["name"] for m in hit.metrics} == {"gmv", "sales_volume", "order_count"}


def test_recall_colloquial_volume_maps_to_sales_volume(catalog: Catalog) -> None:
    for question in ("近三个月每月卖出多少件", "昨天卖出去多少货"):
        hit = recall(question, catalog, TODAY)
        assert [m["name"] for m in hit.metrics] == ["sales_volume"], question


def test_recall_time_word_does_not_become_dimension(catalog: Catalog) -> None:
    """相对时间已被换算成区间，不应再以「月」「季度」的形式出现为下钻维度。"""
    assert [d["name"] for d in recall("上个月各区域的销售额", catalog, TODAY).dimensions] == ["region"]
    assert [d["name"] for d in recall("我上个月进账多少", catalog, TODAY).dimensions] == []


def test_recall_all_time_words_are_stripped(catalog: Catalog) -> None:
    """一句话里出现两个时间词时，两个都要摘掉，漏一个就会串出月份维度。"""
    hit = recall("截止昨天这个月的出单量是多少", catalog, TODAY)
    assert [d["name"] for d in hit.dimensions] == []


def test_recall_explicit_month_drilldown_survives_stripping(catalog: Catalog) -> None:
    """用户明说「每月」时月份维度必须保留，不能和时间词一起被摘掉。"""
    hit = recall("近半年每月销售额走势", catalog, TODAY)
    assert "order_month" in {d["name"] for d in hit.dimensions}


def test_recall_trend_word_implies_time_dimension(catalog: Catalog) -> None:
    """问「走势」等于要求按时间展开，只是没说按月。"""
    hit = recall("近半年件单价走势", catalog, TODAY)
    assert "order_month" in {d["name"] for d in hit.dimensions}


def test_recall_value_synonym_pulls_its_dimension(catalog: Catalog) -> None:
    """说出具体枚举值时，字段对应的维度也要参与打分。"""
    assert "region" in {d["name"] for d in recall("江浙沪上个月卖了多少钱", catalog, TODAY).dimensions}
    hit = recall("上个季度华东区各品类的成交总额", catalog, TODAY)
    assert {d["name"] for d in hit.dimensions} == {"category_name", "region"}


# ---------------------------------------------------------------------------
# 上下文渲染
# ---------------------------------------------------------------------------


def test_render_context_contains_time_and_metric_sections(catalog: Catalog) -> None:
    text = render_context(recall("上个月各区域的成交总额", catalog, TODAY))
    assert "## 时间范围" in text
    assert "## 指标口径" in text
    assert "## 表结构" in text
    assert "orders.created_at" in text


def test_render_context_without_metric_says_so() -> None:
    from app.semantic.loader import SemanticHit

    text = render_context(SemanticHit())
    assert "本次未召回任何已登记指标" in text


def test_render_context_includes_value_hints(catalog: Catalog) -> None:
    text = render_context(recall("江浙沪的销量", catalog, TODAY))
    assert "## 说法对应" in text
    assert "华东" in text


def test_describe_catalog_counts(catalog: Catalog) -> None:
    overview = describe_catalog(catalog)
    assert overview["counts"]["metrics"] == len(catalog.metrics)
    assert overview["counts"]["tables"] == len(catalog.tables)
    assert {t["name"] for t in overview["tables"]} == catalog.table_names()
