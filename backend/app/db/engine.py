"""数据库引擎。

两个库，职责分开：

  ecommerce.db  业务库。只读打开，Agent 生成的所有查询都落在这里。
  app.db        应用库。存会话元数据与（切到 SQLiteSaver 时的）会话快照，可写。

业务库以只读方式连接是整个安全体系的第一层：即使 SQL 守卫被绕过，
连接层也没有写权限。WAL 模式在造数时（data/seed.py）一次性写入库文件属性，
只读连接无需也无法重新设置。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from app.config import get_settings


def _business_uri(path: Path) -> str:
    # 只读 + URI 模式。用 posix 风格路径，Windows 上也能被 SQLite 正确识别
    return f"file:{path.as_posix()}?mode=ro"


def _readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _business_uri(path),
        uri=True,
        timeout=5.0,
        check_same_thread=False,
    )
    # 双保险：连接层与语句层都标为只读
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_business_engine(path: Path | None = None) -> Engine:
    """业务库引擎：只读，供 Repository 执行查询。"""
    db_path = path or get_settings().business_db_path
    if not db_path.exists():
        raise FileNotFoundError(
            f"业务库不存在：{db_path}\n请先运行 python data/seed.py 生成样例数据。"
        )
    engine = create_engine(
        "sqlite://",
        creator=lambda: _readonly_connection(db_path),
        poolclass=None,  # 走默认 QueuePool，creator 负责建连
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi_conn, _record):  # pragma: no cover - 驱动层回调
        # 只读连接不需要事务，关掉隐式 BEGIN 减少锁交互
        dbapi_conn.isolation_level = None

    return engine


def create_app_engine(path: Path | None = None) -> Engine:
    """应用库引擎：可写，存会话元数据。首次调用时自动建表。"""
    db_path = path or get_settings().app_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _record):  # pragma: no cover - 驱动层回调
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.close()

    return engine


def ensure_business_db(path: Path | None = None) -> None:
    """启动时检查业务库是否就绪，缺失时给出可直接执行的修复命令。"""
    db_path = path or get_settings().business_db_path
    if not db_path.exists():
        raise FileNotFoundError(
            f"业务库不存在：{db_path}\n请在 backend 目录下执行：python data/seed.py"
        )
