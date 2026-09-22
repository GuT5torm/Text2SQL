"""反馈闭环用例（FR-G-1 至 FR-G-4）。

按接口层 + 仓储层两个视角覆盖：
- POST /api/feedback 提交、覆盖、原因校验、未知 turn
- GET  /api/feedback/summary 聚合统计
- GET  /api/feedback/export 导出为评测用例片段

落库点写 app.db，业务库 ecommerce.db 不参与（FR-G-4）。
"""

from __future__ import annotations

from typing import Any

from app.db.repository import FeedbackRecord, SessionStore, TurnRecord


def _new_session(client: Any) -> str:
    return client.post("/api/session", json={"title": "反馈用例"}).json()["id"]


def _new_turn(client: Any, sid: str, question: str = "上个月各区域的成交总额") -> int:
    body = client.post("/api/chat/sync", json={"session_id": sid, "question": question}).json()
    return int(body["turn_id"])


# ---------------------------------------------------------------------------
# 仓储层
# ---------------------------------------------------------------------------


def test_upsert_inserts_then_updates(store: SessionStore) -> None:
    sid = store.create_session().id
    tid = store.add_turn(TurnRecord(session_id=sid, question="Q1", answer="A1"))

    fid1 = store.upsert_feedback(
        FeedbackRecord(turn_id=tid, session_id=sid, rating="down", reason_code="wrong_number", note="应该是 100")
    )
    assert fid1 > 0

    fid2 = store.upsert_feedback(
        FeedbackRecord(turn_id=tid, session_id=sid, rating="up", reason_code="", note="")
    )
    assert fid2 == fid1, "同 turn_id 的 upsert 应当命中既有记录"

    fresh = store.get_feedback_by_turn(tid)
    assert fresh is not None
    assert fresh.rating == "up"
    assert fresh.reason_code == ""
    assert fresh.note == ""


def test_get_feedback_returns_none_for_unknown_turn(store: SessionStore) -> None:
    assert store.get_feedback_by_turn(999999) is None


def test_list_feedback_filters_by_session_and_rating(store: SessionStore) -> None:
    sid1 = store.create_session("会话A").id
    sid2 = store.create_session("会话B").id
    t1 = store.add_turn(TurnRecord(session_id=sid1, question="Q1", answer="A1"))
    t2 = store.add_turn(TurnRecord(session_id=sid1, question="Q2", answer="A2"))
    t3 = store.add_turn(TurnRecord(session_id=sid2, question="Q3", answer="A3"))

    store.upsert_feedback(FeedbackRecord(turn_id=t1, session_id=sid1, rating="up"))
    store.upsert_feedback(FeedbackRecord(turn_id=t2, session_id=sid1, rating="down", reason_code="other"))
    store.upsert_feedback(FeedbackRecord(turn_id=t3, session_id=sid2, rating="up"))

    only_sid1 = store.list_feedback(session_id=sid1)
    assert {f.turn_id for f in only_sid1} == {t1, t2}

    only_down = store.list_feedback(rating="down")
    assert all(f.rating == "down" for f in only_down)
    assert any(f.turn_id == t2 for f in only_down)


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_submit_up_down_and_update(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid)

    # 第一次：点踩 + 原因 + 备注
    body = client.post(
        "/api/feedback",
        json={
            "turn_id": tid,
            "session_id": sid,
            "rating": "down",
            "reason_code": "wrong_number",
            "note": "应该是 100",
        },
    ).json()
    assert body["written"] is True
    assert body["feedback"]["rating"] == "down"
    assert body["feedback"]["reason_code"] == "wrong_number"

    # 第二次：改成点赞——应覆盖既有记录（同 turn_id 至多一条）
    body = client.post(
        "/api/feedback",
        json={"turn_id": tid, "session_id": sid, "rating": "up"},
    ).json()
    assert body["id"] == body["feedback"]["id"]
    assert body["feedback"]["rating"] == "up"
    assert body["feedback"]["reason_code"] == ""


def test_submit_rejects_invalid_reason_code(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    res = client.post(
        "/api/feedback",
        json={
            "turn_id": tid,
            "session_id": sid,
            "rating": "down",
            "reason_code": "totally_made_up",
            "note": "",
        },
    )
    assert res.status_code == 422


def test_submit_rejects_unknown_turn(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    res = client.post(
        "/api/feedback",
        json={"turn_id": 999999, "session_id": sid, "rating": "up"},
    )
    assert res.status_code == 404


def test_submit_rejects_unknown_session(client: Any, require_mock: None) -> None:
    res = client.post(
        "/api/feedback",
        json={"turn_id": 1, "session_id": "not-exist", "rating": "up"},
    )
    assert res.status_code == 404


def test_summary_aggregates_ratings_and_reasons(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    t1 = _new_turn(client, sid, "上个月各区域的成交总额")
    t2 = _new_turn(client, sid, "各品类的退款率")
    t3 = _new_turn(client, sid, "各渠道的订单量")

    client.post("/api/feedback", json={"turn_id": t1, "session_id": sid, "rating": "up"})
    client.post(
        "/api/feedback",
        json={"turn_id": t2, "session_id": sid, "rating": "down", "reason_code": "wrong_metric"},
    )
    client.post(
        "/api/feedback",
        json={"turn_id": t3, "session_id": sid, "rating": "down", "reason_code": "wrong_number"},
    )

    body = client.get("/api/feedback/summary", params={"session_id": sid}).json()
    assert body["total"] == 3
    assert body["up"] == 1
    assert body["down"] == 2
    # reason_counts 不含点赞（reason_code 为空）
    assert body["reason_counts"]["wrong_metric"] == 1
    assert body["reason_counts"]["wrong_number"] == 1


def test_summary_rejects_invalid_rating(client: Any) -> None:
    res = client.get("/api/feedback/summary", params={"rating": "meh"})
    assert res.status_code == 422


def test_export_emits_yaml_with_pending_review(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    client.post(
        "/api/feedback",
        json={
            "turn_id": tid,
            "session_id": sid,
            "rating": "down",
            "reason_code": "wrong_number",
            "note": "应该是 100",
        },
    )

    body = client.get("/api/feedback/export", params={"session_id": sid}).json()
    assert body["count"] == 1
    assert "pending_review" in body["yaml"]
    assert "feedback-1" in body["yaml"]
    assert "应该是 100" in body["yaml"]
    assert body["note"].startswith("导出条目需人工确认")


def test_export_empty_when_no_down(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    client.post("/api/feedback", json={"turn_id": tid, "session_id": sid, "rating": "up"})

    body = client.get("/api/feedback/export", params={"session_id": sid}).json()
    assert body["count"] == 0
    assert body["yaml"] == ""


def test_feedback_table_lives_in_app_db_not_business(client: Any, require_mock: None) -> None:
    """FR-G-4：反馈写 app.db，与业务库分离。检查表存在并落库。"""
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    client.post(
        "/api/feedback",
        json={"turn_id": tid, "session_id": sid, "rating": "up"},
    ).json()

    # app.db 在测试夹具里指向 isolated_app_db 路径
    from app.config import get_settings

    settings = get_settings()
    import sqlite3

    conn = sqlite3.connect(str(settings.app_db_path))
    try:
        rows = conn.execute("SELECT turn_id, rating FROM feedback").fetchall()
        assert rows == [(tid, "up")]
    finally:
        conn.close()
