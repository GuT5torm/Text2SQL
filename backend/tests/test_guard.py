"""SQL 守卫用例：R1–R7 每条规则各一组正反例。

用例断言的是设计文档里写明的规则口径，不是当前实现的行为——两者不一致时
以规则为准，改实现而不是改断言。守卫的字段白名单来自真实语义层（conftest 的
guard 夹具），因此语义层配置漂移也会在这里暴露。
"""

from __future__ import annotations

import pytest

from app.db.guard import GuardResult, SqlGuard, strip_sql_fence


def errors_of(result: GuardResult) -> str:
    return result.error_text()


# ---------------------------------------------------------------------------
# R1 单条语句
# ---------------------------------------------------------------------------


def test_r1_accepts_single_statement(guard: SqlGuard) -> None:
    assert guard.check("SELECT id FROM orders LIMIT 5").ok


def test_r1_rejects_multiple_statements(guard: SqlGuard) -> None:
    result = guard.check("SELECT id FROM orders LIMIT 5; DROP TABLE orders")
    assert not result.ok
    assert "只允许单条查询" in errors_of(result)


def test_r1_rejects_empty_sql(guard: SqlGuard) -> None:
    result = guard.check("   ")
    assert not result.ok
    assert "为空" in errors_of(result)


def test_r1_rejects_unparsable_sql(guard: SqlGuard) -> None:
    result = guard.check("SELECT FROM WHERE")
    assert not result.ok


# ---------------------------------------------------------------------------
# R2 只允许查询类语句
# ---------------------------------------------------------------------------


def test_r2_accepts_select(guard: SqlGuard) -> None:
    assert guard.check("SELECT region FROM orders LIMIT 5").ok


def test_r2_accepts_union(guard: SqlGuard) -> None:
    sql = "SELECT region FROM orders UNION SELECT name FROM categories LIMIT 5"
    assert guard.check(sql).ok


@pytest.mark.parametrize("sql", ["UPDATE orders SET status = 'paid'", "DELETE FROM orders WHERE id = 1"])
def test_r2_rejects_dml(guard: SqlGuard, sql: str) -> None:
    result = guard.check(sql)
    assert not result.ok
    assert "只允许 SELECT 查询" in errors_of(result)


# ---------------------------------------------------------------------------
# R3 语句树中不得出现危险节点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE orders",
        "INSERT INTO orders (id) VALUES (1)",
        "ATTACH DATABASE 'other.db' AS other",
        "PRAGMA table_info(orders)",
        "VACUUM",
    ],
)
def test_r3_rejects_forbidden_nodes(guard: SqlGuard, sql: str) -> None:
    result = guard.check(sql)
    assert not result.ok
    assert "被禁止的" in errors_of(result)


def test_r3_rejects_destructive_statement_hidden_in_cte(guard: SqlGuard) -> None:
    """危险节点藏进 CTE 也要拦下。find_all 遍历整棵树，不是只看最外层。"""
    result = guard.check("WITH x AS (DELETE FROM orders RETURNING id) SELECT * FROM x")
    assert not result.ok
    assert "DELETE" in errors_of(result)


# ---------------------------------------------------------------------------
# R4 表必须在语义层登记
# ---------------------------------------------------------------------------


def test_r4_accepts_registered_table(guard: SqlGuard) -> None:
    result = guard.check("SELECT id, region FROM orders LIMIT 5")
    assert result.ok
    assert result.tables == ["orders"]


def test_r4_rejects_unknown_table(guard: SqlGuard) -> None:
    result = guard.check("SELECT * FROM customer_info LIMIT 5")
    assert not result.ok
    assert "未在语义层登记" in errors_of(result)


def test_r4_rejects_foreign_database(guard: SqlGuard) -> None:
    result = guard.check("SELECT * FROM other_db.orders LIMIT 5")
    assert not result.ok
    assert "不允许访问 other_db 库" in errors_of(result)


def test_r4_accepts_main_prefix(guard: SqlGuard) -> None:
    assert guard.check("SELECT * FROM main.orders LIMIT 5").ok


def test_r4_treats_cte_alias_as_not_table(guard: SqlGuard) -> None:
    """CTE 名不是真实表，否则 WITH 子句会被当成越权访问。"""
    result = guard.check("WITH x AS (SELECT id FROM orders) SELECT * FROM x LIMIT 5")
    assert result.ok
    assert result.tables == ["orders"]


def test_r4_treats_subquery_alias_as_not_table(guard: SqlGuard) -> None:
    sql = "SELECT t.n FROM (SELECT COUNT(*) AS n FROM orders) AS t LIMIT 5"
    assert guard.check(sql).ok


# ---------------------------------------------------------------------------
# R5 字段必须存在于所属表
# ---------------------------------------------------------------------------


def test_r5_accepts_known_column(guard: SqlGuard) -> None:
    result = guard.check("SELECT region, COUNT(*) AS n FROM orders GROUP BY region LIMIT 5")
    assert result.ok
    assert "orders.region" in result.columns


def test_r5_rejects_unknown_bare_column(guard: SqlGuard) -> None:
    result = guard.check("SELECT region_name FROM orders LIMIT 5")
    assert not result.ok
    assert "未在任何被引用的表中登记" in errors_of(result)


def test_r5_rejects_unknown_qualified_column(guard: SqlGuard) -> None:
    result = guard.check("SELECT orders.foo FROM orders LIMIT 5")
    assert not result.ok
    assert "在表 orders 中不存在" in errors_of(result)


def test_r5_rejects_unknown_column_under_alias(guard: SqlGuard) -> None:
    result = guard.check("SELECT x.foo FROM orders x LIMIT 5")
    assert not result.ok
    assert "在表 orders 中不存在" in errors_of(result)


def test_r5_rejects_unknown_qualifier(guard: SqlGuard) -> None:
    result = guard.check("SELECT z.id FROM orders LIMIT 5")
    assert not result.ok
    assert "未知的表或别名" in errors_of(result)


def test_r5_accepts_projection_alias_in_order_by(guard: SqlGuard) -> None:
    """ORDER BY 引用投影别名是常见写法，不能当成未登记字段。"""
    sql = "SELECT region, COUNT(*) AS 订单数 FROM orders GROUP BY region ORDER BY 订单数 DESC LIMIT 5"
    assert guard.check(sql).ok


def test_r5_suggests_similar_column(guard: SqlGuard) -> None:
    result = guard.check("SELECT regionn FROM orders LIMIT 5")
    assert "是否为" in errors_of(result)


# ---------------------------------------------------------------------------
# R6 强制 LIMIT
# ---------------------------------------------------------------------------


def test_r6_keeps_existing_limit(guard: SqlGuard) -> None:
    sql = "SELECT id FROM orders LIMIT 5"
    result = guard.check(sql)
    assert result.ok
    assert result.limit_applied is None
    assert not result.rewritten
    assert result.sql == sql


def test_r6_wraps_when_limit_missing(guard: SqlGuard) -> None:
    result = guard.check("SELECT id FROM orders")
    assert result.ok
    assert result.limit_applied == guard.max_rows
    assert result.rewritten
    assert f"LIMIT {guard.max_rows}" in result.sql
    assert result.warnings


def test_r6_wraps_when_limit_exceeds_max(guard: SqlGuard) -> None:
    result = guard.check("SELECT id FROM orders LIMIT 99999")
    assert result.limit_applied == guard.max_rows


def test_r6_boundary_limit_equal_to_max(small_guard: SqlGuard) -> None:
    """LIMIT 恰好等于上限时不改写，边界必须落在允许侧。"""
    result = small_guard.check("SELECT id FROM orders LIMIT 10")
    assert result.ok
    assert result.limit_applied is None


def test_r6_boundary_limit_one_over_max(small_guard: SqlGuard) -> None:
    result = small_guard.check("SELECT id FROM orders LIMIT 11")
    assert result.limit_applied == 10


def test_r6_preserves_order_by_after_wrapping(guard: SqlGuard) -> None:
    """包裹成外层 SELECT 时，原语句的 ORDER BY 必须在内层生效而不是被丢弃。"""
    result = guard.check("SELECT id FROM orders ORDER BY created_at DESC")
    assert "ORDER BY" in result.sql
    assert result.sql.index("ORDER BY") < result.sql.index("LIMIT")


# ---------------------------------------------------------------------------
# R7 多表必须有连接条件
# ---------------------------------------------------------------------------


def test_r7_accepts_join_with_on(guard: SqlGuard) -> None:
    sql = "SELECT o.id FROM orders o JOIN users u ON o.user_id = u.id LIMIT 5"
    assert guard.check(sql).ok


def test_r7_accepts_join_with_using(guard: SqlGuard) -> None:
    sql = "SELECT o.id FROM orders o JOIN users u USING (id) LIMIT 5"
    assert guard.check(sql).ok


def test_r7_rejects_join_without_condition(guard: SqlGuard) -> None:
    """sqlglot 读 sqlite 方言时会把缺 ON 的 JOIN 补成 ON TRUE，不能只看 on 是否为空。"""
    result = guard.check("SELECT o.id FROM orders o JOIN users u LIMIT 5")
    assert not result.ok
    assert "缺少 ON 条件" in errors_of(result)


def test_r7_rejects_explicit_on_true(guard: SqlGuard) -> None:
    result = guard.check("SELECT o.id FROM orders o JOIN users u ON TRUE LIMIT 5")
    assert not result.ok
    assert "缺少 ON 条件" in errors_of(result)


def test_r7_accepts_comma_join_with_where(guard: SqlGuard) -> None:
    """FROM a, b WHERE a.x = b.y 是合法写法，不能因为被解析成 CROSS JOIN 就一律拒绝。"""
    sql = "SELECT * FROM orders, users WHERE orders.user_id = users.id LIMIT 5"
    assert guard.check(sql).ok


def test_r7_accepts_comma_join_with_alias_in_where(guard: SqlGuard) -> None:
    sql = "SELECT * FROM orders o, users u WHERE o.user_id = u.id LIMIT 5"
    assert guard.check(sql).ok


def test_r7_rejects_comma_join_without_where(guard: SqlGuard) -> None:
    result = guard.check("SELECT * FROM orders, users LIMIT 5")
    assert not result.ok
    assert "笛卡尔积" in errors_of(result)


def test_r7_requires_all_tables_linked(guard: SqlGuard) -> None:
    """三表只连了两张，第三张仍是自由连接，必须拦下。"""
    sql = "SELECT * FROM orders o, users u, refunds r WHERE o.user_id = u.id LIMIT 5"
    result = guard.check(sql)
    assert not result.ok
    assert "笛卡尔积" in errors_of(result)


def test_r7_accepts_three_tables_fully_linked(guard: SqlGuard) -> None:
    sql = (
        "SELECT * FROM orders o, users u, refunds r "
        "WHERE o.user_id = u.id AND r.order_id = o.id LIMIT 5"
    )
    assert guard.check(sql).ok


def test_r7_allows_single_table(guard: SqlGuard) -> None:
    assert guard.check("SELECT COUNT(*) AS n FROM orders LIMIT 5").ok


# ---------------------------------------------------------------------------
# 输入形态与整体行为
# ---------------------------------------------------------------------------


def test_strip_sql_fence_removes_markdown_wrapper() -> None:
    assert strip_sql_fence("```sql\nSELECT 1\n```") == "SELECT 1"


def test_fenced_sql_passes(guard: SqlGuard) -> None:
    result = guard.check("```sql\nSELECT id FROM orders LIMIT 1\n```")
    assert result.ok


def test_all_metric_examples_pass_guard(guard: SqlGuard, catalog) -> None:
    """指标示例是喂给模型的范文，任何一条过不了守卫都会污染生成结果。"""
    total = 0
    for metric in catalog.metrics:
        for example in metric.get("examples") or []:
            total += 1
            result = guard.check(example["sql"])
            assert result.ok, f"{metric['name']} 的示例未通过守卫：{result.error_text()}"
    assert total > 0


def test_guard_reports_tables_and_columns(guard: SqlGuard) -> None:
    result = guard.check("SELECT o.region FROM orders o LIMIT 5")
    assert result.tables == ["orders"]
    assert result.columns == ["orders.region"]
