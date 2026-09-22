"""反馈闭环接口（FR-G-1 至 FR-G-4）。

POST /api/feedback         提交点赞点踩，同 turn_id 重复提交按更新处理
GET  /api/feedback/summary  按会话或评分聚合反馈
GET  /api/feedback/export   把点踩记录导出为评测用例片段（FR-G-3）

反馈写入 app.db，与业务库分离（FR-G-4）。turn_id 关联 turns 表，
反馈只能针对已落库的轮次——重复提交改原因不重复入库。

导出的不是已确认用例，单条点踩不能证明口径错了：导出片段交由人工确认
后再写入 cases.yaml。导出格式与 cases.yaml 兼容，可直接复制粘贴评审。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import Container, get_container
from app.db.repository import FeedbackRecord

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

# 点踩原因枚举（FR-G-2）。前端按这五个选项渲染单选。
# 原因编码短、稳定，方便聚合统计；中文文案前端自行渲染。
DOWN_REASONS: tuple[str, ...] = (
    "wrong_metric",  # 口径错
    "wrong_field",   # 字段错
    "wrong_number",  # 数字错
    "off_topic",     # 答非所问
    "other",         # 其他
)
_RATING_VALUES = {"up", "down"}


class FeedbackRequest(BaseModel):
    turn_id: int = Field(description="目标轮次 ID，必须已落库")
    session_id: str = Field(description="目标会话 ID，用于校验归属")
    rating: Literal["up", "down"] = Field(description="点赞 up / 点踩 down")
    reason_code: str = Field(default="", description="点踩原因编码，点赞时为空")
    note: str = Field(default="", max_length=500, description="用户填写的正确问法或口径")


class FeedbackItem(BaseModel):
    id: int
    turn_id: int
    session_id: str
    rating: str
    reason_code: str
    note: str
    created_at: str
    updated_at: str


def _to_dict(rec: FeedbackRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "turn_id": rec.turn_id,
        "session_id": rec.session_id,
        "rating": rec.rating,
        "reason_code": rec.reason_code or "",
        "note": rec.note or "",
        "created_at": rec.created_at.isoformat(timespec="seconds") if rec.created_at else "",
        "updated_at": rec.updated_at.isoformat(timespec="seconds") if rec.updated_at else "",
    }


@router.post("")
def submit_feedback(
    req: FeedbackRequest, container: Container = Depends(get_container)
) -> dict[str, Any]:
    # 反馈只能针对已落库的轮次（FR-G-4）。会话不匹配也算非法，避免越权写入。
    session = container.store.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{req.session_id}")

    turn = _find_turn(container, req.session_id, req.turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail=f"轮次不存在或不在该会话下：{req.turn_id}")

    # 点赞时 reason_code 强制为空；点踩时校验是否在枚举内
    reason_code = ""
    if req.rating == "down":
        if req.reason_code and req.reason_code not in DOWN_REASONS:
            raise HTTPException(
                status_code=422,
                detail=f"reason_code 必须是 {DOWN_REASONS} 之一或为空",
            )
        reason_code = req.reason_code or "other"

    record = FeedbackRecord(
        turn_id=req.turn_id,
        session_id=req.session_id,
        rating=req.rating,
        reason_code=reason_code,
        note=(req.note or "").strip(),
    )
    fid = container.store.upsert_feedback(record)
    fresh = container.store.get_feedback_by_turn(req.turn_id)
    if fresh is None:
        # 落库异常不应发生（前面已 upsert），兜底返回提交回执
        return {"id": fid, "written": True}
    return {"id": fresh.id, "feedback": _to_dict(fresh), "written": True}


@router.get("/summary")
def feedback_summary(
    session_id: str | None = Query(default=None, description="按会话聚合"),
    rating: str | None = Query(default=None, description="按评分筛选：up/down"),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    if rating is not None and rating not in _RATING_VALUES:
        raise HTTPException(status_code=422, detail="rating 只能是 up 或 down")

    records = container.store.list_feedback(session_id=session_id, rating=rating)
    total = len(records)
    rating_counts = Counter(r.rating for r in records)
    # 点踩原因分布，按出现次数倒序，便于看「最常见的口径错在哪类」
    reason_counts = Counter(r.reason_code for r in records if r.rating == "down" and r.reason_code)
    items = [_to_dict(r) for r in records]
    return {
        "total": total,
        "up": rating_counts.get("up", 0),
        "down": rating_counts.get("down", 0),
        "reason_counts": dict(reason_counts.most_common()),
        "items": items,
    }


@router.get("/export")
def export_feedback(
    session_id: str | None = Query(default=None, description="只导出指定会话的点踩"),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    """把点踩记录导出为评测用例片段（FR-G-3）。

    导出的不是已确认用例，单条点踩不能证明口径错了：返回一段 yaml 片段，
    人工确认后再写入 cases.yaml。导出结构对齐 cases.yaml 的 recall 组字段，
    复制粘贴即可作为待确认条目放入评审。
    """
    records = container.store.list_feedback(session_id=session_id, rating="down")
    if not records:
        return {
            "count": 0,
            "yaml": "",
            "note": "没有点踩记录可导出；只有点踩（rating=down）才会转成评测条目。",
        }

    lines: list[str] = ["# FR-G-3 反馈导出：以下条目由点踩记录生成，需人工确认后再写入 cases.yaml"]
    exported = 0
    for rec in records:
        turn = _find_turn(container, rec.session_id, rec.turn_id)
        if turn is None:
            # 轮次被删了（CASCADE 会一起删 feedback，但保险起见再判一次）
            continue
        question = turn.question
        exported += 1
        note_hint = rec.note.strip()
        lines.append("")
        lines.append(f"- id: feedback-{rec.id}")
        lines.append(f"  question: {question!r}")
        lines.append(f"  rating: down")
        lines.append(f"  reason_code: {rec.reason_code or 'other'}")
        if note_hint:
            lines.append(f"  user_note: {note_hint!r}")
        lines.append(f"  turn_id: {rec.turn_id}")
        lines.append(f"  session_id: {rec.session_id}")
        lines.append(f"  feedback_at: {rec.created_at.isoformat(timespec='seconds') if rec.created_at else ''}")
        lines.append(f"  status: pending_review  # 待人工确认")
    yaml_text = "\n".join(lines)
    return {
        "count": exported,
        "yaml": yaml_text,
        "note": "导出条目需人工确认后再写入 cases.yaml；单条点踩不能证明口径错了。",
    }


def _find_turn(container: Container, session_id: str, turn_id: int) -> Any:
    """从落库的轮次里找一条，避免反馈针对未落库的轮次。"""
    turns = container.store.list_turns(session_id)
    for t in turns:
        if t.id == turn_id:
            return t
    return None
