"""应用库 ORM 模型。

只存会话与问答记录这两类元数据，业务数据不在这个库里。
本文件由接入层与历史会话接口使用；编排层不直接依赖它，只通过 Repository 交互。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now()


class Session(Base):
    """一次会话。session_id 同时作为 LangGraph Checkpointer 的 thread_id。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="新会话")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    # 软删除，便于「恢复历史会话」
    archived: Mapped[int] = mapped_column(Integer, default=0)

    turns: Mapped[list["Turn"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Turn.id"
    )


class Turn(Base):
    """一轮问答。SQL、口径、图表配置都留档，历史会话回看时无需重跑。"""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本轮采用的指标口径与口径边界，前端标注用；JSON 字符串
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    chart_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ok / clarify / chitchat / error
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    # —— FR-H-2 历史回看字段补全 ——
    # 阶段时间线与每节点耗时，JSON 字符串，供历史回看（FR-E-1）
    stages_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 守卫改写前的原始 SQL 语句，用于回溯首次生成版本（FR-I-2 后续会用到）
    original_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本轮守卫告警（拒绝 / 改写），区分两类渲染（FR-I-2）
    guard_warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # fix 节点递增，0 表示一次生成就跑通。回看时是「这条轮修正了 N 次」
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    # 结果是否被 SQL_MAX_ROWS 截断（FR-H-2 之 truncated 重定义）
    truncated: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    # 实际落库的行数；与 row_count 区分：执行未截断但落库截断时不同
    persist_row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    # 来源表，实际执行 SQL 的 FROM/JOIN 解析（FR-D-1）。与 retrieved.tables 是两件事
    source_tables_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 异常标记列表，JSON（FR-L-1，第 6 批事）— 本批允许 NULL 不填
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 对比基准值，JSON（FR-M-1，第 4 批事）— 本批允许 NULL 不填
    baselines_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本轮走过的节点序列去重（FR-I-4 后续会用到），TEXT 已可存
    node_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FR-A-5：结构化槽位 JSON（metrics/dimensions/filters/time_range/order_by/top_n）
    slots_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FR-H-3：用户原话与路由改写后的问句分别留存。此前 original_question 只写在
    # 图状态里、全项目无人读取（死字段），改写后的问句又直接覆盖了 question，
    # 回看时无从判断「系统实际回答的是不是用户问的那句」。
    original_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    rewritten_question: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped[Session] = relationship(back_populates="turns")


class Feedback(Base):
    """一轮问答的点赞点踩（FR-G-1 至 FR-G-4）。

    一条 turn 至多一条 feedback：UNIQUE(turn_id) 让重复提交走 upsert，
    而不是再插一条——重复提交在产品里很常见，可能只是用户改了原因。
    rating 取 up/down；down 时 reason_code 与 note 才有意义。
    """

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[int] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), unique=True, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    # up / down
    rating: Mapped[str] = mapped_column(String(8))
    # 口径错 / 字段错 / 数字错 / 答非所问 / 其他；点赞时为空字符串
    reason_code: Mapped[str] = mapped_column(String(32), default="")
    # 用户填写的「正确问法或口径」自由文本（FR-G-2），上限 500 字
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Share(Base):
    """只读分享链接（FR-N-3）。

    「链接即凭据」——token 是 PK 也是访问凭据，不做用户校验。
    snapshot_json 存这一轮的完整结果快照，访问时直接读出，不走 chat 链路。
    expires_at 可为空表示「永久有效」；revoked 是软撤销，前端不再可读。
    """

    __tablename__ = "shares"

    token: Mapped[str] = mapped_column(String(36), primary_key=True)
    turn_id: Mapped[int | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # ChatResult 的 JSON 快照，含问句、结论、SQL、口径、图表配置、明细表
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AlertRule(Base):
    """异常订阅规则（FR-O-1）。

    dimensions_json 为空列表表示不限维度（整体口径）；comparison 取
    gt / lt / deviation；severity 取 notice / attention（与 finding_rules
    的「提示 / 关注」一一映射）。
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric: Mapped[str] = mapped_column(String(64))
    dimensions_json: Mapped[str] = mapped_column(Text, default="[]")
    comparison: Mapped[str] = mapped_column(String(16))
    threshold: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16), default="notice")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Alert(Base):
    """一次规则求值的落库事件（FR-O-2 / FR-O-3）。

    成功未命中写一行 hit=0；命中按切片各写一行 hit=1；求值失败写一行
    status=error 且 hit=0——失败绝不能记成未命中。
    (rule_id, evaluated_at) 表级唯一约束保证调度器重复触发时幂等。
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("rule_id", "evaluated_at", name="uq_alert_rule_evaluated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True
    )
    evaluated_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    # ok / error
    status: Mapped[str] = mapped_column(String(16), default="ok")
    hit: Mapped[int] = mapped_column(Integer, default=0)
    metric: Mapped[str] = mapped_column(String(64), default="")
    slice_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    seen: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
