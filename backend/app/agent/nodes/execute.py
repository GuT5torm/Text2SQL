"""execute 节点：在只读连接上执行查询。

节点只调用 Repository 暴露的方法，不持有 engine，也不 import sqlite3。
这条边界是分层的硬约束之一：节点层与存储层之间只允许通过接口交互。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.state import AgentState, new_stage
from app.db.repository import QueryError, QueryRepository, QueryTimeout

logger = logging.getLogger(__name__)

NODE = "execute"


def execute_node(state: AgentState, *, repo: QueryRepository) -> dict[str, Any]:
    started = time.monotonic()
    sql = state.get("sql") or ""

    try:
        result = repo.execute_readonly(sql)
    except QueryTimeout as exc:
        logger.info("查询超时：%s", exc)
        return _fail(state, f"查询超时：{exc}", started)
    except QueryError as exc:
        # 数据库原始报错原样带进状态，fix 节点的修正质量主要取决于这句话的信息量
        logger.info("查询执行失败：%s", exc)
        return _fail(state, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - 兜住驱动层的意外异常，避免整轮中断
        logger.exception("查询执行出现意外异常")
        return _fail(state, f"查询执行出现意外异常：{exc}", started)

    detail = f"返回 {result.row_count} 行，耗时 {result.elapsed_ms:g} ms"
    if result.truncated:
        detail += "（已截断）"

    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "query_elapsed_ms": result.elapsed_ms,
        "sql_error": None,
        "stages": [new_stage(NODE, "执行查询", detail, _ms(started))],
    }


def _fail(state: AgentState, error: str, started: float) -> dict[str, Any]:
    history = list(state.get("fix_history") or [])
    history.append({"stage": "execute", "sql": (state.get("sql") or "")[:2000], "error": error})
    return {
        "sql_error": error,
        "fix_history": history,
        "stages": [new_stage(NODE, "执行查询", f"执行失败：{error[:80]}", _ms(started))],
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
