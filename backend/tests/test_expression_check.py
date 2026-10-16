"""指标口径签名校验用例。

覆盖三件事：文本规范化（模型产出的空白写法不固定）、签名的两个判定分支
（必要成分 / 聚合纯度）、接入 validate 节点后的行为（不符时写进 fix_history
并置 sql_error，让 fix 回环接得住）。

边界用例比正向用例更要紧：口径校验一旦误报，会把本来正确的查询打回 fix 回环，
重试三次后给出「无法完成」——那比不校验更糟。因此「不该报」的用例占了一半。
"""

from __future__ import annotations

from app.agent.nodes.validate import validate_node
from app.agent.state import initial_state
from app.semantic.expression_check import (
    check_expression_signatures,
    normalize_sql,
    signatures_for,
)

GROSS_PROFIT_SIG = {
    "name": "gross_profit",
    "display": "毛利",
    "require_all": ["cost_price"],
    "require_plain_aggregate": {},
}

SALES_VOLUME_SIG = {
    "name": "sales_volume",
    "display": "销量",
    "require_all": ["quantity"],
    "require_plain_aggregate": {"exclude": ["unit_price", "cost_price"]},
}

SQL_WITH_COST = (
    "SELECT p.brand AS 品牌, SUM(oi.quantity * (oi.unit_price - p.cost_price)) AS 毛利 "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
    "JOIN products p ON p.id = oi.product_id "
    "WHERE o.status NOT IN ('cancelled', 'pending') GROUP BY p.brand LIMIT 1000"
)

SQL_WITHOUT_COST = (
    "SELECT p.brand AS 品牌, SUM(oi.quantity * oi.unit_price) AS 毛利 "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
    "JOIN products p ON p.id = oi.product_id "
    "WHERE o.status NOT IN ('cancelled', 'pending') GROUP BY p.brand LIMIT 1000"
)


# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------


def test_normalize_strips_space_inside_call() -> None:
    """`COUNT ( DISTINCT` 与 `COUNT(DISTINCT` 是同一个表达式。

    两侧空格都要吃：只吃函数名那一侧会留下 `count( distinct`，
    仍然匹配不上签名里的 `count(distinct`。
    """
    assert normalize_sql("COUNT ( DISTINCT o.id )") == "count(distinct o.id )"


def test_normalize_collapses_whitespace_and_lowercases() -> None:
    assert normalize_sql("SELECT  A\n  FROM\tB") == "select a from b"


# ---------------------------------------------------------------------------
# 必要成分
# ---------------------------------------------------------------------------


def test_require_all_missing_reports_metric_label() -> None:
    problems = check_expression_signatures(SQL_WITHOUT_COST, [GROSS_PROFIT_SIG])
    assert len(problems) == 1
    assert "毛利" in problems[0]
    assert "cost_price" in problems[0]


def test_require_all_present_passes() -> None:
    assert check_expression_signatures(SQL_WITH_COST, [GROSS_PROFIT_SIG]) == []


def test_require_all_is_case_insensitive() -> None:
    sql = "SELECT SUM(oi.quantity * (oi.unit_price - p.COST_PRICE)) FROM orders o LIMIT 10"
    assert check_expression_signatures(sql, [GROSS_PROFIT_SIG]) == []


# ---------------------------------------------------------------------------
# 聚合纯度
# ---------------------------------------------------------------------------


def test_plain_aggregate_catches_volume_written_as_amount() -> None:
    """销量写成 SUM(quantity * unit_price) 时，含 quantity 也救不了它。"""
    sql = (
        "SELECT SUM(oi.quantity * oi.unit_price) AS 销量 "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id LIMIT 1000"
    )
    problems = check_expression_signatures(sql, [SALES_VOLUME_SIG])
    assert len(problems) == 1
    assert "销量" in problems[0]


def test_plain_aggregate_passes_when_clean_sum_exists() -> None:
    sql = "SELECT SUM(oi.quantity) AS 销量 FROM orders o JOIN order_items oi ON oi.order_id = o.id LIMIT 1000"
    assert check_expression_signatures(sql, [SALES_VOLUME_SIG]) == []


def test_plain_aggregate_does_not_false_positive_on_mixed_metrics() -> None:
    """同一轮命中多个指标时不误报。

    问「各品类的销量和成交总额」时，语句里既有 SUM(quantity) 也有
    SUM(quantity * unit_price)。纯度判定取「存在一个干净聚合」而非
    「全部干净」，正是为了放行这种正常写法。
    """
    sql = (
        "SELECT c.name AS 品类, SUM(oi.quantity) AS 销量, "
        "SUM(oi.quantity * oi.unit_price) AS 成交总额 "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id LIMIT 1000"
    )
    assert check_expression_signatures(sql, [SALES_VOLUME_SIG]) == []


def test_missing_require_all_skips_plain_check() -> None:
    """必要成分都没出现时只报一条，不堆叠同源说明。"""
    sql = "SELECT COUNT(DISTINCT o.id) FROM orders o LIMIT 10"
    problems = check_expression_signatures(sql, [SALES_VOLUME_SIG])
    assert len(problems) == 1
    assert "quantity" in problems[0]


# ---------------------------------------------------------------------------
# 输入边界
# ---------------------------------------------------------------------------


def test_empty_sql_or_signatures_passes() -> None:
    assert check_expression_signatures("", [GROSS_PROFIT_SIG]) == []
    assert check_expression_signatures("   ", [GROSS_PROFIT_SIG]) == []
    assert check_expression_signatures(SQL_WITHOUT_COST, []) == []


def test_unparsable_sql_is_let_through() -> None:
    """解析失败不报错。

    语句在进本模块前已过 guard 的 AST 校验，正常路径必然可解析。
    这里不成为新的失败来源——放行交给后续环节。
    """
    assert check_expression_signatures("NOT SQL AT ALL (", [GROSS_PROFIT_SIG]) == []


# ---------------------------------------------------------------------------
# 从语义层摘取签名
# ---------------------------------------------------------------------------


def test_signatures_for_skips_metrics_without_signature() -> None:
    metrics = [
        {"name": "a", "display": "甲"},
        {
            "name": "b",
            "display": "乙",
            "expression_signature": {"require_all": ["x"]},
        },
    ]
    out = signatures_for(metrics)
    assert [s["name"] for s in out] == ["b"]
    assert out[0]["require_all"] == ["x"]
    assert out[0]["require_plain_aggregate"] == {}


def test_every_metric_declares_signature(catalog) -> None:
    """七个成交类指标都必须有签名。

    少一个就意味着该指标的口径漂移在本项目里无人把关，
    这是配置遗漏而不是功能取舍，所以断言为零容忍。
    """
    missing = [m["name"] for m in catalog.metrics if not m.get("expression_signature")]
    assert missing == []


# ---------------------------------------------------------------------------
# 接入 validate 节点
# ---------------------------------------------------------------------------


def test_validate_rejects_metric_drift(guard, catalog) -> None:
    """口径不符时置 sql_error 并记进 fix_history，而不是放行。"""
    target = [m for m in catalog.metrics if m["name"] == "gross_profit"]
    state = initial_state("s-drift", "按品牌看今年的毛利")
    state["sql"] = SQL_WITHOUT_COST
    state["retrieved"] = {"metric_signatures": signatures_for(target)}

    out = validate_node(state, guard=guard)

    assert out["sql_error"], "口径不符却放行了"
    assert "毛利" in out["sql_error"]
    assert out["fix_history"][-1]["stage"] == "validate"
    assert out["fix_history"][-1]["error"] == out["sql_error"]


def test_validate_accepts_correct_metric(guard, catalog) -> None:
    """口径正确的语句照常放行，不因新增校验而受阻。"""
    target = [m for m in catalog.metrics if m["name"] == "gross_profit"]
    state = initial_state("s-ok", "按品牌看今年的毛利")
    state["sql"] = SQL_WITH_COST
    state["retrieved"] = {"metric_signatures": signatures_for(target)}

    out = validate_node(state, guard=guard)

    assert out.get("sql_error") is None
    assert out["sql"] and "cost_price" in out["sql"]


def test_validate_without_signatures_behaves_as_before(guard) -> None:
    """未命中任何指标（retrieved 为空）时不触发口径校验。

    闲聊、取表结构这类轮次没有指标，签名清单为空是正常状态，
    不能因此判失败。
    """
    state = initial_state("s-plain", "看看订单表")
    state["sql"] = "SELECT o.id, o.region FROM orders o LIMIT 10"

    out = validate_node(state, guard=guard)

    assert out.get("sql_error") is None


# ---------------------------------------------------------------------------
# 语义层示例的自我一致性
# ---------------------------------------------------------------------------


def test_all_examples_pass_guard_and_signature(catalog, guard) -> None:
    """每条示例都要过守卫与自身指标的口径签名。

    示例是 few-shot 的范本，模型会照着它写。一条过不了守卫的示例等于在示范
    非法语句；一条不符合自身指标签名的示例等于在示范口径漂移——后者正是本模块
    要拦的那类错误，不能从示例里漏进来。新增示例后这条会自动覆盖。
    """
    problems: list[str] = []
    for metric in catalog.metrics:
        sigs = signatures_for([metric])
        for example in metric.get("examples") or []:
            label = f"{metric['name']} | {example.get('q')}"
            result = guard.check(example["sql"])
            if not result.ok:
                problems.append(f"{label}：守卫未通过 —— {result.error_text()}")
                continue
            found = check_expression_signatures(example["sql"], sigs)
            if found:
                problems.append(f"{label}：口径不符 —— " + " / ".join(found))
    assert problems == []


def test_every_metric_has_at_least_five_examples(catalog) -> None:
    """每个指标至少 5 条示例。

    单条示例覆盖不了「同一指标的不同问法」：问日粒度、问 TopN、问口语说法时
    该写的语句形态并不相同，示例池太薄会让 few-shot 只在某一种问法上生效。
    数量下降意味着覆盖面退化，因此作为下限断言而不是参考值。
    """
    thin = {
        metric["name"]: len(metric.get("examples") or [])
        for metric in catalog.metrics
        if len(metric.get("examples") or []) < 5
    }
    assert thin == {}

