"""Repository 的 SQLite 实现。

执行期加固在这里落地，包括三条守卫无法覆盖的规则：

  R8_STATEMENT_TIMEOUT  用 SQLite 的 progress handler 实现。每执行若干条
                        虚拟机指令回调一次，超时即返回非零中断查询，
                        比在外部起线程 kill 更干净，也不会留下半开的事务。
     行数上限          游标只取前 max_rows 行，超出部分置 truncated 标记。
     结果体积上限      单行里放一个超大值（例如几十 MB 的 blob）时，行数
                        上限拦不住，序列化阶段会一次性吃掉数倍内存。这里
                        在返回前估算体积并拒绝，是守卫函数白名单之外的
                        第二道兜底。

连接本身由 db/engine.py 以只读模式建立，本模块不做任何写操作。
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.db.engine import create_app_engine, create_business_engine
from app.db.models import Base, Session, Turn
from app.db.repository import (
    QueryError,
    QueryRepository,
    QueryResult,
    QueryTimeout,
    SessionRecord,
    SessionStore,
    TurnRecord,
)

# progress handler 的回调间隔（虚拟机指令数）。太小会拖慢查询，
# 太大会让超时判断变粗，1000 在演示规模下延迟足够低。
_PROGRESS_INTERVAL = 1000


def _dedupe_columns(columns: list[str]) -> list[str]:
    """重名列补序号后缀，保证列名在结果集内唯一。

    SELECT c.name AS n, p.name AS n 在 SQL 里合法，但结果集按列名组装成字典时
    后一列会直接覆盖前一列，前端表头也会出现两个同名的列各显示同一个值。
    后缀只对第二个及以后的重名列追加，未重名的列名保持原样，界面仍与 SQL 对得上。
    """
    used: set[str] = set()
    out: list[str] = []
    for raw in columns:
        base = raw or "column"
        name = base
        idx = 1
        while name in used:  # 连原列名都已存在时继续加序号，避免补出新的重名
            idx += 1
            name = f"{base}_{idx}"
        used.add(name)
        out.append(name)
    return out


def _estimate_bytes(columns: list[str], rows: list[dict[str, Any]]) -> int:
    """结果集体积估算。

    不真的序列化再量长度：行数已有硬上限，能把体积顶穿的是单个超大值，
    估算只需覆盖到这个量级。字符串按 UTF-8 最坏情况（每字符 3 字节）计。
    """
    total = 0
    for row in rows:
        for key, value in row.items():
            total += len(key) * 3
            if value is None:
                total += 4
            elif isinstance(value, (bytes, bytearray)):
                total += len(value)
            elif isinstance(value, str):
                total += len(value) * 3
            else:
                total += 16
    # 列名本身也要占位，空结果集不该估成 0 以外的误导值
    return total + sum(len(c) * 3 for c in columns)


class SqliteQueryRepository(QueryRepository):
    """业务库只读查询。"""

    def __init__(self, engine: Engine | None = None):
        self.engine = engine or create_business_engine()

    # —— 内部 ——
    def _run(self, sql: str, params: dict[str, Any] | None, max_rows: int) -> QueryResult:
        settings = get_settings()
        deadline = time.monotonic() + settings.sql_timeout_seconds
        started = time.monotonic()
        # 累计执行的指令数，用于扫描量上限的兜底
        counter = {"instructions": 0}

        # 中断原因要区分开：超时与扫描量超标的重试策略不同，
        # 笼统报「超时」会让 fix 节点往错误的方向改
        state = {"reason": ""}

        def _progress() -> int:
            counter["instructions"] += _PROGRESS_INTERVAL
            if time.monotonic() > deadline:
                state["reason"] = (
                    f"查询超过 {settings.sql_timeout_seconds:g} 秒被中断。"
                    "请简化查询范围或缩小时间区间后重试。"
                )
                return 1
            if counter["instructions"] > settings.sql_scan_limit:
                state["reason"] = (
                    f"查询扫描量超过上限（{settings.sql_scan_limit} 条指令）被中断。"
                    "请缩小时间区间或减少关联表后重试。"
                )
                return 1
            return 0

        conn = self.engine.raw_connection()
        try:
            dbapi = getattr(conn, "dbapi_connection", conn)
            dbapi.set_progress_handler(_progress, _PROGRESS_INTERVAL)
            try:
                cur = dbapi.cursor()
                cur.execute(sql, params or {})
                columns = _dedupe_columns([d[0] for d in (cur.description or [])])
                fetched = cur.fetchmany(max_rows + 1)
                cur.close()
            except Exception as exc:  # 数据库原始报错要原样保留给 fix 节点
                message = str(exc)
                if "interrupted" in message.lower():
                    raise QueryTimeout(
                        state["reason"] or f"查询被中断：{message}"
                    ) from exc
                raise QueryError(f"SQLite 执行失败：{message}") from exc
            finally:
                dbapi.set_progress_handler(None, 0)
        finally:
            conn.close()

        truncated = len(fetched) > max_rows
        rows = [dict(zip(columns, row)) for row in fetched[:max_rows]]

        size = _estimate_bytes(columns, rows)
        limit = settings.sql_max_result_bytes
        if size > limit:
            # 走到这里说明守卫的函数白名单之外仍有漏网的写法。直接拒绝而不是截断：
            # 截断一个几十 MB 的单值没有意义，用户需要的是换一种问法
            raise QueryError(
                f"结果集体积约 {size / 1024 / 1024:.1f} MB，超过上限 "
                f"{limit / 1024 / 1024:.0f} MB，已拒绝返回。"
                "请减少返回的字段或改用聚合查询。"
            )

        return QueryResult(
            sql=sql,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            bytes_estimate=size,
        )

    # —— 接口实现 ——
    def execute_readonly(self, sql: str, max_rows: int | None = None) -> QueryResult:
        settings = get_settings()
        return self._run(sql, None, max_rows or settings.sql_max_rows)

    def list_tables(self) -> list[str]:
        # sqlite_ 前缀是 SQLite 内部表（sqlite_stat1 等），不参与语义层一致性核对
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
            ).fetchall()
        return [r[0] for r in rows]

    def count_rows(self, table: str) -> int:
        # 表名不来自用户输入，但仍做一次形态检查，避免这里的字符串拼接被滥用
        if not table.replace("_", "").isalnum():
            raise ValueError(f"非法的表名：{table}")
        with self.engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


class SqliteSessionStore(SessionStore):
    """应用库的会话与问答记录。"""

    def __init__(self, engine: Engine | None = None):
        self.engine = engine or create_app_engine()
        Base.metadata.create_all(self.engine)

    # —— 会话 ——
    def create_session(self, title: str = "新会话") -> SessionRecord:
        with OrmSession(self.engine) as s:
            obj = Session(id=str(uuid.uuid4()), title=title)
            s.add(obj)
            s.commit()
            s.refresh(obj)
            return self._to_record(obj, 0)

    def get_session(self, session_id: str) -> SessionRecord | None:
        with OrmSession(self.engine) as s:
            obj = s.get(Session, session_id)
            if obj is None:
                return None
            n = s.query(Turn).filter(Turn.session_id == session_id).count()
            return self._to_record(obj, n)

    def list_sessions(
        self, include_archived: bool = False, keyword: str | None = None
    ) -> list[SessionRecord]:
        with OrmSession(self.engine) as s:
            q = s.query(Session)
            if not include_archived:
                q = q.filter(Session.archived == 0)
            if keyword:
                # 标题或任一轮提问命中即算命中
                like = f"%{keyword}%"
                sub = s.query(Turn.session_id).filter(Turn.question.like(like))
                q = q.filter(Session.title.like(like) | Session.id.in_(sub))
            objs = q.order_by(Session.updated_at.desc()).all()
            counts = dict(
                s.query(Turn.session_id, func.count(Turn.id))
                .group_by(Turn.session_id)
                .all()
            )
            return [self._to_record(o, counts.get(o.id, 0)) for o in objs]

    def rename_session(self, session_id: str, title: str) -> bool:
        with OrmSession(self.engine) as s:
            obj = s.get(Session, session_id)
            if obj is None:
                return False
            obj.title = title[:120]
            s.commit()
            return True

    def archive_session(self, session_id: str, archived: bool = True) -> bool:
        with OrmSession(self.engine) as s:
            obj = s.get(Session, session_id)
            if obj is None:
                return False
            obj.archived = 1 if archived else 0
            s.commit()
            return True

    def delete_session(self, session_id: str) -> bool:
        with OrmSession(self.engine) as s:
            obj = s.get(Session, session_id)
            if obj is None:
                return False
            s.delete(obj)
            s.commit()
            return True

    # —— 问答记录 ——
    def add_turn(self, turn: TurnRecord) -> int:
        with OrmSession(self.engine) as s:
            obj = Turn(
                session_id=turn.session_id,
                question=turn.question,
                answer=turn.answer,
                sql=turn.sql,
                metrics_json=turn.metrics_json,
                chart_json=turn.chart_json,
                rows_json=turn.rows_json,
                row_count=turn.row_count,
                elapsed_ms=turn.elapsed_ms,
                status=turn.status,
                error=turn.error,
            )
            s.add(obj)
            # 会话标题取首轮问题，便于历史列表辨认
            sess = s.get(Session, turn.session_id)
            if sess is not None:
                if sess.title == "新会话" and turn.question:
                    sess.title = turn.question[:40]
                sess.updated_at = dt.datetime.now()
            s.commit()
            # 回填自增主键，让调用方拿到的是已经落库的记录而不是半截对象
            turn.id = obj.id
            return obj.id

    def list_turns(self, session_id: str) -> list[TurnRecord]:
        with OrmSession(self.engine) as s:
            objs = (
                s.query(Turn)
                .filter(Turn.session_id == session_id)
                .order_by(Turn.id)
                .all()
            )
            return [
                TurnRecord(
                    id=o.id,
                    session_id=o.session_id,
                    question=o.question,
                    answer=o.answer,
                    sql=o.sql,
                    metrics_json=o.metrics_json,
                    chart_json=o.chart_json,
                    rows_json=o.rows_json,
                    row_count=o.row_count,
                    elapsed_ms=o.elapsed_ms,
                    status=o.status,
                    error=o.error,
                    created_at=o.created_at,
                )
                for o in objs
            ]

    @staticmethod
    def _to_record(obj: Session, turn_count: int) -> SessionRecord:
        return SessionRecord(
            id=obj.id,
            title=obj.title,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
            archived=obj.archived,
            turn_count=turn_count,
        )


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
