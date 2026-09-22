"""非 SQLite 业务库的只读查询实现（PostgreSQL / MySQL）。

与 SqliteQueryRepository 共用同一套「只放行 SELECT」的 SQL 守卫，区别仅在
连接层：这里用连接串建引擎。表名/字段名白名单、行数上限、结果体积上限都在
守卫与上层节点处统一约束，本实现只负责把 SQL 跑通并把结果序列化成字典列表。

值序列化参考 SqliteQueryRepository：日期 / 小数 / 二进制 / UUID 等 JSON 不
友好的类型统一转成字符串，避免前端收到无法序列化的对象。
"""

from __future__ import annotations

import datetime as dt
import decimal
import time
import uuid
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db.repository import QueryError, QueryRepository, QueryResult


def _dedupe_columns(columns: list[str]) -> list[str]:
    used: set[str] = set()
    out: list[str] = []
    for raw in columns:
        base = raw or "column"
        name = base
        idx = 1
        while name in used:
            idx += 1
            name = f"{base}_{idx}"
        used.add(name)
        out.append(name)
    return out


def _estimate_bytes(columns: list[str], rows: list[dict[str, Any]]) -> int:
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
    return total + sum(len(c) * 3 for c in columns)


def _jsonify(value: Any) -> Any:
    """把 JSON 不友好的类型转成字符串；字典 / 列表保持（驱动可能返回数组）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


class GenericSqlQueryRepository(QueryRepository):
    """PostgreSQL / MySQL 等任意方言的只读查询仓库。"""

    def __init__(self, dsn: str, dialect: str):
        self.dialect = dialect
        self.engine: Engine = create_engine(dsn, future=True, pool_pre_ping=True)

    def execute_readonly(self, sql: str, max_rows: int | None = None) -> QueryResult:
        settings = get_settings()
        limit = max_rows or settings.sql_max_rows
        started = time.monotonic()
        with self.engine.connect() as conn:
            try:
                result = conn.execute(text(sql))
                columns = _dedupe_columns(list(result.keys()))
                fetched = result.fetchmany(limit + 1)
            except Exception as exc:  # 数据库原始报错原样保留给 fix 节点
                raise QueryError(f"执行失败：{exc}") from exc

            truncated = len(fetched) > limit
            rows = [
                {c: _jsonify(row._mapping[c]) for c in columns}
                for row in fetched[:limit]
            ]

        size = _estimate_bytes(columns, rows)
        if size > settings.sql_max_result_bytes:
            raise QueryError(
                f"结果集体积约 {size / 1024 / 1024:.1f} MB，超过上限 "
                f"{settings.sql_max_result_bytes / 1024 / 1024:.0f} MB，已拒绝返回。"
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

    def list_tables(self) -> list[str]:
        insp = inspect(self.engine)
        return insp.get_table_names()

    def count_rows(self, table: str) -> int:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"非法的表名：{table}")
        with self.engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
