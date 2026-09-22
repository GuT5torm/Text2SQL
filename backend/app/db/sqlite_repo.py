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
from app.db.models import Alert, AlertRule, Base, Feedback, Session, Share, Turn
from app.db.repository import (
    AlertRecord,
    AlertRuleRecord,
    FeedbackRecord,
    QueryError,
    QueryRepository,
    QueryResult,
    QueryTimeout,
    SessionRecord,
    SessionStore,
    ShareRecord,
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
                    f"R8 查询超过 {settings.sql_timeout_seconds:g} 秒被中断。"
                    "请简化查询范围或缩小时间区间后重试。"
                )
                return 1
            if counter["instructions"] > settings.sql_scan_limit:
                state["reason"] = (
                    f"R8 查询扫描量超过上限（{settings.sql_scan_limit} 条指令）被中断。"
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
        # create_all 不会改旧库结构，已存在的 turns 表按需补齐新列。
        # 老库迁移与新库初始化共用同一份 add_column 清单，避免两边漂移。
        _ensure_turn_columns(self.engine)

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

    # FR-H-5：归档保留期自动清理。归档超过 N 天未恢复的会话物理删除。
    # 采用惰性清理——只在 list_sessions 与 archive_session 触发时顺手扫一次，
    # 避免引入后台定时任务与本机 cron 依赖。
    def purge_expired_archived(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = dt.datetime.now() - dt.timedelta(days=retention_days)
        with OrmSession(self.engine) as s:
            # SQLite 没有 DATE_SUB，用 strftime 比较；updated_at 形如 2026-09-22T...
            stale = (
                s.query(Session)
                .filter(Session.archived == 1)
                .filter(Session.updated_at < cutoff)
                .all()
            )
            for obj in stale:
                # 关联 turns 由外键 ON DELETE CASCADE 清理（schema.sql 已设置）
                s.delete(obj)
            if stale:
                s.commit()
            return len(stale)

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
                stages_json=turn.stages_json,
                original_sql=turn.original_sql,
                guard_warnings_json=turn.guard_warnings_json,
                retry_count=turn.retry_count,
                truncated=turn.truncated,
                persist_row_count=turn.persist_row_count,
                source_tables_json=turn.source_tables_json,
                findings_json=turn.findings_json,
                baselines_json=turn.baselines_json,
                node_path=turn.node_path,
                slots_json=turn.slots_json,
                original_question=turn.original_question,
                rewritten_question=turn.rewritten_question,
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
                    stages_json=o.stages_json,
                    original_sql=o.original_sql,
                    guard_warnings_json=o.guard_warnings_json,
                    retry_count=int(o.retry_count or 0),
                    truncated=int(o.truncated or 0),
                    persist_row_count=int(o.persist_row_count or 0),
                    source_tables_json=o.source_tables_json,
                    findings_json=o.findings_json,
                    baselines_json=o.baselines_json,
                    node_path=o.node_path,
                    slots_json=o.slots_json,
                    original_question=o.original_question,
                    rewritten_question=o.rewritten_question,
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

    # —— FR-G 反馈闭环 ——
    def upsert_feedback(self, feedback: FeedbackRecord) -> int:
        """同一 turn_id 至多一条。命中即更新（rating / reason_code / note）。

        UNIQUE(turn_id) 让并发场景也安全：第二次 insert 会被约束拦下转 update。
        这里直接按 turn_id 查再决定 insert/update，避免依赖方言特定的 ON CONFLICT。
        """
        with OrmSession(self.engine) as s:
            existing = (
                s.query(Feedback).filter(Feedback.turn_id == feedback.turn_id).one_or_none()
            )
            now = dt.datetime.now()
            if existing is None:
                obj = Feedback(
                    turn_id=feedback.turn_id,
                    session_id=feedback.session_id,
                    rating=feedback.rating,
                    reason_code=feedback.reason_code,
                    note=feedback.note,
                    created_at=now,
                    updated_at=now,
                )
                s.add(obj)
                s.commit()
                s.refresh(obj)
                return obj.id
            existing.rating = feedback.rating
            existing.reason_code = feedback.reason_code
            existing.note = feedback.note
            existing.updated_at = now
            s.commit()
            return existing.id

    def get_feedback_by_turn(self, turn_id: int) -> FeedbackRecord | None:
        with OrmSession(self.engine) as s:
            obj = s.query(Feedback).filter(Feedback.turn_id == turn_id).one_or_none()
            return self._to_feedback_record(obj) if obj else None

    def list_feedback(
        self,
        session_id: str | None = None,
        rating: str | None = None,
    ) -> list[FeedbackRecord]:
        with OrmSession(self.engine) as s:
            q = s.query(Feedback)
            if session_id is not None:
                q = q.filter(Feedback.session_id == session_id)
            if rating is not None:
                q = q.filter(Feedback.rating == rating)
            objs = q.order_by(Feedback.created_at.desc()).all()
            return [self._to_feedback_record(o) for o in objs]

    @staticmethod
    def _to_feedback_record(obj: Feedback) -> FeedbackRecord:
        return FeedbackRecord(
            id=obj.id,
            turn_id=obj.turn_id,
            session_id=obj.session_id,
            rating=obj.rating,
            reason_code=obj.reason_code or "",
            note=obj.note or "",
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )

    # —— FR-N-3 只读分享 ——
    def create_share(self, share: ShareRecord) -> str:
        with OrmSession(self.engine) as s:
            obj = Share(
                token=share.token,
                turn_id=share.turn_id,
                session_id=share.session_id,
                snapshot_json=share.snapshot_json or "{}",
                expires_at=share.expires_at,
                revoked=share.revoked,
                created_at=share.created_at or dt.datetime.now(),
            )
            s.add(obj)
            s.commit()
            return obj.token

    def get_share(self, token: str) -> ShareRecord | None:
        with OrmSession(self.engine) as s:
            obj = s.get(Share, token)
            if obj is None:
                return None
            return ShareRecord(
                token=obj.token,
                turn_id=obj.turn_id,
                session_id=obj.session_id,
                snapshot_json=obj.snapshot_json or "{}",
                expires_at=obj.expires_at,
                revoked=obj.revoked,
                created_at=obj.created_at,
            )

    # —— FR-O 异常订阅 ——
    @staticmethod
    def _to_alert_rule_record(obj: AlertRule) -> AlertRuleRecord:
        try:
            dimensions = json.loads(obj.dimensions_json or "[]")
        except json.JSONDecodeError:
            dimensions = []
        return AlertRuleRecord(
            id=obj.id,
            metric=obj.metric,
            dimensions=dimensions if isinstance(dimensions, list) else [],
            comparison=obj.comparison,
            threshold=obj.threshold,
            severity=obj.severity,
            enabled=bool(obj.enabled),
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )

    def list_alert_rules(self, enabled_only: bool = False) -> list[AlertRuleRecord]:
        with OrmSession(self.engine) as s:
            q = s.query(AlertRule)
            if enabled_only:
                q = q.filter(AlertRule.enabled == 1)
            objs = q.order_by(AlertRule.id.asc()).all()
            return [self._to_alert_rule_record(o) for o in objs]

    def get_alert_rule(self, rule_id: int) -> AlertRuleRecord | None:
        with OrmSession(self.engine) as s:
            obj = s.get(AlertRule, rule_id)
            return self._to_alert_rule_record(obj) if obj else None

    def upsert_alert_rule(self, rule: AlertRuleRecord) -> AlertRuleRecord:
        with OrmSession(self.engine) as s:
            if rule.id:
                obj = s.get(AlertRule, rule.id)
                if obj is None:
                    raise KeyError(f"规则不存在：{rule.id}")
            else:
                obj = AlertRule()
                s.add(obj)
            obj.metric = rule.metric
            obj.dimensions_json = dump_json(rule.dimensions or [])
            obj.comparison = rule.comparison
            obj.threshold = rule.threshold
            obj.severity = rule.severity
            obj.enabled = 1 if rule.enabled else 0
            s.commit()
            s.refresh(obj)
            return self._to_alert_rule_record(obj)

    def delete_alert_rule(self, rule_id: int) -> bool:
        with OrmSession(self.engine) as s:
            obj = s.get(AlertRule, rule_id)
            if obj is None:
                return False
            s.delete(obj)
            s.commit()
            return True

    @staticmethod
    def _to_alert_record(obj: Alert) -> AlertRecord:
        try:
            hits = json.loads(obj.slice_json or "[]")
        except json.JSONDecodeError:
            hits = []
        return AlertRecord(
            id=obj.id,
            rule_id=obj.rule_id,
            evaluated_at=obj.evaluated_at,
            status=obj.status,
            hit=bool(obj.hit),
            metric=obj.metric or "",
            hits=hits if isinstance(hits, list) else [],
            reason=obj.reason or "",
            seen=bool(obj.seen),
            created_at=obj.created_at,
        )

    def insert_alert_evaluation(self, alert: AlertRecord) -> AlertRecord:
        with OrmSession(self.engine) as s:
            existing = (
                s.query(Alert)
                .filter(
                    Alert.rule_id == alert.rule_id,
                    Alert.evaluated_at == alert.evaluated_at,
                )
                .one_or_none()
            )
            if existing is not None:
                # FR-O-3：调度器重复触发同一周期时不产生新行，原样回传旧记录。
                return self._to_alert_record(existing)
            first_value = None
            if alert.hits:
                v = alert.hits[0].get("value")
                first_value = float(v) if isinstance(v, (int, float)) else None
            obj = Alert(
                rule_id=alert.rule_id,
                evaluated_at=alert.evaluated_at,
                status=alert.status,
                hit=1 if alert.hit else 0,
                metric=alert.metric or "",
                slice_json=dump_json(alert.hits),
                value=first_value,
                baseline_json=None,
                reason=alert.reason or "",
                seen=1 if alert.seen else 0,
            )
            s.add(obj)
            s.commit()
            s.refresh(obj)
            return self._to_alert_record(obj)

    def list_alerts(
        self,
        seen: bool | None = None,
        rule_id: int | None = None,
        limit: int = 100,
    ) -> list[AlertRecord]:
        with OrmSession(self.engine) as s:
            q = s.query(Alert)
            if seen is not None:
                q = q.filter(Alert.seen == (1 if seen else 0))
            if rule_id is not None:
                q = q.filter(Alert.rule_id == rule_id)
            objs = q.order_by(Alert.evaluated_at.desc(), Alert.id.desc()).limit(limit).all()
            return [self._to_alert_record(o) for o in objs]

    def count_unseen_alerts(self) -> int:
        with OrmSession(self.engine) as s:
            return s.query(Alert).filter(Alert.seen == 0).count()

    def mark_alert_seen(self, alert_id: int) -> bool:
        with OrmSession(self.engine) as s:
            obj = s.get(Alert, alert_id)
            if obj is None:
                return False
            obj.seen = 1
            s.commit()
            return True

    def latest_alert_eval(self) -> AlertRecord | None:
        with OrmSession(self.engine) as s:
            obj = (
                s.query(Alert).order_by(Alert.evaluated_at.desc(), Alert.id.desc()).first()
            )
            return self._to_alert_record(obj) if obj else None

    def consecutive_alert_failures(self) -> int:
        """从最近一次求值向前数连续 error 次数；最近一次成功即为 0。"""
        with OrmSession(self.engine) as s:
            rows = (
                s.query(Alert.status, Alert.evaluated_at)
                .order_by(Alert.evaluated_at.desc(), Alert.id.desc())
                .all()
            )
        count = 0
        for status, _at in rows:
            if status == "error":
                count += 1
            else:
                break
        return count


# 当再给 turns 表加新字段时，同时更新 _TURN_MIGRATIONS 列名 / 类型，
# 这样旧库迁移可继续不依赖手写 SQL。create_all 会自动在空库建好全部字段，
# 旧库只补缺失列。
_TURN_MIGRATIONS: list[tuple[str, str]] = [
    ("stages_json", "TEXT"),
    ("original_sql", "TEXT"),
    ("guard_warnings_json", "TEXT"),
    ("retry_count", "INTEGER"),
    ("truncated", "INTEGER"),
    ("persist_row_count", "INTEGER"),
    ("source_tables_json", "TEXT"),
    ("findings_json", "TEXT"),
    ("baselines_json", "TEXT"),
    ("node_path", "TEXT"),
    ("slots_json", "TEXT"),
    # FR-H-3：原话与改写句分别留存
    ("original_question", "TEXT"),
    ("rewritten_question", "TEXT"),
]


def _ensure_turn_columns(engine: Engine) -> None:
    """兼容老库：发现缺失列就 ALTER TABLE 加上。SQLite 不支持 IF NOT EXISTS ADD，
    所以要先 PRAGMA 查 schema 再决定加哪些列。"""
    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(turns)")).fetchall()
        }
        for col, decl in _TURN_MIGRATIONS:
            if col in existing:
                continue
            conn.execute(text(f'ALTER TABLE turns ADD COLUMN "{col}" {decl}'))
        conn.commit()


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
