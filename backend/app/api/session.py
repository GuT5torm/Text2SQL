"""会话接口：创建、列表、详情、重命名、归档、删除。

session_id 同时是 LangGraph Checkpointer 的 thread_id，因此会话一旦创建，
多轮上下文就在图内部按这个键自动累积，接入层不需要自己维护对话历史。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import Container, get_container

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session", tags=["session"])


class CreateSessionRequest(BaseModel):
    title: str = Field(default="新会话", max_length=120)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ArchiveSessionRequest(BaseModel):
    archived: bool = True


def _session_dict(rec: Any) -> dict[str, Any]:
    return {
        "id": rec.id,
        "title": rec.title,
        "created_at": rec.created_at.isoformat(timespec="seconds"),
        "updated_at": rec.updated_at.isoformat(timespec="seconds"),
        "archived": bool(rec.archived),
        "turn_count": rec.turn_count,
    }


@router.post("")
def create_session(
    req: CreateSessionRequest | None = None,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    rec = container.store.create_session((req.title if req else None) or "新会话")
    return _session_dict(rec)


@router.get("")
def list_sessions(
    keyword: str | None = Query(default=None, description="按标题或提问内容搜索"),
    include_archived: bool = Query(default=False),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    items = container.store.list_sessions(include_archived=include_archived, keyword=keyword)
    return {"total": len(items), "items": [_session_dict(r) for r in items]}


@router.get("/{session_id}")
def get_session(session_id: str, container: Container = Depends(get_container)) -> dict[str, Any]:
    rec = container.store.get_session(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")

    turns = []
    for t in container.store.list_turns(session_id):
        turns.append(
            {
                "id": t.id,
                "question": t.question,
                "answer": t.answer,
                "sql": t.sql,
                "metrics": _loads(t.metrics_json),
                "chart": _loads(t.chart_json),
                "result": _loads(t.rows_json),
                "row_count": t.row_count,
                "elapsed_ms": t.elapsed_ms,
                "status": t.status,
                "error": t.error,
                "created_at": t.created_at.isoformat(timespec="seconds") if t.created_at else None,
            }
        )
    return {**_session_dict(rec), "turns": turns}


@router.patch("/{session_id}")
def update_session(
    session_id: str,
    rename: RenameSessionRequest | None = None,
    archive: ArchiveSessionRequest | None = None,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    """重命名与归档合并在一个接口里：两者都是对会话元数据的局部修改。"""
    changed = False
    if rename is not None:
        changed |= container.store.rename_session(session_id, rename.title)
    if archive is not None:
        changed |= container.store.archive_session(session_id, archive.archived)
    if not changed:
        raise HTTPException(status_code=404, detail=f"会话不存在或未提供修改内容：{session_id}")
    rec = container.store.get_session(session_id)
    return _session_dict(rec) if rec else {"id": session_id, "updated": True}


@router.delete("/{session_id}")
def delete_session(session_id: str, container: Container = Depends(get_container)) -> dict[str, Any]:
    if not container.store.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return {"id": session_id, "deleted": True}


def _loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
