"""接口层用例：健康检查、会话管理、语义层调试接口、两轮问答的进度与落库。

这些用例走的是完整应用（依赖注入容器 + 编译后的图），断言的是接入层的契约：
状态码、字段完整性、跨轮状态是否残留。链路内部的判定由 test_guard 与
test_repository 负责，这里不重复。
"""

from __future__ import annotations

import json
from typing import Any


def _new_session(client: Any) -> str:
    return client.post("/api/session", json={"title": "接口用例"}).json()["id"]


# ---------------------------------------------------------------------------
# 运维接口
# ---------------------------------------------------------------------------


def test_health_reports_runtime_state(client: Any) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["metrics"] > 0
    assert body["tables"] > 0
    assert isinstance(body["llm_mock"], bool)


def test_stats_returns_usage_totals(client: Any) -> None:
    assert isinstance(client.get("/api/stats").json(), dict)


# ---------------------------------------------------------------------------
# 语义层调试接口
# ---------------------------------------------------------------------------


def test_semantic_overview(client: Any) -> None:
    body = client.get("/api/semantic").json()
    assert body["counts"]["metrics"] > 0
    assert "orders" in {t["name"] for t in body["tables"]}


def test_semantic_metrics_expose_expression(client: Any) -> None:
    items = client.get("/api/semantic/metrics").json()["items"]
    gmv = next(m for m in items if m["name"] == "gmv")
    assert gmv["expression"]


def test_semantic_recall_without_model(client: Any) -> None:
    body = client.get("/api/semantic/recall", params={"q": "上个月各区域的成交总额"}).json()
    assert body["metrics"] == ["gmv"]
    assert body["time_range"]["label"] == "上个月"
    assert "context" not in body

    with_ctx = client.get(
        "/api/semantic/recall", params={"q": "上个月各区域的成交总额", "with_context": True}
    ).json()
    assert "指标口径" in with_ctx["context"]


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------


def test_session_lifecycle(client: Any) -> None:
    sid = _new_session(client)

    detail = client.get(f"/api/session/{sid}").json()
    assert detail["turns"] == []
    assert detail["turn_count"] == 0

    assert client.patch(f"/api/session/{sid}", json={"rename": {"title": "改名后"}}).json()["title"] == "改名后"
    assert client.patch(f"/api/session/{sid}", json={"archive": {"archived": True}}).json()["archived"] is True
    assert client.get("/api/session").json()["total"] == 0
    assert client.get("/api/session", params={"include_archived": True}).json()["total"] == 1

    assert client.delete(f"/api/session/{sid}").json()["deleted"] is True
    assert client.get(f"/api/session/{sid}").status_code == 404


def test_unknown_session_returns_404(client: Any) -> None:
    assert client.get("/api/session/not-exist").status_code == 404
    assert client.delete("/api/session/not-exist").status_code == 404
    assert client.patch("/api/session/not-exist", json={"rename": {"title": "x"}}).status_code == 404
    assert (
        client.post("/api/chat/sync", json={"session_id": "not-exist", "question": "成交额"}).status_code == 404
    )


def test_question_length_is_validated(client: Any) -> None:
    sid = _new_session(client)
    assert client.post("/api/chat/sync", json={"session_id": sid, "question": ""}).status_code == 422
    assert client.post("/api/chat/sync", json={"session_id": sid, "question": "额" * 501}).status_code == 422


# ---------------------------------------------------------------------------
# 编排链路（依赖桩实现）
# ---------------------------------------------------------------------------


def test_chat_sync_completes_one_round(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    body = client.post("/api/chat/sync", json={"session_id": sid, "question": "上个月各区域的成交总额"}).json()

    assert body["status"] in {"ok", "empty"}
    assert body["sql"]
    assert body["stages"], "进度列表为空说明节点没有上报阶段事件"
    assert body["answer"]


def test_stages_do_not_accumulate_across_rounds(client: Any, require_mock: None) -> None:
    """第二轮不能带着第一轮的阶段。这是 P2 修过的缺陷，用接口层再兜一道。"""
    sid = _new_session(client)
    counts = []
    for question in ("上个月各区域的成交总额", "各品类的退款率", "最近三个月的下单渠道分布"):
        body = client.post("/api/chat/sync", json={"session_id": sid, "question": question}).json()
        counts.append(len(body["stages"]))
    assert len(set(counts)) == 1, f"各轮阶段数不一致：{counts}"


def test_turns_are_persisted(client: Any, require_mock: None) -> None:
    """落库失败曾被兜底异常吞掉，接口照常返回 ok，因此必须在接口层断言。"""
    sid = _new_session(client)
    for question in ("上个月各区域的成交总额", "各品类的退款率"):
        client.post("/api/chat/sync", json={"session_id": sid, "question": question})

    detail = client.get(f"/api/session/{sid}").json()
    assert detail["turn_count"] == 2
    assert len(detail["turns"]) == 2
    assert detail["turns"][0]["sql"]
    assert detail["turns"][0]["chart"]["chart_type"]


def test_sse_stream_emits_stages_then_result(client: Any, require_mock: None) -> None:
    sid = _new_session(client)
    events: list[tuple[str, dict[str, Any]]] = []

    with client.stream(
        "POST", "/api/chat", json={"session_id": sid, "question": "上个月各区域的成交总额"}
    ) as response:
        event_name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: ") and event_name:
                events.append((event_name, json.loads(line[6:])))
                event_name = None

    names = [name for name, _ in events]
    assert "stage" in names
    assert "result" in names
    assert names[-1] == "done"
    assert names.count("result") == 1
