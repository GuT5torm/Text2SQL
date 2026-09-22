"""未收录维度检测的单测。

这类检测的特点是误报比漏报危害大：一旦把已知维度判成生词，正常查询的 prompt
与答案里就会多出一条错误的口径提示。所以用例里负例（不该报的）比正例多，
且负例覆盖到评测集里真实出现过的全部「各 X」句式。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.graph import _finalize_caveats  # noqa: E402
from app.semantic.dimension_check import (  # noqa: E402
    build_vocabulary,
    describe_substitution,
    detect_unsupported_dimensions,
    extract_dimension_terms,
    group_by_columns,
)
from app.semantic.loader import (  # noqa: E402
    UNSUPPORTED_CAVEAT_PREFIX,
    load_catalog,
    recall,
    render_context,
)


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def vocab(catalog):
    return catalog.vocabulary


# ---------------------------------------------------------------- 抽取


@pytest.mark.parametrize(
    "question, expected",
    [
        ("上个月各种支付方式的下单人数", ["支付方式"]),
        ("各区域的成交总额", ["区域"]),
        ("上周各城市的订单量", ["城市"]),
        ("不同会员等级的客单价", ["会员等级"]),
        ("每个区域的销售额如何", ["区域"]),
        ("按渠道分组统计销售额", ["渠道"]),
        # 抽词层不做剥离，"支付方式在去年" 会整段落进来；洗净交给 detect 层
        ("各种支付方式在去年的分布", ["支付方式在去年"]),
    ],
)
def test_extract_dimension_terms(question, expected):
    assert extract_dimension_terms(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "近半年每月销售额走势",  # 时间粒度不是业务维度
        "近7天每天的成交总额",
        "你好呀",
        "上个月成交了多少",
    ],
)
def test_extract_returns_empty_without_group_intent(question):
    assert extract_dimension_terms(question) == []


# ---------------------------------------------------------------- 判定


@pytest.mark.parametrize(
    "question, expected",
    [
        ("上个月各种支付方式的下单人数", ["支付方式"]),
        ("各大促活动的销量", ["大促活动"]),
        ("每个快递公司的妥投时长", ["快递公司"]),
        # 相邻的已知词会被从抽词结果里剥掉，只剩真正库里没有的那一段
        ("各种支付方式在去年的分布", ["支付方式"]),
    ],
)
def test_detects_unknown_dimensions(question, expected, catalog, vocab):
    assert detect_unsupported_dimensions(question, catalog, vocab) == expected


@pytest.mark.parametrize(
    "question",
    [
        "各区域的成交总额",
        "上周各城市的订单量",
        "上个月各会员等级的客单价",
        "各品类今年的毛利",
        "各渠道销售额占比",
        "每个用户的平均下单次数",
        "近7天每天的成交总额",
        "今年以来各区域的退款率",
    ],
)
def test_known_dimensions_not_flagged(question, catalog, vocab):
    """负例。全部取自评测集的真实问句，误报会直接在 pipeline 组暴露。"""
    assert detect_unsupported_dimensions(question, catalog, vocab) == []


def test_glued_terms_not_flagged_when_every_part_is_known(catalog, vocab):
    """「品类退款率」整段不在词汇池里，但每一段都认识，不该判成新维度。"""
    assert detect_unsupported_dimensions("各品类退款率", catalog, vocab) == []


def test_empty_vocabulary_is_never_flagged(catalog):
    """手工构造的 Catalog 没有词汇池时一律不判定，否则每个词都会被当生词。"""
    assert detect_unsupported_dimensions("各种支付方式的人数", catalog, set()) == []
    assert detect_unsupported_dimensions("各区域的成交额", catalog, set()) == []


def test_vocabulary_excludes_single_char_entries(catalog):
    """单字条目参与包含判定会制造虚假命中，加载时应已过滤。"""
    assert all(len(v) >= 2 for v in build_vocabulary(catalog))


# ---------------------------------------------------------------- SQL 侧


def test_group_by_columns_from_group_clause():
    sql = "SELECT o.channel AS 支付方式, COUNT(*) n FROM orders o GROUP BY o.channel"
    assert group_by_columns(sql) == ["o.channel"]


def test_group_by_columns_falls_back_to_non_aggregate_projection():
    """没有 GROUP BY 时退到 SELECT 里的非聚合列，跳过计数列。"""
    sql = "SELECT c.name AS 品类, SUM(oi.amount) AS 成交额 FROM order_items oi JOIN products p GROUP BY c.name"
    assert group_by_columns(sql) == ["c.name"]


def test_group_by_columns_reads_through_cte():
    """CTE 的真实 SELECT 挂在 this 上；外层自己又有 GROUP BY 时按外层的取。"""
    sql = (
        "WITH base AS (SELECT channel, user_id FROM orders) "
        "SELECT channel, COUNT(DISTINCT user_id) n FROM base GROUP BY channel"
    )
    assert group_by_columns(sql) == ["channel"]


def test_group_by_columns_caps_at_three_columns():
    sql = "SELECT a, b, c, d, COUNT(*) n FROM t GROUP BY a, b, c, d"
    assert len(group_by_columns(sql)) == 3


@pytest.mark.parametrize("sql", ["", "   ", "这根本不是 SQL"])
def test_group_by_columns_tolerates_unparsable(sql):
    assert group_by_columns(sql) == []


def test_describe_substitution_names_the_actual_column():
    sql = "SELECT o.channel AS 支付方式, COUNT(*) n FROM orders o GROUP BY o.channel"
    note = describe_substitution(sql, ["支付方式"])
    assert note is not None
    assert note.startswith(UNSUPPORTED_CAVEAT_PREFIX)
    assert "o.channel" in note
    assert "支付方式" in note


def test_describe_substitution_without_unknown_dimension():
    assert describe_substitution("SELECT 1", []) is None


def test_describe_substitution_without_groupable_sql():
    """拿不到分组字段就不写死批量提醒，避免给出没法核对的话。"""
    assert describe_substitution("SELECT COUNT(*) AS n FROM orders", ["支付方式"]) is None


# ---------------------------------------------------------------- 收口


def test_finalize_caveats_replaces_generic_note_with_specific_one():
    state = {
        "retrieved": {
            "caveats": [f"{UNSUPPORTED_CAVEAT_PREFIX}「支付方式」：本库语义层与表结构中均无对应字段"],
            "unsupported_dimensions": ["支付方式"],
        },
        "sql": "SELECT o.channel, COUNT(*) n FROM orders o GROUP BY o.channel",
    }
    out = _finalize_caveats(state)
    assert len(out) == 1
    assert out[0].startswith(UNSUPPORTED_CAVEAT_PREFIX)
    assert "o.channel" in out[0]


def test_finalize_caveats_keeps_generic_note_when_sql_has_no_grouping():
    generic = f"{UNSUPPORTED_CAVEAT_PREFIX}「支付方式」：本库语义层与表结构中均无对应字段"
    state = {
        "retrieved": {
            "caveats": [generic],
            "unsupported_dimensions": ["支付方式"],
        },
        "sql": "SELECT COUNT(*) AS n FROM orders",
    }
    assert _finalize_caveats(state) == [generic]


def test_finalize_caveats_passes_through_when_nothing_unknown():
    caveats = ["成交总额：不含已取消订单"]
    state = {"retrieved": {"caveats": caveats, "unsupported_dimensions": []}, "sql": ""}
    assert _finalize_caveats(state) == caveats


def test_finalize_caveats_preserves_other_caveats():
    other = "成交用户数：同一用户多次下单只计一次"
    state = {
        "retrieved": {
            "caveats": [other, f"{UNSUPPORTED_CAVEAT_PREFIX}「支付方式」：无对应字段"],
            "unsupported_dimensions": ["支付方式"],
        },
        "sql": "SELECT o.channel, COUNT(*) n FROM orders o GROUP BY o.channel",
    }
    out = _finalize_caveats(state)
    assert other in out
    assert sum(1 for c in out if c.startswith(UNSUPPORTED_CAVEAT_PREFIX)) == 1


# ---------------------------------------------------------------- 集成


def test_recall_fills_unsupported_dimensions(catalog):
    hit = recall("上个月各种支付方式的下单人数", catalog)
    assert hit.unsupported_dimensions == ["支付方式"]
    assert any(c.startswith(UNSUPPORTED_CAVEAT_PREFIX) for c in hit.caveats())


def test_render_context_warns_against_substitution(catalog):
    hit = recall("上个月各种支付方式的下单人数", catalog)
    context = render_context(hit)
    assert "本库未收录的维度" in context
    assert "不要用语义相近的其他字段顶替" in context


def test_render_context_stays_clean_for_supported_dimensions(catalog):
    hit = recall("上个月各区域的成交总额", catalog)
    assert "本库未收录的维度" not in render_context(hit)
    assert not [c for c in hit.caveats() if c.startswith(UNSUPPORTED_CAVEAT_PREFIX)]


def test_group_by_columns_skips_literal_dimension_columns():
    """`'未知' AS 支付方式` 是模型承认维度缺位后的写法，不是分组依据。

    把它当成真实分组列会让提示变成「结果按 '未知' AS 支付方式 分组」，
    一句不知所云的话，还不如不提示。
    """
    sql = "SELECT '未知' AS 支付方式, COUNT(DISTINCT user_id) n FROM orders"
    assert group_by_columns(sql) == []
    assert describe_substitution(sql, ["支付方式"]) is None
