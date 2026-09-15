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
        container.store.add_turn(
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
                    }
                ),
                chart_json=dump_json(result.get("chart")) if result.get("chart") else None,
                rows_json=dump_json(
                    {
                        "columns": result.get("columns") or [],
                        "rows": (result.get("rows") or [])[:PERSIST_ROWS],
                        "row_count": result.get("row_count") or 0,
                    }
                ),
                row_count=result.get("row_count") or 0,
                elapsed_ms=result.get("query_elapsed_ms") or 0.0,
                status=result.get("status") or "ok",
                error=result.get("sql_error") or None,
            )
        )
    except Exception:  # noqa: BLE001 - 落库失败不应影响本次回答的返回
        logger.exception("问答记录写入失败（不影响本次返回）")


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
    _persist(container, req.session_id, req.question.strip(), result)
    return result
