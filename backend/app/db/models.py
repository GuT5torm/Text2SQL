"""应用库 ORM 模型。

只存会话与问答记录这两类元数据，业务数据不在这个库里。
本文件由接入层与历史会话接口使用；编排层不直接依赖它，只通过 Repository 交互。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
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

    session: Mapped[Session] = relationship(back_populates="turns")
