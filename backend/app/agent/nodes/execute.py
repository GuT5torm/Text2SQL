"""execute 节点：在只读连接上执行查询。

节点只调用 Repository 暴露的方法，不持有 engine，也不 import sqlite3。
这条边界是分层的硬约束之一：节点层与存储层之间只允许通过接口交互。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import sqlglot
from sqlglot import exp

from app.agent.state import AgentState, new_stage
from app.db.repository import QueryError, QueryRepository, QueryTimeout

logger = logging.getLogger(__name__)

NODE = "execute"


def execute_node(state: AgentState, *, repo: QueryRepository) -> dict[str, Any]:
    started = time.monotonic()
    sql = state.get("sql") or ""

    # FR-D-1：把实际执行的 SQL 中出现的表名解析出来。失败路径也解析，
    # 这样排查时能看到最后一次尝试访问了哪些表（语义层召回候选 ≠ 实际访问）。
    source_tables = _extract_source_tables(sql)

    try:
        result = repo.execute_readonly(sql)
    except QueryTimeout as exc:
        logger.info("查询超时：%s", exc)
        return _fail(state, f"查询超时：{exc}", started, source_tables)
    except QueryError as exc:
        # 数据库原始报错原样带进状态，fix 节点的修正质量主要取决于这句话的信息量
        logger.info("查询执行失败：%s", exc)
        return _fail(state, str(exc), started, source_tables)
    except Exception as exc:  # noqa: BLE001 - 兜住驱动层的意外异常，避免整轮中断
        logger.exception("查询执行出现意外异常")
        return _fail(state, f"查询执行出现意外异常：{exc}", started, source_tables)

    detail = f"返回 {result.row_count} 行，耗时 {result.elapsed_ms:g} ms"
    if result.truncated:
        detail += "（已截断）"
    if result.row_count == 0:
        detail += "（空结果，将尝试放宽条件）"

    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "query_elapsed_ms": result.elapsed_ms,
        # FR-F-1：空结果是「语句能跑但条件把数据筛没了」，与报错是两类失败。
        # 置位后由路由决定是否走 fix 的回灌分支；不置位则按原链路直接出结论。
        "empty_result": result.row_count == 0,
        # FR-D-1 来源表，落库与回看都基于这个字段
        "source_tables": source_tables,
        # FR-H-2 之 persist_row_count：实际查询到的行数；
        # 与 SQL_MAX_ROWS 不同，这是执行层的上限而不是落库层 PERSIST_ROWS 的上限。
        "persist_row_count": result.row_count,
        "sql_error": None,
        "stages": [new_stage(NODE, "执行查询", detail, _ms(started))],
    }


def _fail(
    state: AgentState,
    error: str,
    started: float,
    source_tables: list[str],
) -> dict[str, Any]:
    history = list(state.get("fix_history") or [])
    history.append({"stage": "execute", "sql": (state.get("sql") or "")[:2000], "error": error})
    return {
        "sql_error": error,
        "fix_history": history,
        # 失败路径也带上曾经尝试访问的表名，便于排查
        "source_tables": source_tables,
        "stages": [new_stage(NODE, "执行查询", f"执行失败：{error[:80]}", _ms(started))],
    }


def _extract_source_tables(sql: str) -> list[str]:
    """用 sqlglot 提取 SQL 中出现的表名（FROM / JOIN），去重并保序。

    不与 retrieved.tables 合并——前者是「语句实际访问」，后者是「语义层召回候选」。

    排除两类伪表：
      - CTE 自身别名：`WITH c AS (...) SELECT * FROM c`，最后的 `c` 不算，
        真正访问的表是 CTE 内部 FROM 的对象
      - 子查询别名：`FROM (SELECT ... FROM orders) t`，`t` 在 sqlglot AST
        里表现为 Subquery.alias 而非 Table，已经天然不出现；这里仍留一道
        防护，挡住未来 sqlglot 表达方式变更带来的回归
    """
    if not sql:
        return []
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except sqlglot.errors.ParseError:
        return []
    # CTE 别名集合：sqlglot 会把 `WITH c AS ...` 中的 c 表现为 FROM 下的
    # 一个 Table 节点，需要先把这些名字扣掉
    cte_aliases = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    seen: set[str] = set()
    out: list[str] = []
    for tbl in tree.find_all(exp.Table):
        name = tbl.name
        if not name or name in seen or name in cte_aliases:
            continue
        seen.add(name)
        out.append(name)
    return out


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
