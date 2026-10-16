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

from app.agent.checkpointer import build_checkpointer_with_mode
from app.agent.graph import build_graph
from app.audit.recorder import AuditRecorder, configure_recorder, get_recorder, reset_recorder
from app.audit.sinks import DatabaseSink, LoggingSink, NullSink
from app.config import Settings, get_settings
from app.db.audit_repo import SqliteAuditRepository
from app.db.engine import ensure_business_db
from app.db.guard import SqlGuard
from app.db.repository import AuditRepository, QueryRepository, SessionStore
from app.db.sqlite_repo import SqliteQueryRepository, SqliteSessionStore
from app.db.generic_repo import GenericSqlQueryRepository
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
    # FR-J-3：checkpointer 实际生效模式（配置 sqlite 失败时会退回 memory）
    checkpointer_mode: str = "memory"
    # 审计仓储。None 表示审计未启用或应用库不可用（此时落点降级为日志，
    # 查询接口无从返回数据）——两者在接口层统一处理，不在这里混淆
    audit: AuditRepository | None = None
    audit_recorder: AuditRecorder | None = None


def _configure_audit(s: Settings) -> AuditRepository | None:
    """装配审计落点，返回可查询的仓储（降级时返回 None）。

    分三档，每档都有明确出口，不允许出现「函数写了但没人调用」的摆设：
      关闭      → NullSink，审计静默；
      正常      → DatabaseSink，写应用库的 audit_log 表；
      库不可用  → LoggingSink，写日志。查询接口拿不到数据，如实返回不可用。
    """
    if not s.audit_enabled:
        configure_recorder(NullSink())
        return None
    try:
        repo = SqliteAuditRepository()
    except Exception:  # noqa: BLE001 - 审计建连失败不能拖垮启动
        logger.warning("审计库不可用，降级为日志输出，主链路不受影响", exc_info=True)
        configure_recorder(LoggingSink())
        return None
    configure_recorder(DatabaseSink(repo))
    return repo


def build_container(settings: Settings | None = None) -> Container:
    s = settings or get_settings()
    catalog = load_catalog()
    gateway = Gateway(s)
    guard = SqlGuard.from_catalog(catalog, max_rows=s.sql_max_rows, dialect=s.sql_dialect)

    # 业务库仓库按方言切换：SQLite 走只读 mode=ro 连接（安全体系第一层），
    # 其余方言走通用实现。这是 ADR-002 留出的唯一换库接缝。
    if s.sql_dialect == "sqlite":
        ensure_business_db(s.business_db_path)
        repo: QueryRepository = SqliteQueryRepository()
    else:
        if not s.business_db_dsn:
            raise RuntimeError(
                f"方言为 {s.sql_dialect} 时需要配置 BUSINESS_DB_DSN（连接串）"
            )
        repo = GenericSqlQueryRepository(s.business_db_dsn, s.sql_dialect)
    store = SqliteSessionStore()
    # 审计必须在图装配之前 configure：节点拿的是此刻的全局记录器实例，
    # 顺序反了整张图就会一直用未装配时的 NullSink
    audit_repo = _configure_audit(s)
    checkpointer, checkpointer_mode = build_checkpointer_with_mode(s)

    # 时间词锚点：启动时取一次库内数据截止日。业务库只读，进程生命周期内
    # 不会变；相对时间词（上个月、近 7 天）按它换算而不是按系统日期，
    # 否则数据陈旧后会落到没有数据的月份。/api/health 报的是同一个值。
    data_as_of = repo.data_as_of()

    graph = build_graph(
        gateway=gateway,
        catalog=catalog,
        guard=guard,
        repo=repo,
        checkpointer=checkpointer,
        history_window=s.history_window,
        max_retry=s.sql_max_retry,
        # FR-F-1：空结果回灌配额，与报错修正是两套计数
        max_empty=s.sql_max_empty_retry,
        audit=get_recorder(),
        data_as_of=data_as_of,
    )
    logger.info(
        "容器装配完成：指标 %d 个、表 %d 张、LLM %s、时间锚点 %s",
        len(catalog.metrics),
        len(catalog.tables),
        "桩实现" if gateway.mock else s.llm_model,
        data_as_of.isoformat() if data_as_of else "系统日期",
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
        checkpointer_mode=checkpointer_mode,
        audit=audit_repo,
        audit_recorder=get_recorder(),
    )


@lru_cache(maxsize=1)
def get_container() -> Container:
    return build_container()


def reset_container() -> None:
    """测试或配置变更后重建容器。

    同时复位审计记录器：容器是进程内单例、记录器也是，只清前者会让旧落点
    继续挂在全局上，用例之间互相串数据。
    """
    get_container.cache_clear()
    reset_recorder()
