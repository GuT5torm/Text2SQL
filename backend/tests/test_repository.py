"""数据访问层用例：执行期加固与会话仓储。

执行期的两条限制（R8 超时、行数上限）无法在守卫里做，只能在这里验证：
守卫看的是语句文本，超时与截断只有真正跑起来才知道生效没有。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.db.repository import (
    QueryError,
    QueryRepository,
    QueryTimeout,
    SessionStore,
    TurnRecord,
)
from app.db.sqlite_repo import SqliteSessionStore

# 递归 CTE，不中断就会一直算下去
_RUNAWAY_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
    "SELECT COUNT(*) AS n FROM c"
)


@pytest.fixture()
def env_settings() -> Iterator[object]:
    """临时改写 SQL 相关的配置项。进程内配置带 lru_cache，改完必须清缓存。"""

    saved: dict[str, str | None] = {}

    def _apply(**kwargs: float | int) -> object:
        for key, value in kwargs.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = str(value)
        get_settings.cache_clear()
        return get_settings()

    yield _apply

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 只读查询
# ---------------------------------------------------------------------------


def test_list_tables_excludes_sqlite_internals(business_repo: QueryRepository) -> None:
    tables = business_repo.list_tables()
    assert "orders" in tables
    assert not any(t.startswith("sqlite_") for t in tables)


def test_count_rows_matches_query(business_repo: QueryRepository) -> None:
    expected = business_repo.execute_readonly("SELECT COUNT(*) AS n FROM orders").rows[0]["n"]
    assert business_repo.count_rows("orders") == expected


def test_data_as_of_is_earlier_of_two_table_maxes(business_repo: QueryRepository) -> None:
    """锚点取两表较早的最大日期：任一张表没有数据的月份不能算可统计区间。"""
    as_of = business_repo.data_as_of()
    assert as_of is not None
    orders_max = business_repo.execute_readonly(
        "SELECT MAX(created_at) AS m FROM orders"
    ).rows[0]["m"]
    refunds_max = business_repo.execute_readonly(
        "SELECT MAX(created_at) AS m FROM refunds"
    ).rows[0]["m"]
    expected = min(str(orders_max)[:10], str(refunds_max)[:10])
    assert as_of.isoformat() == expected


def test_data_as_of_matches_health_endpoint(client, business_repo: QueryRepository) -> None:
    """/api/health 报的截止日与时间词锚点同一来源，两端必须对得上。"""
    health = client.get("/api/health").json()
    assert health["data_as_of"] == business_repo.data_as_of().isoformat()


@pytest.mark.parametrize("table", ["orders; DROP TABLE users", "orders ", "or-ders"])
def test_count_rows_rejects_illegal_table_name(business_repo: QueryRepository, table: str) -> None:
    with pytest.raises(ValueError, match="非法的表名"):
        business_repo.count_rows(table)


def test_execute_readonly_returns_columns_and_rows(business_repo: QueryRepository) -> None:
    result = business_repo.execute_readonly("SELECT region, COUNT(*) AS n FROM orders GROUP BY region")
    assert result.columns == ["region", "n"]
    assert result.row_count > 0
    assert set(result.rows[0]) == {"region", "n"}
    assert result.elapsed_ms >= 0


def test_max_rows_truncates(business_repo: QueryRepository) -> None:
    result = business_repo.execute_readonly("SELECT id FROM orders", max_rows=3)
    assert result.row_count == 3
    assert result.truncated is True


def test_default_max_rows_comes_from_settings(business_repo: QueryRepository) -> None:
    result = business_repo.execute_readonly("SELECT id FROM orders")
    assert result.row_count <= get_settings().sql_max_rows


def test_query_error_keeps_database_wording(business_repo: QueryRepository) -> None:
    """fix 节点靠原始报错定位问题，包装时不能把关键信息丢掉。"""
    with pytest.raises(QueryError) as exc:
        business_repo.execute_readonly("SELECT * FROM no_such_table")
    assert "no such table" in str(exc.value)


def test_connection_rejects_write(business_repo: QueryRepository) -> None:
    """只读连接是守卫之外的最后一道防线，绕过守卫也写不进去。"""
    with pytest.raises(QueryError):
        business_repo.execute_readonly("UPDATE orders SET status = 'paid'")


# ---------------------------------------------------------------------------
# R8 执行超时与扫描量上限
# ---------------------------------------------------------------------------


def test_r8_timeout_interrupts_runaway_query(
    business_repo: QueryRepository, env_settings: object
) -> None:
    # 扫描量上限要放宽，否则会先于超时触发（递归 CTE 不到一秒就能跑满默认上限）
    env_settings(SQL_TIMEOUT_SECONDS=0.5, SQL_SCAN_LIMIT=1_000_000_000)  # type: ignore[operator]
    with pytest.raises(QueryTimeout) as exc:
        business_repo.execute_readonly(_RUNAWAY_SQL)
    assert "被中断" in str(exc.value)
    assert "0.5 秒" in str(exc.value)


def test_r8_scan_limit_fires_before_default_timeout(
    business_repo: QueryRepository, env_settings: object
) -> None:
    """默认扫描量上限比 10 秒超时更早生效，这条用例记录的是实际优先级。"""
    env_settings(SQL_TIMEOUT_SECONDS=10)  # type: ignore[operator]
    with pytest.raises(QueryTimeout) as exc:
        business_repo.execute_readonly(_RUNAWAY_SQL)
    assert "扫描量" in str(exc.value)


def test_r8_scan_limit_interrupts_before_timeout(
    business_repo: QueryRepository, env_settings: object
) -> None:
    env_settings(SQL_TIMEOUT_SECONDS=30, SQL_SCAN_LIMIT=2000)  # type: ignore[operator]
    with pytest.raises(QueryTimeout) as exc:
        business_repo.execute_readonly("SELECT o.id, u.id FROM orders o, users u")
    assert "扫描量" in str(exc.value)


def test_r8_progress_handler_is_cleared_after_query(
    business_repo: QueryRepository, env_settings: object
) -> None:
    """中断过一次之后，后续正常查询不应继续被上次的 handler 拦下。"""
    env_settings(SQL_TIMEOUT_SECONDS=0.5)  # type: ignore[operator]
    with pytest.raises(QueryTimeout):
        business_repo.execute_readonly(_RUNAWAY_SQL)
    env_settings(SQL_TIMEOUT_SECONDS=10)  # type: ignore[operator]
    assert business_repo.execute_readonly("SELECT id FROM orders LIMIT 3").row_count == 3


# ---------------------------------------------------------------------------
# 会话仓储
# ---------------------------------------------------------------------------


def test_create_and_get_session(store: SessionStore) -> None:
    rec = store.create_session("测试会话")
    assert rec.id
    assert rec.title == "测试会话"
    assert rec.turn_count == 0

    fetched = store.get_session(rec.id)
    assert fetched is not None and fetched.id == rec.id
    assert store.get_session("not-exist") is None


def test_add_turn_backfills_primary_key(store: SessionStore) -> None:
    sid = store.create_session().id
    turn = TurnRecord(session_id=sid, question="上个月各区域的成交总额", answer="...")
    returned = store.add_turn(turn)

    assert returned > 0
    assert turn.id == returned, "回填后调用方应能直接拿到主键"


def test_list_turns_keeps_insert_order(store: SessionStore) -> None:
    sid = store.create_session().id
    for i in range(3):
        store.add_turn(TurnRecord(session_id=sid, question=f"问题{i}", answer=f"答案{i}"))

    turns = store.list_turns(sid)
    assert [t.question for t in turns] == ["问题0", "问题1", "问题2"]
    assert [t.id for t in turns] == sorted(t.id for t in turns)
    assert store.get_session(sid).turn_count == 3  # type: ignore[union-attr]


def test_first_question_becomes_session_title(store: SessionStore) -> None:
    sid = store.create_session().id
    store.add_turn(TurnRecord(session_id=sid, question="各品类的退款率", answer=""))
    assert store.get_session(sid).title == "各品类的退款率"  # type: ignore[union-attr]


def test_custom_title_is_not_overwritten(store: SessionStore) -> None:
    sid = store.create_session("我的分析").id
    store.add_turn(TurnRecord(session_id=sid, question="各品类的退款率"))
    assert store.get_session(sid).title == "我的分析"  # type: ignore[union-attr]


def test_rename_archive_and_delete(store: SessionStore) -> None:
    sid = store.create_session().id

    assert store.rename_session(sid, "新标题")
    assert store.get_session(sid).title == "新标题"  # type: ignore[union-attr]
    assert not store.rename_session("not-exist", "x")

    assert store.archive_session(sid)
    assert store.list_sessions() == []
    assert len(store.list_sessions(include_archived=True)) == 1

    assert store.delete_session(sid)
    assert store.get_session(sid) is None
    assert not store.delete_session(sid)


def test_rename_truncates_long_title(store: SessionStore) -> None:
    sid = store.create_session().id
    store.rename_session(sid, "长" * 200)
    assert len(store.get_session(sid).title) == 120  # type: ignore[union-attr]


def test_list_sessions_searches_question_text(store: SessionStore) -> None:
    sid = store.create_session("会话标题").id
    store.add_turn(TurnRecord(session_id=sid, question="华东区退款率", answer=""))

    assert [s.id for s in store.list_sessions(keyword="退款")] == [sid]
    assert [s.id for s in store.list_sessions(keyword="会话标题")] == [sid]
    assert store.list_sessions(keyword="不存在的词") == []


def test_turn_fields_roundtrip(store: SqliteSessionStore) -> None:
    sid = store.create_session().id
    store.add_turn(
        TurnRecord(
            session_id=sid,
            question="q",
            answer="a",
            sql="SELECT 1",
            metrics_json='{"metric_used":["gmv"]}',
            chart_json='{"chart_type":"bar"}',
            rows_json='{"columns":["n"],"rows":[{"n":1}],"row_count":1}',
            row_count=1,
            elapsed_ms=12.5,
            status="ok",
        )
    )
    turn = store.list_turns(sid)[0]
    assert turn.sql == "SELECT 1"
    assert turn.chart_json and "bar" in turn.chart_json
    assert turn.row_count == 1
    assert turn.elapsed_ms == 12.5
    assert turn.status == "ok"
