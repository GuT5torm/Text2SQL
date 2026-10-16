"""审计日志用例。

断言的重点不是「能不能写进去」，而是审计与业务留痕（turns 表）的分界线是否
真的成立：

  - 被守卫拒绝、从未执行的语句也要能查到（turns 不记）；
  - 会话删除后审计仍在（turns 会级联消失）；
  - 审计写失败不影响主链路（节点照常返回）；
  - 关闭时彻底静默（不写库也不写日志）。

接口层的断言另有一组：审计不可用时必须报错，不能返回空列表——空列表会被
误读成「这段时间没有拦截」。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

import pytest

from app.agent.nodes.execute import execute_node
from app.agent.nodes.validate import validate_node
from app.agent.state import initial_state
from app.api.deps import get_container
from app.audit.events import (
    EVENT_GUARD_BLOCK,
    EVENT_GUARD_PASS,
    EVENT_GUARD_REWRITE,
    EVENT_METRIC_BLOCK,
    EVENT_SQL_ERROR,
    EVENT_SQL_EXECUTE,
    OUTCOME_DENIED,
    OUTCOME_ERROR,
    OUTCOME_OK,
    AuditEvent,
)
from app.audit.recorder import (
    AuditRecorder,
    configure_recorder,
    get_recorder,
    reset_recorder,
)
from app.audit.sinks import AuditSink, MemorySink, NullSink
from app.db.audit_repo import SqliteAuditRepository
from app.db.engine import create_app_engine
from app.db.repository import (
    QueryError,
    QueryRepository,
    QueryResult,
    TurnRecord,
)
from app.db.sqlite_repo import SqliteSessionStore

BLOCKED_SQL = "SELECT * FROM secret_ledger LIMIT 5"
PLAIN_SQL = "SELECT id FROM orders LIMIT 5"
NO_LIMIT_SQL = "SELECT id FROM orders"

# 毛利签名与「只算成交额」的语句：安全过关但口径不符，取自口径校验用例
GROSS_PROFIT_SIG = {
    "name": "gross_profit",
    "display": "毛利",
    "require_all": ["cost_price"],
    "require_plain_aggregate": {},
}
SQL_WITHOUT_COST = (
    "SELECT p.brand AS 品牌, SUM(oi.quantity * oi.unit_price) AS 毛利 "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
    "JOIN products p ON p.id = oi.product_id "
    "WHERE o.status NOT IN ('cancelled', 'pending') GROUP BY p.brand LIMIT 1000"
)


class _FakeRepo(QueryRepository):
    """业务库替身。只关心 execute_readonly 的返回值与异常。"""

    def __init__(self, result: QueryResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def execute_readonly(self, sql: str, max_rows: int | None = None) -> QueryResult:
        self.calls.append(sql)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def list_tables(self) -> list[str]:
        return ["orders"]

    def count_rows(self, table: str) -> int:
        return 0

    def data_as_of(self) -> dt.date | None:
        # 审计用例不涉及时间词换算，返回 None 让节点退回系统日期即可
        return None


class _BoomSink(AuditSink):
    def write(self, event: AuditEvent) -> None:
        raise RuntimeError("审计库写不进去")


def _state(sql: str, **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = dict(initial_state("sess-audit", "上个月各区域成交总额"))
    state["sql"] = sql
    state.update(extra)
    return state


@pytest.fixture()
def recorder() -> Any:
    """每个用例独立的记录器，用完复位全局。"""
    sink = MemorySink()
    rec = AuditRecorder(sink)

    def _done() -> None:
        reset_recorder()

    yield rec, sink
    _done()


# ---------------------------------------------------------------------------
# 守卫判定
# ---------------------------------------------------------------------------


def test_guard_block_is_recorded_with_rule_id(guard: Any, recorder: Any) -> None:
    rec, sink = recorder
    result = validate_node(_state(BLOCKED_SQL), guard=guard, audit=rec)

    assert result["sql_error"]
    events = sink.filter(EVENT_GUARD_BLOCK)
    assert len(events) == 1
    event = events[0]
    assert event.outcome == OUTCOME_DENIED
    # 规则编号从守卫文案解析，审计里按规则统计拦截分布靠这个字段
    assert event.rule_id == "R4"
    assert "secret_ledger" in (event.sql_text or "")
    assert event.session_id == "sess-audit"
    assert event.question == "上个月各区域成交总额"


def test_guard_rewrite_and_pass_are_distinct(guard: Any, recorder: Any) -> None:
    rec, sink = recorder
    validate_node(_state(NO_LIMIT_SQL), guard=guard, audit=rec)
    validate_node(_state(PLAIN_SQL), guard=guard, audit=rec)

    assert len(sink.filter(EVENT_GUARD_REWRITE)) == 1
    assert len(sink.filter(EVENT_GUARD_PASS)) == 1
    rewrite = sink.filter(EVENT_GUARD_REWRITE)[0]
    assert rewrite.outcome == OUTCOME_OK
    assert rewrite.detail["warnings"]


def test_metric_block_is_recorded(guard: Any, recorder: Any) -> None:
    rec, sink = recorder
    state = _state(SQL_WITHOUT_COST, retrieved={"metric_signatures": [GROSS_PROFIT_SIG]})
    result = validate_node(state, guard=guard, audit=rec)

    assert "口径" in (result["sql_error"] or "")
    events = sink.filter(EVENT_METRIC_BLOCK)
    assert len(events) == 1
    # 口径拦截没有守卫规则编号，如实留空，靠 event_type 区分
    assert events[0].rule_id is None
    assert events[0].outcome == OUTCOME_DENIED


# ---------------------------------------------------------------------------
# SQL 执行
# ---------------------------------------------------------------------------


def test_execute_success_records_rows_and_tables(recorder: Any) -> None:
    rec, sink = recorder
    repo = _FakeRepo(
        result=QueryResult(
            sql=PLAIN_SQL,
            columns=["id"],
            rows=[{"id": 1}, {"id": 2}],
            row_count=2,
            elapsed_ms=3.5,
            bytes_estimate=64,
        )
    )
    execute_node(_state(PLAIN_SQL), repo=repo, audit=rec)

    events = sink.filter(EVENT_SQL_EXECUTE)
    assert len(events) == 1
    event = events[0]
    assert event.outcome == OUTCOME_OK
    assert event.row_count == 2
    assert event.source_tables == ["orders"]
    assert event.detail["columns"] == ["id"]
    assert event.duration_ms is not None


def test_execute_error_records_original_message(recorder: Any) -> None:
    rec, sink = recorder
    repo = _FakeRepo(error=QueryError("no such column: xxx"))
    result = execute_node(_state(PLAIN_SQL), repo=repo, audit=rec)

    assert "no such column" in (result["sql_error"] or "")
    events = sink.filter(EVENT_SQL_ERROR)
    assert len(events) == 1
    assert events[0].outcome == OUTCOME_ERROR
    assert events[0].message == "no such column: xxx"


def test_sink_failure_does_not_break_node(guard: Any) -> None:
    """审计写不进去时主链路照常返回。"""
    rec = AuditRecorder(_BoomSink())
    result = validate_node(_state(PLAIN_SQL), guard=guard, audit=rec)
    assert not result.get("sql_error")


def test_audit_disabled_is_silent(guard: Any) -> None:
    rec = AuditRecorder(NullSink())
    assert rec.enabled is False
    validate_node(_state(BLOCKED_SQL), guard=guard, audit=rec)
    assert rec.enabled is False


# ---------------------------------------------------------------------------
# 与 turns 表的分界线
# ---------------------------------------------------------------------------


def test_audit_survives_session_delete(isolated_app_db: Any) -> None:
    """删会话不能抹掉审计。这一条同时验证 turns 确实被清空，形成对照。"""
    engine = create_app_engine(isolated_app_db)
    store = SqliteSessionStore(engine=engine)
    audit = SqliteAuditRepository(engine=engine)

    session_id = store.create_session("审计留存").id
    store.add_turn(TurnRecord(session_id=session_id, question="上个月成交总额", answer="1 亿元"))
    audit.record(
        AuditEvent(
            event_type=EVENT_GUARD_BLOCK,
            outcome=OUTCOME_DENIED,
            session_id=session_id,
            sql_text=BLOCKED_SQL,
            message="R4 未登记的表：secret_ledger",
        )
    )

    assert store.delete_session(session_id) is True
    assert store.list_turns(session_id) == []  # turns 被级联清空

    assert audit.count() == 1
    kept = audit.search()[0]
    # 审计保留原 session_id：会话删了仍留着唯一的追溯线索
    assert kept.session_id == session_id
    assert audit.search(keyword="secret_ledger")[0].id == kept.id


def test_audit_repo_filters_and_summary(isolated_app_db: Any) -> None:
    audit = SqliteAuditRepository(engine=create_app_engine(isolated_app_db))
    base = dt.datetime(2026, 9, 1, 12, 0, 0)
    audit.record(
        AuditEvent(
            event_type=EVENT_GUARD_BLOCK,
            outcome=OUTCOME_DENIED,
            rule_id="R4",
            session_id="s1",
            message="R4 未登记的表",
            ts=base,
        )
    )
    audit.record(
        AuditEvent(
            event_type=EVENT_SQL_EXECUTE,
            outcome=OUTCOME_OK,
            session_id="s1",
            row_count=10,
            ts=base + dt.timedelta(hours=1),
        )
    )

    assert audit.search(event_type=EVENT_GUARD_BLOCK, limit=10, offset=0)[0].rule_id == "R4"
    assert audit.search(outcome=OUTCOME_OK, limit=10, offset=0)[0].row_count == 10
    assert audit.count(session_id="s1") == 2
    assert audit.count(session_id="nobody") == 0
    # 时间倒序：后写的一条排前面
    assert audit.search(limit=10, offset=0)[0].event_type == EVENT_SQL_EXECUTE
    assert audit.count(since=base + dt.timedelta(minutes=30)) == 1
    assert audit.count(until=base + dt.timedelta(minutes=30)) == 1

    summary = audit.summary()
    assert summary["total"] == 2
    assert summary["by_event_type"][EVENT_GUARD_BLOCK] == 1
    assert summary["by_outcome"][OUTCOME_DENIED] == 1
    # 规则统计只收非空值，否则一条无编号的记录会淹没真实分布
    assert summary["by_rule"] == {"R4": 1}


def test_recorder_switch_is_global() -> None:
    configure_recorder(MemorySink())
    try:
        assert get_recorder().enabled is True
    finally:
        reset_recorder()
    assert get_recorder().enabled is False


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------


def _seed(container: Any, **kwargs: Any) -> int:
    assert container.audit is not None
    return container.audit.record(
        AuditEvent(
            event_type=kwargs.get("event_type", EVENT_GUARD_BLOCK),
            outcome=kwargs.get("outcome", OUTCOME_DENIED),
            session_id=kwargs.get("session_id", "s-seed"),
            question=kwargs.get("question", "上个月各区域成交总额"),
            sql_text=kwargs.get("sql_text", BLOCKED_SQL),
            message=kwargs.get("message", "R4 未登记的表：secret_ledger"),
            rule_id=kwargs.get("rule_id", "R4"),
        )
    )


def test_audit_endpoint_lists_and_filters(client: Any) -> None:
    container = get_container()
    _seed(container)
    _seed(container, event_type=EVENT_SQL_EXECUTE, outcome=OUTCOME_OK, rule_id=None, row_count=3)

    body = client.get("/api/audit", params={"limit": 10}).json()
    assert body["total"] >= 2
    assert set(body["event_types"]) >= {EVENT_GUARD_BLOCK, EVENT_SQL_EXECUTE}

    blocked = client.get("/api/audit", params={"event_type": EVENT_GUARD_BLOCK}).json()
    assert all(i["event_type"] == EVENT_GUARD_BLOCK for i in blocked["items"])
    assert blocked["items"][0]["rule_id"] == "R4"

    by_keyword = client.get("/api/audit", params={"keyword": "secret_ledger"}).json()
    assert by_keyword["total"] >= 1


def test_audit_endpoint_summary(client: Any) -> None:
    container = get_container()
    _seed(container)
    body = client.get("/api/audit/summary").json()
    assert body["total"] >= 1
    assert body["by_event_type"][EVENT_GUARD_BLOCK] >= 1
    assert body["by_rule"]["R4"] >= 1


def test_audit_export_csv_keeps_commas_and_newlines(client: Any) -> None:
    container = get_container()
    # 问句里带逗号与换行：手工拼串的 CSV 会在下游打开时错位
    _seed(container, question="上个月，华东\n各区域成交总额")

    resp = client.get("/api/audit/export", params={"format": "csv"})
    assert resp.status_code == 200
    text = resp.text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    assert header[0] == "id"
    questions = [r[header.index("question")] for r in rows[1:]]
    assert "上个月，华东\n各区域成交总额" in questions


def test_audit_export_json_shape(client: Any) -> None:
    container = get_container()
    _seed(container)
    body = client.get("/api/audit/export", params={"format": "json"}).json()
    assert body["count"] >= 1
    assert body["items"][0]["event_type"] == EVENT_GUARD_BLOCK


def test_audit_endpoint_rejects_unknown_enum(client: Any) -> None:
    assert client.get("/api/audit", params={"event_type": "nope"}).status_code == 400
    assert client.get("/api/audit", params={"outcome": "maybe"}).status_code == 400
    assert client.get("/api/audit/export", params={"format": "xlsx"}).status_code == 400


def test_audit_endpoint_reports_unavailable(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """审计不可用时返回 409，而不是空列表。"""
    container = get_container()
    monkeypatch.setattr(container, "audit", None)
    body = client.get("/api/audit").json()
    assert client.get("/api/audit").status_code == 409
    assert "审计" in body["detail"]
