"""审计仓储的 SQLite 实现。

落应用库（app.db），与会话元数据同库但不同表：审计与会话的生命周期不一致
（会话可删、审计不可删），混在一张表里就只能靠软删除标记区分，久了必然出错。

建表走 Base.metadata.create_all：AuditLog 与其余模型同属一个 Base，容器
装配时 SqliteSessionStore 已经建过一次，这里再调一次是幂等的，目的是让本
仓储单独实例化时（脚本、测试）也能直接用。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession

from app.audit.events import AuditEvent
from app.db.engine import create_app_engine
from app.db.models import AuditLog, Base
from app.db.repository import AuditRepository


def _load_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _load_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump(value: Any) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _to_event(obj: AuditLog) -> AuditEvent:
    return AuditEvent(
        event_type=obj.event_type,
        outcome=obj.outcome,
        session_id=obj.session_id,
        question=obj.question,
        sql_text=obj.sql_text,
        rule_id=obj.rule_id,
        message=obj.message or "",
        source_tables=_load_list(obj.source_tables_json),
        row_count=obj.row_count,
        duration_ms=obj.duration_ms,
        detail=_load_dict(obj.detail_json),
        ts=obj.ts,
        id=obj.id,
    )


class SqliteAuditRepository(AuditRepository):
    """审计事件存储。只写不改，只查不删。"""

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine or create_app_engine()
        Base.metadata.create_all(self.engine)

    def record(self, event: AuditEvent) -> int:
        with OrmSession(self.engine) as s:
            obj = AuditLog(
                ts=event.ts or dt.datetime.now(),
                event_type=event.event_type,
                outcome=event.outcome,
                rule_id=event.rule_id,
                # 会话可能已被删除，也可能压根不存在（脚本、探针发起的查询）。
                # 这里不校验归属：审计宁可留下一个关联不上的 id，也不能丢记录
                session_id=event.session_id,
                question=event.question,
                sql_text=event.sql_text,
                message=event.message or "",
                source_tables_json=_dump(event.source_tables),
                row_count=event.row_count,
                duration_ms=event.duration_ms,
                detail_json=_dump(event.detail),
            )
            s.add(obj)
            s.commit()
            s.refresh(obj)
            event.id = obj.id
            event.ts = obj.ts
            return int(obj.id)

    def search(
        self,
        *,
        event_type: str | None = None,
        outcome: str | None = None,
        rule_id: str | None = None,
        session_id: str | None = None,
        keyword: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        with OrmSession(self.engine) as s:
            q = s.query(AuditLog)
            q = self._apply_filters(
                q,
                event_type=event_type,
                outcome=outcome,
                rule_id=rule_id,
                session_id=session_id,
                keyword=keyword,
                since=since,
                until=until,
            )
            rows = (
                q.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [_to_event(r) for r in rows]

    def count(
        self,
        *,
        event_type: str | None = None,
        outcome: str | None = None,
        rule_id: str | None = None,
        session_id: str | None = None,
        keyword: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> int:
        with OrmSession(self.engine) as s:
            q = self._apply_filters(
                s.query(AuditLog),
                event_type=event_type,
                outcome=outcome,
                rule_id=rule_id,
                session_id=session_id,
                keyword=keyword,
                since=since,
                until=until,
            )
            return int(q.count())

    def summary(
        self,
        *,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> dict[str, Any]:
        def group(column: Any, drop_null: bool = False) -> dict[str, int]:
            with OrmSession(self.engine) as s:
                q = s.query(column, func.count(AuditLog.id)).group_by(column)
                if since is not None:
                    q = q.filter(AuditLog.ts >= since)
                if until is not None:
                    q = q.filter(AuditLog.ts <= until)
                if drop_null:
                    q = q.filter(column.isnot(None))
                return {str(k): int(v) for k, v in q.all() if k is not None}

        with OrmSession(self.engine) as s:
            q = s.query(func.count(AuditLog.id))
            if since is not None:
                q = q.filter(AuditLog.ts >= since)
            if until is not None:
                q = q.filter(AuditLog.ts <= until)
            total = int(q.scalar() or 0)

        return {
            "total": total,
            "by_event_type": group(AuditLog.event_type),
            "by_outcome": group(AuditLog.outcome),
            # 规则编号只在守卫拦截时有值，空值不进统计，避免淹没真实分布
            "by_rule": group(AuditLog.rule_id, drop_null=True),
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        }

    def _apply_filters(
        self,
        q: Any,
        *,
        event_type: str | None,
        outcome: str | None,
        rule_id: str | None,
        session_id: str | None,
        keyword: str | None,
        since: dt.datetime | None,
        until: dt.datetime | None,
    ) -> Any:
        if event_type:
            q = q.filter(AuditLog.event_type == event_type)
        if outcome:
            q = q.filter(AuditLog.outcome == outcome)
        if rule_id:
            q = q.filter(AuditLog.rule_id == rule_id)
        if session_id:
            q = q.filter(AuditLog.session_id == session_id)
        if since is not None:
            q = q.filter(AuditLog.ts >= since)
        if until is not None:
            q = q.filter(AuditLog.ts <= until)
        if keyword:
            like = f"%{keyword}%"
            q = q.filter(
                AuditLog.question.like(like)
                | AuditLog.sql_text.like(like)
                | AuditLog.message.like(like)
            )
        return q
