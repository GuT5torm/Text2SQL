"""对话接口。

POST /api/chat 用 SSE 推送两类事件：

  stage   节点进度。每过一个节点推一条，前端据此显示「正在生成查询」等状态。
          查询类请求通常要数秒到十几秒，没有进度反馈的界面会让人以为卡死。
  result  本轮最终结果。包含结论、SQL、口径、结果集与图表配置。
  error   未预期的异常。节点内部的失败不会走这里，那是 give_up 节点的职责。

SSE 在这里承担的是「进度 + 一次完整结果」，不是逐字输出。原因是本系统的主要
等待来自 SQL 生成与执行，而不是文本生成，逐字输出的信息量反而更低。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agent.graph import summarize_result
from app.agent.state import initial_state
from app.agent.checkpointer import thread_config
from app.api.deps import Container, get_container
from app.db.masking import mask_result
from app.db.repository import TurnRecord
from app.db.sqlite_repo import dump_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])

# 落库的结果集上限。全量结果已随 SSE 返回给前端，库里只留够回看的部分
PERSIST_ROWS = 200


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="用户的自然语言问题")
    session_id: str = Field(description="会话ID，由 POST /api/session 创建")


def sse(event: str, data: Any) -> str:
    """按 SSE 规范格式化一条事件。data 用单行 JSON，避免换行破坏帧边界。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _iter_chat(container: Container, session_id: str, question: str) -> Iterator[str]:
    config = thread_config(session_id)
    inputs: dict[str, Any] = {
        **initial_state(session_id, question),
        "messages": [HumanMessage(content=question)],
    }

    try:
        for chunk in container.graph.stream(inputs, config=config, stream_mode="updates"):
            for _node, update in (chunk or {}).items():
                for stage in (update or {}).get("stages") or []:
                    yield sse("stage", stage)

        final = container.graph.get_state(config).values
        result = summarize_result(final)
        # FR-I-3：脱敏必须在落库与下发之前，rows_json 与 SSE 帧共用同一份数据。
        mask_result(result, container.catalog)
        _persist(container, session_id, question, result)
        yield sse("result", result)

    except Exception as exc:  # noqa: BLE001 - 兜住编排层的意外异常，保证前端能收到终止帧
        logger.exception("对话链路异常")
        yield sse("error", {"message": f"服务端处理失败：{exc}"})
    finally:
        # 主动结束流，避免前端 EventSource 认为连接仍在等待
        yield "event: done\ndata: {}\n\n"


def _persist(container: Container, session_id: str, question: str, result: dict[str, Any]) -> None:
    try:
        # 持久化结果集的限制：超过 PERSIST_ROWS 的部分丢，但落库行数如实记下，
        # 与执行端 row_count 一致能让回看时看出「查询到底返了几行 vs 库只存了几行」。
        rows_full = list(result.get("rows") or [])
        rows_to_persist = rows_full[:PERSIST_ROWS]
        turn_id = container.store.add_turn(
            TurnRecord(
                session_id=session_id,
                question=question,
                answer=result.get("answer") or "",
                sql=result.get("sql") or None,
                metrics_json=dump_json(
                    {
                        "metric_used": result.get("metric_used") or [],
                        "caveats": result.get("caveats") or [],
                        "time_range": result.get("time_range"),
                        # FR-C-1：把写法说明也塞进 metrics_json，
                        # 旧版接口仍读得到；新建专用字段 sql_reasoning 也写库方便回看
                        # FR-I-3：脱敏标记随轮次落库，历史回看可显示「已脱敏」
                        "masked": bool(result.get("masked")),
                        "masked_fields": result.get("masked_fields") or [],
                        # FR-B-3 / FR-D-5：结论数字核对与时间范围一致性
                        "number_checks": result.get("number_checks") or [],
                        "time_range_consistent": result.get("time_range_consistent"),
                    }
                ),
                chart_json=dump_json(result.get("chart")) if result.get("chart") else None,
                rows_json=dump_json(
                    {
                        "columns": result.get("columns") or [],
                        "rows": rows_to_persist,
                        "row_count": result.get("row_count") or 0,
                    }
                ),
                row_count=result.get("row_count") or 0,
                elapsed_ms=result.get("query_elapsed_ms") or 0.0,
                status=result.get("status") or "ok",
                error=result.get("sql_error") or None,
                # —— FR-H-2 历史回看字段 ——
                stages_json=dump_json(result.get("stages") or []),
                original_sql=result.get("original_sql") or None,
                guard_warnings_json=dump_json(result.get("guard_warnings") or []),
                retry_count=int(result.get("retry_count") or 0),
                # FR-H-2 之 truncated 重定义：执行时是否被 max_rows 截断。
                # 落库层 PERSIST_ROWS 截断不算 truncated（是落库行为，不是查询行为）。
                truncated=1 if result.get("truncated") else 0,
                persist_row_count=len(rows_to_persist),
                # FR-D-1 来源表
                source_tables_json=dump_json(result.get("source_tables") or []),
                # FR-L-1 落库，值由第 6 批填充；FR-M-1 的 baselines 本批落库
                findings_json=dump_json(result.get("findings") or []),
                baselines_json=dump_json(result["baselines"])
                if result.get("baselines")
                else None,
                # FR-E-1：用 stages 字段反推节点序列（同一字段两处使用但语义不同）。
                # state 中没有 node_path 累加器，避免给 LangGraph 加 list reducer 复杂度。
                node_path=dump_json(_derive_node_path(result.get("stages") or [])),
                # FR-A-5：结构化槽位落库，历史回看可显示
                slots_json=dump_json(result.get("slots") or {}),
                # FR-H-3：原话与改写句都落库。原话是用户输入的那句，改写句是链路
                # 真正执行的那句，回看时要能判断两者是否一致
                original_question=result.get("original_question") or question,
                rewritten_question=result.get("rewritten_question") or None,
            )
        )
        # 把落库后的 turn_id 回填到 result，前端据此调用 feedback/share 接口。
        # 落库前的 result 不带 turn_id；这里在 add_turn 后回填是 SSE 链路的最后一个写点。
        result["turn_id"] = turn_id
    except Exception:  # noqa: BLE001 - 落库失败不应影响本次回答的返回
        logger.exception("问答记录写入失败（不影响本次返回）")


def _derive_node_path(stages: list[dict[str, Any]]) -> str:
    """从 stages 提取节点序列去重 + 保序。同一节点多次出现（fix 回环）只取一次。"""
    seen: set[str] = set()
    out: list[str] = []
    for s in stages:
        name = s.get("node") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return ",".join(out)


@router.post("/chat")
def chat(req: ChatRequest, container: Container = Depends(get_container)) -> StreamingResponse:
    if container.store.get_session(req.session_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{req.session_id}")

    return StreamingResponse(
        _iter_chat(container, req.session_id, req.question.strip()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx 下必须关掉缓冲，否则进度会被攒到最后一次性吐出，
            # 逐节点反馈就失效了。配置见 deploy/nginx.conf
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/sync")
def chat_sync(req: ChatRequest, container: Container = Depends(get_container)) -> dict[str, Any]:
    """非流式版本，便于脚本与评测集调用，也便于 curl 手工调试。"""
    if container.store.get_session(req.session_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{req.session_id}")

    config = thread_config(req.session_id)
    inputs: dict[str, Any] = {
        **initial_state(req.session_id, req.question.strip()),
        "messages": [HumanMessage(content=req.question.strip())],
    }
    final = container.graph.invoke(inputs, config=config)
    result = summarize_result(final)
    # FR-I-3：与 SSE 路径同一脱敏点，保证两种接口出参一致。
    mask_result(result, container.catalog)
    _persist(container, req.session_id, req.question.strip(), result)
    return result
