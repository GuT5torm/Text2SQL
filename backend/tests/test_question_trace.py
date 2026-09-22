"""FR-H-3 原始问句与改写问句单测。

需求原文：死字段要么被消费（日志与落库同时保留原话与改写句），要么删除，
不得保留不生效的字段。本文件锁住三件事：

1. route 节点改写时，原话与改写句都在状态里，不再互相覆盖；
2. 两列会随 turns 表迁移补上，旧库不报错；
3. 落库后回看能取回两个版本。
"""

from __future__ import annotations

from typing import Any

from app.agent.nodes.route import route_node
from app.db.repository import TurnRecord
from app.db.sqlite_repo import _TURN_MIGRATIONS
from app.llm.schemas import RouteDecision


class _FakeRouteGateway:
    """返回固定 RouteDecision 的替身。"""

    def __init__(self, decision: RouteDecision) -> None:
        self._decision = decision

    @property
    def mock(self) -> bool:
        return True

    def complete_json(self, prompt: str, schema: type, tag: str | None = None) -> Any:
        return self._decision


def _decision(rewritten: str | None) -> RouteDecision:
    return RouteDecision(
        intent="query",
        reason="取数问题",
        rewritten_question=rewritten,
        slots={"metrics": [], "dimensions": [], "filters": [], "time_range": "", "order_by": None, "top_n": None},
    )


def test_rewrite_keeps_both_versions() -> None:
    gateway = _FakeRouteGateway(_decision("上个月各区域的成交总额是多少"))
    state = {"question": "那上个月呢", "original_question": "那上个月呢", "messages": []}
    out = route_node(state, gateway=gateway, history_window=6)

    assert out["question"] == "上个月各区域的成交总额是多少"
    assert out["rewritten_question"] == "上个月各区域的成交总额是多少"
    # 原话不被覆盖：改写只改 question，original_question 由 initial_state 写入
    assert out.get("original_question", "那上个月呢") == "那上个月呢"


def test_no_rewrite_records_original_as_rewritten() -> None:
    gateway = _FakeRouteGateway(_decision(None))
    state = {"question": "各区域的成交总额", "original_question": "各区域的成交总额", "messages": []}
    out = route_node(state, gateway=gateway, history_window=6)

    # 未改写时 route 不回写 question（state 里本来就是原话），只把改写句记成同值
    assert out.get("question", "各区域的成交总额") == "各区域的成交总额"
    assert out["rewritten_question"] == "各区域的成交总额"


def test_migration_covers_new_columns() -> None:
    cols = {col for col, _ in _TURN_MIGRATIONS}
    assert "original_question" in cols
    assert "rewritten_question" in cols


def test_persist_and_replay_both_versions(store: Any) -> None:
    sid = store.create_session().id
    tid = store.add_turn(
        TurnRecord(
            session_id=sid,
            question="上个月各区域的成交总额是多少",
            answer="A",
            original_question="那上个月呢",
            rewritten_question="上个月各区域的成交总额是多少",
        )
    )
    (turn,) = [t for t in store.list_turns(sid) if t.id == tid]
    assert turn.original_question == "那上个月呢"
    assert turn.rewritten_question == "上个月各区域的成交总额是多少"


def test_legacy_turn_without_new_columns(store: Any) -> None:
    """旧库记录缺两列时回看不报错，两列为空。"""
    sid = store.create_session().id
    tid = store.add_turn(TurnRecord(session_id=sid, question="Q", answer="A"))
    (turn,) = [t for t in store.list_turns(sid) if t.id == tid]
    assert turn.original_question is None
    assert turn.rewritten_question is None
