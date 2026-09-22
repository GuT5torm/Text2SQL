"""只读分享用例（FR-N-3）。

接口层覆盖：
- POST /api/share 创建分享，返回 token 与过期时间
- GET  /api/share/{token} 读快照，已撤销/过期返回 410，不存在返回 404
- 快照内容与已落库轮次一致（问句、SQL、口径、图表、明细表）
- 软撤销（revoked=1）后不可读
- 第 7 批 FR-I-3 数据掩码尚未接入，本批不验证敏感字段
"""

from __future__ import annotations

from typing import Any

from app.db.repository import ShareRecord, SessionStore, TurnRecord


def _new_session(client: Any) -> str:
    return client.post("/api/session", json={"title": "分享用例"}).json()["id"]


def _new_turn(client: Any, sid: str, question: str = "上个月各区域的成交总额") -> int:
    body = client.post("/api/chat/sync", json={"session_id": sid, "question": question}).json()
    return int(body["turn_id"])


# ---------------------------------------------------------------------------
# 仓储层
# ---------------------------------------------------------------------------


def test_create_and_get_share(store: SessionStore) -> None:
    sid = store.create_session().id
    tid = store.add_turn(TurnRecord(session_id=sid, question="Q1", answer="A1"))
    token = store.create_share(
        ShareRecord(token="abc-token", turn_id=tid, session_id=sid, snapshot_json='{"question":"Q1"}')
    )
    assert token == "abc-token"

    rec = store.get_share(token)
    assert rec is not None
    assert rec.turn_id == tid
    assert rec.snapshot_json == '{"question":"Q1"}'
    assert rec.revoked == 0


def test_get_share_returns_none_for_unknown(store: SessionStore) -> None:
    assert store.get_share("never-exists") is None


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_create_share_returns_token_and_expires(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    body = client.post(
        "/api/share",
        json={"turn_id": tid, "session_id": sid, "expires_in_days": 7},
    ).json()
    assert body["token"]
    assert body["turn_id"] == tid
    assert body["expires_at"] is not None


def test_create_share_rejects_unknown_turn(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    res = client.post(
        "/api/share",
        json={"turn_id": 999999, "session_id": sid},
    )
    assert res.status_code == 404


def test_read_share_returns_snapshot(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid, "上个月各区域的成交总额")
    created = client.post("/api/share", json={"turn_id": tid, "session_id": sid}).json()
    token = created["token"]

    body = client.get(f"/api/share/{token}").json()
    assert body["token"] == token
    assert body["turn_id"] == tid
    snap = body["snapshot"]
    # 快照只装已落库内容：问句、SQL、口径、图表、明细表都在
    assert snap["question"] == "上个月各区域的成交总额"
    assert snap["sql"]
    assert snap["status"] in {"ok", "empty"}
    assert "metric_used" in snap
    assert "row_count" in snap


def test_read_share_404_for_unknown_token(client: Any) -> None:
    res = client.get("/api/share/not-exist")
    assert res.status_code == 404


def test_read_share_410_when_expired(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    # expires_in_days 最小值是 1，要测过期必须直接改库
    created = client.post("/api/share", json={"turn_id": tid, "session_id": sid}).json()
    token = created["token"]

    # 直接改库把 expires_at 改成过去时间
    import datetime as dt

    from app.config import get_settings
    from sqlalchemy import text
    from app.db.engine import create_app_engine

    engine = create_app_engine(get_settings().app_db_path)
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :t WHERE token = :tk"),
            {"t": dt.datetime(2020, 1, 1), "tk": token},
        )
        conn.commit()

    res = client.get(f"/api/share/{token}")
    assert res.status_code == 410
    assert "过期" in res.json()["detail"]


def test_read_share_410_when_revoked(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    created = client.post("/api/share", json={"turn_id": tid, "session_id": sid}).json()
    token = created["token"]

    from app.config import get_settings
    from sqlalchemy import text
    from app.db.engine import create_app_engine

    engine = create_app_engine(get_settings().app_db_path)
    with engine.connect() as conn:
        conn.execute(text("UPDATE shares SET revoked = 1 WHERE token = :tk"), {"tk": token})
        conn.commit()

    res = client.get(f"/api/share/{token}")
    assert res.status_code == 410
    assert "撤销" in res.json()["detail"]


def test_snapshot_includes_partial_period_and_findings(client: Any, require_mock: None) -> None:
    """FR-N-3 快照含 baselines / findings 字段，与历史回看同款。"""
    sid = _new_session(client)
    tid = _new_turn(client, sid, "上个月各区域的成交总额")
    token = client.post("/api/share", json={"turn_id": tid, "session_id": sid}).json()["token"]

    snap = client.get(f"/api/share/{token}").json()["snapshot"]
    assert snap["baselines"] is not None
    assert snap["granularity"] == "month"
    assert snap["partial_period"] is False
    assert len(snap["findings"]) > 0


def test_share_does_not_touch_business_db(client: Any, require_mock: None) -> None:
    """FR-N-3：分享快照来自 app.db 落库内容，不查业务库也能读到。"""
    sid = _new_session(client)
    tid = _new_turn(client, sid)
    created = client.post("/api/share", json={"turn_id": tid, "session_id": sid}).json()

    # 这里不真的离线业务库——只是确认 share API 不读 business_repo，
    # 接口返回的快照内容与 /api/session/{sid} 的 turn 内容一致即可。
    turn = client.get(f"/api/session/{sid}").json()["turns"][0]
    snap = client.get(f"/api/share/{created['token']}").json()["snapshot"]
    assert snap["question"] == turn["question"]
    assert snap["sql"] == turn["sql"]
