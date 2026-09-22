"""第三批（FR-H-2 + FR-E-1 + FR-D-1 + FR-D-2 + FR-C-1）的单测。

四个落点：
  1. execute._extract_source_tables：解析 FROM/JOIN 的表名，含 CTE / 子查询
     别名去重，保序
  2. _freshness.compute_data_freshness：库内 MAX + 配置覆盖两条路径，
     阈值与 is_stale 判定
  3. sqlite_repo._ensure_turn_columns：「旧库 + 新代码」迁移 9 列，且
     重复执行不报错
  4. SqliteSessionStore.add_turn / list_turns：9 个新字段的往返一致性
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.agent.nodes._freshness import compute_data_freshness
from app.agent.nodes.execute import _extract_source_tables
from app.config import get_settings
from app.db.repository import QueryRepository, TurnRecord
from app.db.sqlite_repo import (
    SqliteSessionStore,
    _TURN_MIGRATIONS,
    _ensure_turn_columns,
    dump_json,
)


# ---------------------------------------------------------------------------
# execute._extract_source_tables
# ---------------------------------------------------------------------------


class TestExtractSourceTables:
    """表名解析：去重 + 保序 + CTE / 子查询别名处理。"""

    def test_simple_from(self) -> None:
        sql = "SELECT * FROM orders"
        assert _extract_source_tables(sql) == ["orders"]

    def test_join_with_alias(self) -> None:
        sql = "SELECT o.id FROM orders o JOIN refunds r ON r.order_id = o.id"
        # 别名 o / r 不应被当作表名
        assert _extract_source_tables(sql) == ["orders", "refunds"]

    def test_preserves_first_seen_order(self) -> None:
        # 出现顺序在多次引用时按首次出现
        sql = (
            "SELECT u.id, o.id "
            "FROM orders o JOIN users u ON u.id = o.user_id "
            "LEFT JOIN refunds r ON r.order_id = o.id"
        )
        assert _extract_source_tables(sql) == ["orders", "users", "refunds"]

    def test_deduplicates_repeated_table(self) -> None:
        sql = (
            "SELECT o.id FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.id "
            "JOIN order_items oi2 ON oi2.order_id = o.id"
        )
        # order_items 出现两次只保留一次
        result = _extract_source_tables(sql)
        assert result.count("order_items") == 1
        assert "orders" in result

    def test_cte_name_excluded(self) -> None:
        # CTE 名「c」不应被认作表名；真实查询中的「orders」应当保留
        sql = (
            "WITH c AS (SELECT region, SUM(amount) AS s FROM orders GROUP BY region) "
            "SELECT * FROM c"
        )
        result = _extract_source_tables(sql)
        # 「orders」是 CTE 内部访问的表，应当出现在结果中；「c」不出现
        assert "orders" in result
        assert "c" not in result

    def test_subquery_alias_excluded(self) -> None:
        sql = (
            "SELECT t.region FROM "
            "(SELECT region, COUNT(*) AS n FROM orders GROUP BY region) t"
        )
        # 子查询别名 t 不出现；orders 出现
        result = _extract_source_tables(sql)
        assert "orders" in result
        assert "t" not in result

    def test_empty_sql_returns_empty(self) -> None:
        assert _extract_source_tables("") == []

    def test_invalid_sql_returns_empty_not_raises(self) -> None:
        # 解析失败时返回空列表而不是抛异常，execute 节点已在 try/except 内
        assert _extract_source_tables("SELECT FROM WHERE") == []

    def test_four_table_join(self) -> None:
        sql = (
            "SELECT c.name "
            "FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.id "
            "JOIN products p ON p.id = oi.product_id "
            "JOIN categories c ON c.id = p.category_id"
        )
        assert _extract_source_tables(sql) == [
            "orders", "order_items", "products", "categories"
        ]


# ---------------------------------------------------------------------------
# _freshness.compute_data_freshness
# ---------------------------------------------------------------------------


class TestDataFreshness:
    """数据时效：库内 MAX 与配置覆盖两条路径。"""

    @pytest.fixture()
    def env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """进出都清 settings 缓存，避免其他用例读到污染值。"""
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_uses_library_max_by_default(
        self, business_repo: QueryRepository, env_overrides: None
    ) -> None:
        monkeypatch_ = pytest.MonkeyPatch()
        monkeypatch_.setenv("DATA_CUTOFF_OVERRIDE", "")
        try:
            result = compute_data_freshness(business_repo)
        finally:
            monkeypatch_.undo()
        # 默认从库内取，应给出 ISO 格式日期
        assert result["source"] == "library_max"
        for key in ("orders_cutoff", "refunds_cutoff"):
            assert isinstance(result[key], str)
            assert _is_iso(result[key]), f"{key} 不是 ISO 日期：{result[key]!r}"
        assert result["threshold_days"] == 7  # 默认阈值

    def test_config_override_takes_precedence(
        self, business_repo: QueryRepository, env_overrides: None
    ) -> None:
        monkeypatch_ = pytest.MonkeyPatch()
        monkeypatch_.setenv("DATA_CUTOFF_OVERRIDE", "2026-08-01")
        try:
            result = compute_data_freshness(business_repo)
        finally:
            monkeypatch_.undo()
        # 配置覆盖时两表 cutoff 都按配置值取
        assert result["source"] == "config_override"
        assert result["orders_cutoff"] == "2026-08-01"
        assert result["refunds_cutoff"] == "2026-08-01"

    def test_is_stale_when_age_exceeds_threshold(
        self, business_repo: QueryRepository, env_overrides: None
    ) -> None:
        # 用 0 天阈值强制触发 is_stale
        monkeypatch_ = pytest.MonkeyPatch()
        monkeypatch_.setenv("DATA_STALENESS_DAYS", "0")
        # cutoff 必须钉死为一个过去的日期，不能依赖业务库的真实截止日。
        # 2026-09-22 把演示数据平移到当天后，这条用例原本的假设（库里的数据
        # 一定不是今天的）不再成立，age_days=0 时 0 阈值也不会判 stale。
        # 固定 cutoff 后，本用例只考察阈值比较本身。
        monkeypatch_.setenv("DATA_CUTOFF_OVERRIDE", "2026-08-01")
        try:
            result = compute_data_freshness(business_repo)
        finally:
            monkeypatch_.undo()
        # 任何非今天的 cutoff 都会被 0 阈值判为 stale
        assert result["is_stale"] is True
        assert result["threshold_days"] == 0

    def test_note_surfaces_orders_refunds_gap(
        self, business_repo: QueryRepository, env_overrides: None
    ) -> None:
        result = compute_data_freshness(business_repo)
        # 实测两表相差 1 天（refunds 早于 orders 1 天），note 应包含这一描述
        # 不锁文案，只验关键事实
        orders_age = result["orders_age_days"]
        refunds_age = result["refunds_age_days"]
        if orders_age is not None and refunds_age is not None and abs(orders_age - refunds_age) >= 1:
            assert "天" in result["note"]


def _is_iso(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 迁移：旧库 + 新代码
# ---------------------------------------------------------------------------


class TestTurnColumnMigration:
    """验证 _ensure_turn_columns 的迁移行为。

    旧库只有 7 个核心字段。新代码启动后 9 个新字段都应被 ALTER TABLE
    加上；再次调用时应幂等（不重复加）。
    """

    @pytest.fixture()
    def old_schema_db(self, tmp_path) -> Iterator[Engine]:
        """手工建一张只有 7 列的 turns 表，模拟升级前状态。"""
        path = tmp_path / "old_app.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT,
                    sql TEXT
                )
                """
            ))
        yield engine
        engine.dispose()

    def test_adds_all_nine_columns_to_old_db(self, old_schema_db: Engine) -> None:
        _ensure_turn_columns(old_schema_db)

        with old_schema_db.connect() as conn:
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(turns)")).fetchall()
            }
        expected = {name for name, _ in _TURN_MIGRATIONS}
        assert expected.issubset(cols), f"缺失列：{expected - cols}"

    def test_is_idempotent(self, old_schema_db: Engine) -> None:
        # 第一次加列，第二次不应抛「duplicate column」
        _ensure_turn_columns(old_schema_db)
        _ensure_turn_columns(old_schema_db)  # 不抛异常

        with old_schema_db.connect() as conn:
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(turns)")).fetchall()
            }
        # 仍然只有 7 + 9 = 16 列，没有重复
        assert len(cols) == 7 + len(_TURN_MIGRATIONS)

    def test_no_op_on_fresh_db(self, tmp_path) -> None:
        """空库或已迁移库：再次调用不应报错也不应新增列。"""
        path = tmp_path / "fresh_app.db"
        engine = create_engine(f"sqlite:///{path}")
        # 用 Base 一次性建齐
        from app.db.models import Base

        Base.metadata.create_all(engine)
        with engine.connect() as conn:
            cols_before = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(turns)")).fetchall()
            }
        _ensure_turn_columns(engine)
        with engine.connect() as conn:
            cols_after = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(turns)")).fetchall()
            }
        assert cols_before == cols_after


# ---------------------------------------------------------------------------
# 新字段的写入与回放
# ---------------------------------------------------------------------------


class TestTurnRecordRoundTrip:
    """TurnRecord 的 9 个新字段在 add_turn / list_turns 之间保持一致。"""

    def test_write_then_list_preserves_new_fields(
        self, store: SqliteSessionStore, business_repo: QueryRepository
    ) -> None:
        sid = store.create_session().id
        record = TurnRecord(
            session_id=sid,
            question="上个月各区域的成交总额",
            answer="ok",
            sql="SELECT region FROM orders",
            original_sql="SELECT region FROM orders",
            stages_json=dump_json([
                {"node": "route", "label": "路由", "detail": "ok"},
                {"node": "execute", "label": "执行", "detail": "ok"},
            ]),
            guard_warnings_json=dump_json(["已包裹为最多返回 1000 行"]),
            retry_count=1,
            truncated=1,
            persist_row_count=42,
            source_tables_json=dump_json(["orders", "refunds"]),
            findings_json=dump_json([{"label": "x"}]),
            baselines_json=dump_json({"prev": 1.0}),
            node_path=dump_json(["route", "execute"]),
        )
        new_id = store.add_turn(record)

        listed = store.list_turns(sid)
        assert len(listed) == 1
        got = listed[0]

        # 字段值在往返过程中完全一致
        assert got.id == new_id
        assert got.original_sql == "SELECT region FROM orders"
        assert got.stages_json is not None
        assert json.loads(got.stages_json) == [
            {"node": "route", "label": "路由", "detail": "ok"},
            {"node": "execute", "label": "执行", "detail": "ok"},
        ]
        assert got.guard_warnings_json is not None
        assert "已包裹" in got.guard_warnings_json
        assert got.retry_count == 1
        assert got.truncated == 1
        assert got.persist_row_count == 42
        assert got.source_tables_json is not None
        assert json.loads(got.source_tables_json) == ["orders", "refunds"]
        assert got.findings_json is not None
        assert json.loads(got.findings_json) == [{"label": "x"}]
        assert got.baselines_json is not None
        assert json.loads(got.baselines_json) == {"prev": 1.0}
        assert got.node_path is not None
        assert json.loads(got.node_path) == ["route", "execute"]

    def test_defaults_for_legacy_records(
        self, store: SqliteSessionStore
    ) -> None:
        """只填核心字段的旧记录：list_turns 返回时新字段走默认，不抛异常。"""
        sid = store.create_session().id
        store.add_turn(TurnRecord(
            session_id=sid,
            question="旧记录",
            answer="ok",
        ))
        listed = store.list_turns(sid)
        assert len(listed) == 1
        got = listed[0]
        # 9 个新字段都走默认（None / 0）
        assert got.stages_json is None
        assert got.original_sql is None
        assert got.guard_warnings_json is None
        assert got.retry_count == 0
        assert got.truncated == 0
        assert got.persist_row_count == 0
        assert got.source_tables_json is None
        assert got.findings_json is None
        assert got.baselines_json is None
        assert got.node_path is None