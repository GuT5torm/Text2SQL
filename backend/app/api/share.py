"""只读分享接口（FR-N-3）。

POST /api/share         从已落库的轮次生成只读快照与凭据 token
GET  /api/share/{token} 读取快照，只返回已落库内容

「链接即凭据」——token 是 PK 也是访问凭据，不做用户校验。
快照存的是 turn 落库后的完整 ChatResult 字段（问句、结论、SQL、口径、
图表配置、明细表），访问时直接读出，不走 chat 链路，也不依赖业务库是否
在线。FR-N-1 PDF 导出要求的「不得依赖服务端二次查询」由这条快照实现。

FR-I-3：快照构造时对 result 再走一次脱敏管道。rows_json 在对话落库时已经
脱敏，这里属于「同一路径再过一遍」——mask_result 幂等，只兜底历史老数据。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.graph import derive_guard_notices
from app.api.deps import Container, get_container
from app.db.masking import mask_result
from app.db.repository import ShareRecord
from app.db.sqlite_repo import dump_json

router = APIRouter(prefix="/api/share", tags=["share"])

# 快照里要保留的字段。落库 JSON 里没有 stages / sql_reasoning 等运行时字段
# 是正常的（与历史回看一致）；快照只读「已落库」部分，不补链路运行时字段。
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "question",
    "answer",
    "sql",
    "status",
    "error",
    "metric_used",
    "caveats",
    "time_range",
    "chart",
    "result",  # { columns, rows, row_count }
    "row_count",
    "elapsed_ms",
    "baselines",
    "findings",
    "partial_period",
    "granularity",
    "source_tables",
    "guard_warnings",
    "guard_notices",
    "number_checks",
    "time_range_consistent",
    "masked",
    "masked_fields",
    "created_at",
)


class ShareRequest(BaseModel):
    turn_id: int = Field(description="要分享的轮次 ID")
    session_id: str = Field(description="轮次所属会话 ID，用于校验归属")
    expires_in_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="过期天数；为空表示永久有效",
    )


@router.post("")
def create_share(req: ShareRequest, container: Container = Depends(get_container)) -> dict[str, Any]:
    # 反馈只能针对已落库轮次，分享同理：快照就是落库内容的副本
    session = container.store.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{req.session_id}")

    turn = _find_turn(container, req.session_id, req.turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail=f"轮次不存在或不在该会话下：{req.turn_id}")

    snapshot = _build_snapshot(turn)
    # FR-I-3：分享快照与落库走同一脱敏管道（幂等，老数据兜底）。
    mask_result(snapshot["result"], container.catalog)
    snapshot["masked"] = bool(snapshot["result"].get("masked")) or bool(
        snapshot.get("masked")
    )
    snapshot["masked_fields"] = sorted(
        set(snapshot.get("masked_fields") or [])
        | set(snapshot["result"].get("masked_fields") or [])
    )
    expires_at = None
    if req.expires_in_days is not None:
        expires_at = dt.datetime.now() + dt.timedelta(days=req.expires_in_days)

    token = uuid.uuid4().hex
    share = ShareRecord(
        token=token,
        turn_id=req.turn_id,
        session_id=req.session_id,
        snapshot_json=dump_json(snapshot),
        expires_at=expires_at,
        revoked=0,
    )
    container.store.create_share(share)
    return {
        "token": token,
        "turn_id": req.turn_id,
        "session_id": req.session_id,
        "expires_at": expires_at.isoformat(timespec="seconds") if expires_at else None,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


@router.get("/{token}")
def read_share(token: str, container: Container = Depends(get_container)) -> dict[str, Any]:
    rec = container.store.get_share(token)
    if rec is None:
        raise HTTPException(status_code=404, detail="分享链接不存在")
    if rec.revoked:
        raise HTTPException(status_code=410, detail="分享链接已被撤销")
    if rec.expires_at is not None and rec.expires_at < dt.datetime.now():
        raise HTTPException(status_code=410, detail="分享链接已过期")

    try:
        snapshot = json.loads(rec.snapshot_json) if rec.snapshot_json else {}
    except json.JSONDecodeError:
        # 落库异常的兜底：返回最小可用结构而不是 500，让前端至少能告知用户
        snapshot = {}

    return {
        "token": rec.token,
        "turn_id": rec.turn_id,
        "session_id": rec.session_id,
        "snapshot": snapshot,
        "expires_at": rec.expires_at.isoformat(timespec="seconds") if rec.expires_at else None,
        "created_at": rec.created_at.isoformat(timespec="seconds") if rec.created_at else None,
    }


def _find_turn(container: Container, session_id: str, turn_id: int) -> Any:
    """从落库的轮次里找一条，避免分享未落库的内容。"""
    turns = container.store.list_turns(session_id)
    for t in turns:
        if t.id == turn_id:
            return t
    return None


def _build_snapshot(turn: Any) -> dict[str, Any]:
    """从已落库的 TurnRecord 构造只读快照。

    快照只装「已落库」部分：stages / sql_reasoning / fix_reasoning 等运行时
    字段在前端聊天流里有，历史回看时没。分享页按历史回看同款渲染，缺这些
    字段降级即可，不要塞假值。
    """
    metrics = _loads(turn.metrics_json) or {}
    rows = _loads(turn.rows_json) or {}
    baselines = _loads(turn.baselines_json)
    findings = _loads(turn.findings_json) or []
    guard_warnings = _loads(turn.guard_warnings_json) or []

    return {
        "question": turn.question,
        "answer": turn.answer or "",
        "sql": turn.sql or "",
        "status": turn.status or "ok",
        "error": turn.error or "",
        "metric_used": metrics.get("metric_used") or [],
        "caveats": metrics.get("caveats") or [],
        "time_range": metrics.get("time_range"),
        "chart": _loads(turn.chart_json),
        "result": {
            "columns": rows.get("columns") or [],
            "rows": rows.get("rows") or [],
            "row_count": rows.get("row_count") or turn.row_count or 0,
        },
        "row_count": turn.row_count or 0,
        "elapsed_ms": turn.elapsed_ms or 0.0,
        "baselines": baselines,
        "findings": findings,
        "partial_period": _baselines_field(baselines, "partial_period", False),
        "granularity": _baselines_field(baselines, "granularity", None),
        "source_tables": _loads(turn.source_tables_json) or [],
        # FR-I-2：守卫双通道与本轮返回同源
        "guard_warnings": guard_warnings,
        "guard_notices": derive_guard_notices(
            {"sql_error": turn.error, "guard_warnings": guard_warnings}
        ),
        # FR-B-3 / FR-D-5 / FR-I-3
        "number_checks": metrics.get("number_checks") or [],
        "time_range_consistent": metrics.get("time_range_consistent"),
        "masked": bool(metrics.get("masked")),
        "masked_fields": metrics.get("masked_fields") or [],
        "created_at": turn.created_at.isoformat(timespec="seconds") if turn.created_at else None,
    }


def _loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _baselines_field(baselines: Any, field: str, default: Any) -> Any:
    if not isinstance(baselines, dict):
        return default
    value = baselines.get(field)
    return value if value is not None else default
