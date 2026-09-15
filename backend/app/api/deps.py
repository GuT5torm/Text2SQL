"""依赖装配。

整个应用的组装发生在这里，只有这一处知道「哪个实现装配到哪个接口上」。
好处有两个：节点与路由的单元测试只需构造自己需要的依赖，不必启动整个应用；
换实现（例如把 SQLite 换成 PostgreSQL）只改本文件的一行。

容器在进程内只构造一次：语义层加载要读三个 YAML，图的编译也有成本，
不应每个请求重建。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.agent.checkpointer import build_checkpointer
from app.agent.graph import build_graph
from app.config import Settings, get_settings
from app.db.engine import ensure_business_db
from app.db.guard import SqlGuard
from app.db.repository import QueryRepository, SessionStore
from app.db.sqlite_repo import SqliteQueryRepository, SqliteSessionStore
from app.llm.gateway import Gateway
from app.semantic.loader import Catalog, load_catalog

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    catalog: Catalog
    gateway: Gateway
    guard: SqlGuard
    repo: QueryRepository
    store: SessionStore
    graph: Any
    checkpointer: Any


def build_container(settings: Settings | None = None) -> Container:
    s = settings or get_settings()
    ensure_business_db(s.business_db_path)

    catalog = load_catalog()
    gateway = Gateway(s)
    guard = SqlGuard.from_catalog(catalog, max_rows=s.sql_max_rows)
    repo = SqliteQueryRepository()
    store = SqliteSessionStore()
    checkpointer = build_checkpointer(s)

    graph = build_graph(
        gateway=gateway,
        catalog=catalog,
        guard=guard,
        repo=repo,
        checkpointer=checkpointer,
        history_window=s.history_window,
        max_retry=s.sql_max_retry,
    )
    logger.info(
        "容器装配完成：指标 %d 个、表 %d 张、LLM %s",
        len(catalog.metrics),
        len(catalog.tables),
        "桩实现" if gateway.mock else s.llm_model,
    )
    return Container(
        settings=s,
        catalog=catalog,
        gateway=gateway,
        guard=guard,
        repo=repo,
        store=store,
        graph=graph,
        checkpointer=checkpointer,
    )


@lru_cache(maxsize=1)
def get_container() -> Container:
    return build_container()


def reset_container() -> None:
    """测试或配置变更后重建容器。"""
    get_container.cache_clear()
