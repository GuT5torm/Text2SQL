"""按需 SQL 解释接口（FR-C-2）。

POST /api/sql/explain 接收一条 SQL，按关键字段分段解释其含义，不落库、
不包含结果集。解释是纯结构化的，不走 LLM：对四表 JOIN 得到逐段说明，
表与字段都在语义层登记范围内。

超时 15 秒（由前端控制）；接口本身只做解析与映射，不访问数据库。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
import sqlglot
from sqlglot import exp

from app.api.deps import Container, get_container

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sql", tags=["sql"])


class ExplainRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=5000, description="要解释的 SQL 语句")
    question: str = Field(default="", max_length=500, description="用户的原始问题，可选")
    session_id: str = Field(default="", description="会话ID，可选，用于日志关联")


def _table_display(name: str, catalog: Any) -> str:
    """从语义层取表的显示名与说明。"""
    tbl = catalog.tables.get(name)
    if tbl is None:
        return name
    return getattr(tbl, "display_name", None) or getattr(tbl, "name", None) or name


def _column_display(table: str, column: str, catalog: Any) -> str:
    """从语义层取字段的显示名。"""
    tbl = catalog.tables.get(table)
    if tbl is None:
        return column
    fields = getattr(tbl, "fields", None) or []
    for f in fields:
        fname = getattr(f, "name", "") or ""
        if fname.lower() == column.lower():
            disp = getattr(f, "display_name", None) or ""
            desc = getattr(f, "description", None) or ""
            return f"{disp or column}" + (f"（{desc}）" if desc else "")
    return column


def _explain_select(stmt: exp.Select, catalog: Any, dialect: str = "sqlite") -> list[str]:
    """对一条 SELECT 语句逐段解释。"""
    parts: list[str] = []

    # —— FROM / JOIN：涉及的表 ——
    tables: list[str] = []
    for table in stmt.find_all(exp.Table):
        name = (table.name or "").lower()
        if name and name not in tables:
            tables.append(name)

    if tables:
        table_labels = [_table_display(t, catalog) for t in tables]
        parts.append(f"从「{'、'.join(table_labels)}」表中取数。")

    # —— SELECT：查询哪些字段 ——
    select_cols: list[str] = []
    for proj in stmt.expressions:
        alias = proj.alias or ""
        if alias:
            select_cols.append(alias)
        elif isinstance(proj, exp.Column):
            select_cols.append(_column_display(proj.table or "", proj.name, catalog))
        else:
            # 聚合表达式
            rendered = proj.sql(dialect=dialect)
            select_cols.append(rendered)

    if select_cols:
        parts.append(f"返回字段：{'、'.join(select_cols)}。")

    # —— WHERE：过滤条件 ——
    where = stmt.args.get("where")
    if where is not None:
        parts.append(f"过滤条件：{where.sql(dialect=dialect)}。")

    # —— GROUP BY：分组 ——
    group = stmt.args.get("group")
    if group is not None:
        group_cols = [g.sql(dialect=dialect) for g in group.expressions]
        parts.append(f"按「{'、'.join(group_cols)}」分组聚合。")

    # —— HAVING：聚合后过滤 ——
    having = stmt.args.get("having")
    if having is not None:
        parts.append(f"聚合后过滤：{having.sql(dialect=dialect)}。")

    # —— ORDER BY：排序 ——
    order = stmt.args.get("order")
    if order is not None:
        order_cols = [o.sql(dialect=dialect) for o in order.expressions]
        parts.append(f"按「{'、'.join(order_cols)}」排序。")

    # —— LIMIT：行数限制 ——
    limit = stmt.args.get("limit")
    if limit is not None:
        for key in ("expression", "this"):
            val = limit.args.get(key)
            if isinstance(val, exp.Literal):
                parts.append(f"最多返回 {val.this} 行。")
                break

    return parts


@router.post("/explain")
def explain_sql(req: ExplainRequest, container: Container = Depends(get_container)) -> dict[str, Any]:
    """按需解释一条 SQL 的含义（FR-C-2）。

    纯结构化解析：按 SQL 关键字切分 + 字段映射到语义层 + 分段说明。
    不走 LLM、不落库、不包含结果集。
    """
    from app.db.guard import strip_sql_fence

    dialect = getattr(container.settings, "sql_dialect", "sqlite")
    sql = strip_sql_fence(req.sql)
    if not sql:
        return {"segments": [], "error": "SQL 为空"}

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as exc:
        return {"segments": [], "error": f"SQL 解析失败：{exc}"}

    if len(statements) != 1:
        return {"segments": [], "error": f"期望单条语句，实际解析出 {len(statements)} 条"}

    stmt = statements[0]
    catalog = container.catalog
    segments: list[dict[str, str]] = []

    # 对 SELECT 语句逐段解释
    if isinstance(stmt, exp.Select):
        parts = _explain_select(stmt, catalog, dialect)
        for i, text in enumerate(parts, 1):
            segments.append({"index": i, "text": text})
    elif isinstance(stmt, (exp.Union, exp.Except, exp.Intersect)):
        segments.append({"index": 1, "text": "这是一条集合操作语句（UNION/EXCEPT/INTERSECT）。"})
    else:
        segments.append({"index": 1, "text": f"语句类型：{type(stmt).__name__}，非标准查询。"})

    return {
        "segments": segments,
        "tables": [(t.name or "") for t in (stmt.find_all(exp.Table) if stmt else [])],
    }
