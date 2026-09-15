"""端到端冒烟脚本：不依赖密钥，用离线桩把整条链路走一遍。

跑法：在 backend 目录下 .venv/Scripts/python.exe tests/smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def show(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    failures: list[str] = []
    with TestClient(app) as client:
        health = client.get("/api/health")
        show("health", health.json())
        if health.status_code != 200 or health.json().get("status") != "ok":
            failures.append("健康检查未通过")

        sem = client.get("/api/semantic/health")
        show("semantic/health", sem.json())
        if not sem.json().get("consistent"):
            failures.append(
                "语义层与业务库表名不一致："
                f"missing_in_semantic={sem.json().get('missing_in_semantic')} "
                f"missing_in_db={sem.json().get('missing_in_db')}"
            )

        recall = client.get(
            "/api/semantic/recall",
            params={"q": "上个季度华东区各品类的成交总额", "with_context": False},
        )
        show("recall", recall.json())

        created = client.post("/api/session", json={"title": ""})
        session_id = created.json()["id"]
        print(f"\nsession_id={session_id}")

        for question in (
            "上个月各区域的成交总额",
            "各品类的退款率",
            "最近三个月的下单渠道分布",
        ):
            resp = client.post(
                "/api/chat/sync",
                json={"session_id": session_id, "question": question},
            )
            payload = resp.json()
            print(f"\n--- Q: {question} ---")
            print("status:", payload.get("status"))
            print("sql:", payload.get("sql"))
            print("columns:", payload.get("columns"))
            print("row_count:", payload.get("row_count"))
            print("answer:", (payload.get("answer") or "")[:200])
            chart = payload.get("chart") or {}
            print("chart_type:", (chart.get("option") or {}) and chart.get("type") or chart.get("type"))
            print("stages:", len(payload.get("stages") or []))
            print("error:", payload.get("error"))
            if payload.get("status") not in {"ok", "empty"}:
                failures.append(f"问答未成功：{question} → {payload.get('status')} {payload.get('error')}")

    print("\n=== 结果 ===")
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
