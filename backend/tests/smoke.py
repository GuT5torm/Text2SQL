"""端到端冒烟脚本：不依赖密钥，用离线桩把整条链路走一遍。

除了走通链路，这里还守两处曾经出过问题的行为：

- stages 的条数每轮应当相同。用 operator.add 归约时它会跨轮累积，第二轮变
  14 条、第三轮 21 条，接口返回的进度里混进上一轮的内容。
- 每轮问答都要落到会话里。TurnRecord 曾经缺 id 默认值导致写入抛异常，异常
  被落库处的兜底吞掉，接口照常返回 ok，历史会话实际是空的。

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

        questions = (
            "上个月各区域的成交总额",
            "各品类的退款率",
            "最近三个月的下单渠道分布",
        )
        stage_counts: list[int] = []

        for question in questions:
            resp = client.post(
                "/api/chat/sync",
                json={"session_id": session_id, "question": question},
            )
            payload = resp.json()
            stages = payload.get("stages") or []
            stage_counts.append(len(stages))

            print(f"\n--- Q: {question} ---")
            print("status:", payload.get("status"))
            print("sql:", payload.get("sql"))
            print("columns:", payload.get("columns"))
            print("row_count:", payload.get("row_count"))
            print("answer:", (payload.get("answer") or "")[:200])
            # 图表载荷的字段名是 chart_type，不是 type
            print("chart_type:", (payload.get("chart") or {}).get("chart_type"))
            print("stages:", len(stages), [s.get("node") for s in stages])
            print("error:", payload.get("error"))
            if payload.get("status") not in {"ok", "empty"}:
                failures.append(f"问答未成功：{question} → {payload.get('status')} {payload.get('error')}")

        if len(set(stage_counts)) != 1:
            failures.append(f"stages 条数跨轮不一致，疑似重复累积：{stage_counts}")
        elif stage_counts and stage_counts[0] == 0:
            failures.append("stages 为空，进度事件未随结果返回")

        detail = client.get(f"/api/session/{session_id}").json()
        turns = detail.get("turns") or []
        print(f"\n--- 会话落库 ---\nturn_count: {detail.get('turn_count')} | 实际条数: {len(turns)}")
        if len(turns) != len(questions):
            failures.append(f"问答记录未完整落库：期望 {len(questions)} 条，实际 {len(turns)} 条")
        if turns and not turns[0].get("sql"):
            failures.append("首轮记录缺少 sql 字段，落库内容不完整")
        if turns and not (turns[0].get("chart") or {}).get("chart_type"):
            failures.append("首轮记录缺少图表配置，历史回看时无法还原图")

    print("\n=== 结果 ===")
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
