"""审计日志查询接口。

三个出口各有分工：

  /api/audit         逐条检索，排查用。带总数供分页；
  /api/audit/summary 按类型 / 结果 / 规则编号计数，看趋势用；
  /api/audit/export  按当前筛选导出 CSV 或 JSON，留档与对外用。

本模块不做鉴权——项目没有用户体系，单机演示环境下接口本身即访问边界。
部署到共享环境时必须在外层加访问控制，否则审计日志会比业务数据更敏感：
它同时记录了问句与语句原文。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.api.deps import Container, get_container
from app.audit.events import EVENT_TYPES, OUTCOMES, AuditEvent
from app.db.repository import AuditRepository

router = APIRouter(prefix="/api/audit", tags=["audit"])

# 单次导出上限。检索接口默认 100 是为了页面列表，导出是一次性动作，
# 放宽到 5000 足够覆盖演示规模，又不至于让一次请求拖出整表。
EXPORT_LIMIT = 5000

_CSV_HEADERS = [
    "id",
    "ts",
    "event_type",
    "outcome",
    "rule_id",
    "session_id",
    "question",
    "sql_text",
    "message",
    "source_tables",
    "row_count",
    "duration_ms",
]


def _repo(container: Container) -> AuditRepository:
    """取审计仓储。未启用或库不可用时明确报错，而不是返回空列表——

    空列表会让人以为「这段时间确实没有拦截」，与「审计根本没在工作」是
    两件完全不同的事，接口层必须区分开。
    """
    if container.audit is None:
        raise HTTPException(
            status_code=409,
            detail="审计未启用（AUDIT_ENABLED=false）或审计库不可用，当前无审计数据",
        )
    return container.audit


def _validate_choice(value: str | None, allowed: tuple[str, ...], field: str) -> str | None:
    """枚举参数校验。

    拼错的枚举如果静默忽略，接口会返回「全部记录」，排查时反而更费时间，
    因此直接 400 并把可选值回给调用方。
    """
    if not value:
        return None
    if value not in allowed:
        raise HTTPException(
            status_code=400, detail=f"{field} 取值 {value!r} 无效，可选值：{list(allowed)}"
        )
    return value


def _to_dict(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "ts": event.ts,
        "event_type": event.event_type,
        "outcome": event.outcome,
        "rule_id": event.rule_id,
        "session_id": event.session_id,
        "question": event.question,
        "sql_text": event.sql_text,
        "message": event.message,
        "source_tables": list(event.source_tables),
        "row_count": event.row_count,
        "duration_ms": event.duration_ms,
        "detail": dict(event.detail),
    }


def _csv_text(events: list[AuditEvent]) -> str:
    """导出 CSV。

    走 csv.writer 而不是手工拼串：问句与 SQL 里都可能有逗号、引号与换行，
    手工拼出来的文件下游打不开。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADERS)
    for e in events:
        writer.writerow(
            [
                e.id,
                e.ts.isoformat() if e.ts else "",
                e.event_type,
                e.outcome,
                e.rule_id or "",
                e.session_id or "",
                e.question or "",
                e.sql_text or "",
                e.message or "",
                "、".join(e.source_tables),
                "" if e.row_count is None else e.row_count,
                "" if e.duration_ms is None else round(e.duration_ms, 1),
            ]
        )
    return buf.getvalue()


def _filters(
    event_type: str | None,
    outcome: str | None,
    rule_id: str | None,
    session_id: str | None,
    keyword: str | None,
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> dict[str, Any]:
    return {
        "event_type": _validate_choice(event_type, EVENT_TYPES, "event_type"),
        "outcome": _validate_choice(outcome, OUTCOMES, "outcome"),
        "rule_id": rule_id or None,
        "session_id": session_id or None,
        "keyword": keyword.strip() if keyword and keyword.strip() else None,
        "since": since,
        "until": until,
    }


@router.get("")
def list_audit(
    event_type: str | None = None,
    outcome: str | None = None,
    rule_id: str | None = None,
    session_id: str | None = None,
    keyword: str | None = Query(None, max_length=200),
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    """按条件检索审计记录，按时间倒序。"""
    repo = _repo(container)
    flt = _filters(event_type, outcome, rule_id, session_id, keyword, since, until)
    events = repo.search(limit=limit, offset=offset, **flt)
    total = repo.count(**flt)
    return {
        "items": [_to_dict(e) for e in events],
        "total": total,
        "limit": limit,
        "offset": offset,
        # 事件类型清单随响应返回，前端或脚本不必硬编码
        "event_types": list(EVENT_TYPES),
        "outcomes": list(OUTCOMES),
    }


@router.get("/summary")
def audit_summary(
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    """按事件类型、结果与规则编号计数。"""
    return _repo(container).summary(since=since, until=until)


@router.get("/export")
def export_audit(
    format: str = "csv",
    event_type: str | None = None,
    outcome: str | None = None,
    rule_id: str | None = None,
    session_id: str | None = None,
    keyword: str | None = Query(None, max_length=200),
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    container: Container = Depends(get_container),
) -> Response:
    """按当前筛选导出。format 取 csv 或 json。"""
    repo = _repo(container)
    flt = _filters(event_type, outcome, rule_id, session_id, keyword, since, until)
    events = repo.search(limit=EXPORT_LIMIT, offset=0, **flt)

    if format == "json":
        return Response(
            content=_dump_json(events),
            media_type="application/json",
            headers=_attachment("audit-log.json"),
        )
    if format == "csv":
        return Response(
            # 带 BOM：Excel 默认按本地编码打开无 BOM 的 UTF-8，中文会乱码
            content="\ufeff" + _csv_text(events),
            media_type="text/csv; charset=utf-8",
            headers=_attachment("audit-log.csv"),
        )
    raise HTTPException(status_code=400, detail="format 只支持 csv 或 json")


def _attachment(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


def _dump_json(events: list[AuditEvent]) -> str:
    payload = {
        "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(events),
        "items": [_to_dict(e) for e in events],
    }
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)
