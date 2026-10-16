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

from app.agent.graph import derive_guard_notices
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
                # 历史回看时 AnswerCard 据此调用 feedback / share 接口。
                # 与 chat.py::_persist 回填到 result 的 turn_id 同字段名同语义。
                "turn_id": t.id,
                "question": t.question,
                # FR-H-3：原话与改写句回放。老库两列为空，前端按「未记录」降级
                "original_question": t.original_question or t.question,
                "rewritten_question": t.rewritten_question or "",
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
                # —— FR-H-2 历史回看补齐 ——
                # 阶段时间线、原始语句、守卫告警、修正次数与截断标记。
                # 老库迁移会缺这些字段，_loads 已能容忍 None；空字符串视作「回看时没有」。
                "stages": _loads(t.stages_json) or [],
                "original_sql": t.original_sql or "",
                "guard_warnings": _loads(t.guard_warnings_json) or [],
                # FR-I-2：与本轮返回同一条 derive_guard_notices 路径，
                # rejected 从 error 文本还原，rewritten 来自守卫告警 JSON。
                "guard_notices": derive_guard_notices(
                    {
                        "sql_error": t.error,
                        "guard_warnings": _loads(t.guard_warnings_json) or [],
                    }
                ),
                "retry_count": int(t.retry_count or 0),
                "truncated": bool(t.truncated),
                "persist_row_count": int(t.persist_row_count or 0),
                "source_tables": _loads(t.source_tables_json) or [],
                "findings": _loads(t.findings_json) or [],
                "baselines": _loads(t.baselines_json),
                # partial_period 与 granularity 存在 baselines_json 内，前端 ChatResult
                # 把它们声明在顶层，这里提取出来保持字段契约一致（FR-M-2 历史回看）。
                "partial_period": _baselines_field(t.baselines_json, "partial_period", False),
                "granularity": _baselines_field(t.baselines_json, "granularity", None),
                # node_path 既可能是字符串（老写法）也可能是 list（chat.py 落库时 dump_json 的形态）
                # _loads 容忍字符串：JSON 解析失败则当 None；前端拿到字符串时自行 split。
                "node_path": _loads(t.node_path),
                # FR-B-3 / FR-D-5 / FR-I-3：这三个标记存在 metrics_json 内，
                # 提到顶层与本轮 ChatResult 契约保持一致。
                "number_checks": (_loads(t.metrics_json) or {}).get("number_checks") or [],
                "time_range_consistent": (_loads(t.metrics_json) or {}).get(
                    "time_range_consistent"
                ),
                # 槽位覆盖核对结果，与本轮 ChatResult 契约保持一致
                "slot_gaps": (_loads(t.metrics_json) or {}).get("slot_gaps") or [],
                "masked": bool((_loads(t.metrics_json) or {}).get("masked")),
                "masked_fields": (_loads(t.metrics_json) or {}).get("masked_fields") or [],
                # FR-A-5：结构化槽位历史回看
                "slots": _loads(t.slots_json) or {},
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


def _baselines_field(baselines_json: str | None, field: str, default: Any) -> Any:
    """从 baselines_json 中提取单个顶层字段。

    partial_period 与 granularity 在 compute_baselines 里写入 baselines dict，
    落库后存在 baselines_json 列。历史回看时前端 ChatResult 把它们声明在顶层，
    这里提取出来保持字段契约一致。老库 baselines_json 为空时返回默认值。
    """
    data = _loads(baselines_json)
    if not isinstance(data, dict):
        return default
    value = data.get(field)
    return value if value is not None else default
