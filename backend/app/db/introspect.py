"""数据源内省与语义层草稿生成。

让普通用户通过界面连接一个新数据库：先只读内省库的真实结构（表 / 字段 /
类型 / 主键 / 外键），再把结构变成一份「语义层草稿」写进 YAML。表结构是
从库里读出来的事实，绝不包含猜测；指标与维度是启发式推测，统一标为
「草稿·需人工确认口径」，由用户在界面预览确认后应用。

内省走 SQLAlchemy 的 inspect，对 sqlite / postgres / mysql 三种方言通用，
不需要为每种库单独写代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

# 方言归一化：界面/配置的写法统一成 sqlglot 与本项目内部用的短名。
_DIALECT_ALIASES = {
    "sqlite": "sqlite",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
}


@dataclass
class DatasourceSpec:
    """一个待连接的数据源描述。sqlite 用 path，其余用 dsn（连接串）。"""

    dialect: str  # sqlite | postgres | mysql（已归一化）
    path: str | None = None
    dsn: str | None = None


@dataclass
class IntrospectionResult:
    dialect: str
    tables: list[dict[str, Any]]


def normalize_dialect(raw: str) -> str:
    """把界面/配置里的方言写法归一化；不认识的直接抛错，避免静默落到 sqlite。"""
    key = (raw or "").strip().lower()
    if key not in _DIALECT_ALIASES:
        raise ValueError(f"不支持的方言：{raw}（可选 sqlite / postgres / mysql）")
    return _DIALECT_ALIASES[key]


def _readonly_sqlite_connection(path: Path) -> Any:
    import sqlite3

    conn = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
        check_same_thread=False,
    )
    conn.execute("PRAGMA query_only = ON")
    return conn


def _engine_for_spec(spec: DatasourceSpec) -> Engine:
    """按数据源描述建一个只读引擎用于内省。sqlite 走 mode=ro URI。"""
    if spec.dialect == "sqlite":
        if not spec.path:
            raise ValueError("SQLite 数据源需要填写库文件路径")
        p = Path(spec.path)
        if not p.exists():
            raise FileNotFoundError(f"库文件不存在：{p}")
        return create_engine(
            "sqlite://",
            creator=lambda: _readonly_sqlite_connection(p),
            future=True,
        )
    if not spec.dsn:
        raise ValueError("该方言需要填写连接串（DSN）")
    # postgres / mysql 的只读靠 SQL 守卫约束（只放行 SELECT），connect 层不做写。
    return create_engine(spec.dsn, future=True, pool_pre_ping=True)


def _normalize_type(type_obj: Any) -> str:
    """把 SQLAlchemy 类型对象压成语义层用的少量基类，便于启发式与守卫统一处理。"""
    name = re.split(r"[\(\[]", str(type_obj))[0].upper().strip()
    mapping = {
        "INTEGER": "INTEGER",
        "INT": "INTEGER",
        "BIGINT": "INTEGER",
        "SMALLINT": "INTEGER",
        "TINYINT": "INTEGER",
        "REAL": "REAL",
        "FLOAT": "REAL",
        "DOUBLE": "REAL",
        "NUMERIC": "REAL",
        "DECIMAL": "REAL",
        "DATETIME": "DATETIME",
        "TIMESTAMP": "DATETIME",
        "DATE": "DATE",
        "TIME": "DATETIME",
        "BOOLEAN": "BOOLEAN",
        "BOOL": "BOOLEAN",
        "JSON": "TEXT",
        "JSONB": "TEXT",
        "BLOB": "TEXT",
    }
    if name in mapping:
        return mapping[name]
    if name.startswith("VARCHAR") or name in ("TEXT", "CHAR", "CLOB", "STRING"):
        return "TEXT"
    return "TEXT"


def introspect(spec: DatasourceSpec) -> IntrospectionResult:
    """只读内省库结构。任何失败（文件缺失 / 驱动缺失 / 鉴权失败）都原样上抛。"""
    engine = _engine_for_spec(spec)
    try:
        insp = inspect(engine)
        raw_tables = insp.get_table_names()
        tables: list[dict[str, Any]] = []
        for t in raw_tables:
            cols = insp.get_columns(t)
            pk = (insp.get_pk_constraint(t) or {}).get("constrained_columns") or []
            fks = insp.get_foreign_keys(t) or []
            fields: list[dict[str, Any]] = []
            for c in cols:
                fields.append(
                    {
                        "name": c["name"],
                        "type": _normalize_type(c["type"]),
                        "pk": c["name"] in pk,
                        "nullable": bool(c.get("nullable", True)),
                    }
                )
            foreign_keys: list[dict[str, str]] = []
            for fk in fks:
                ref_table = fk.get("referred_table")
                for lcol, rcol in zip(
                    fk.get("constrained_columns", []),
                    fk.get("referred_columns", []),
                ):
                    foreign_keys.append(
                        {"column": lcol, "ref_table": ref_table, "ref_column": rcol}
                    )
            tables.append(
                {
                    "name": t,
                    "display": t,
                    "fields": fields,
                    "pk": list(pk),
                    "foreign_keys": foreign_keys,
                }
            )
        return IntrospectionResult(dialect=spec.dialect, tables=tables)
    finally:
        engine.dispose()


_IDENT_RE = re.compile(r"[^0-9a-zA-Z_]")


def _safe_ident(s: str) -> str:
    """把任意表/列名拼成语义层可用的标识符：非字母数字下划线替换成 _，首字符补前缀。"""
    cleaned = _IDENT_RE.sub("_", s)
    if cleaned and cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return cleaned or "col"


def _month_expr(dialect: str, column: str) -> str:
    """按方言给出「按月」分组的表达式，供启发式维度使用。"""
    if dialect == "sqlite":
        return f"strftime('%Y-%m', {column})"
    if dialect == "mysql":
        return f"DATE_FORMAT({column}, '%Y-%m')"
    # postgres
    return f"to_char({column}, 'YYYY-MM')"


def build_semantic_draft(
    spec: DatasourceSpec, intro: IntrospectionResult
) -> dict[str, dict[str, Any]]:
    """由内省结果生成语义层草稿。

    表 / 字段 / 类型 / 主键 / 外键是事实（直接来自库），原样进入 tables.yaml。
    指标与维度是启发式：数值列 → 求和指标，时间列 → 按月维度，外加每张表的
    记录数指标。全部标「草稿·启发式推测口径」，由用户在界面确认后再用于生产。
    """
    table_names = {t["name"] for t in intro.tables}
    tables_yaml: list[dict[str, Any]] = []
    dimensions_yaml: list[dict[str, Any]] = []
    metrics_yaml: list[dict[str, Any]] = []

    for t in intro.tables:
        tname = t["name"]
        fields: list[dict[str, Any]] = []
        for f in t["fields"]:
            fd: dict[str, Any] = {"name": f["name"], "type": f["type"]}
            if f["pk"]:
                fd["pk"] = True
            for fk in t["foreign_keys"]:
                if fk["column"] == f["name"] and fk["ref_table"] in table_names:
                    fd["ref"] = f'{fk["ref_table"]}.{fk["ref_column"]}'
            fields.append(fd)

        joins = [
            f"JOIN {fk['ref_table']} ON {fk['ref_table']}.{fk['ref_column']} = {tname}.{fk['column']}"
            for fk in t["foreign_keys"]
            if fk["ref_table"] in table_names
        ]
        td: dict[str, Any] = {"name": tname, "display": tname}
        if joins:
            td["join"] = " ".join(joins)
        td["fields"] = fields
        tables_yaml.append(td)

        for f in t["fields"]:
            col = f"{tname}.{f['name']}"
            if f["type"] in ("DATETIME", "DATE", "TIMESTAMP"):
                dim_name = _safe_ident(f"{tname}_{f['name']}_month")
                dimensions_yaml.append(
                    {
                        "name": dim_name,
                        "display": f"{tname}.{f['name']} 按月",
                        "expression": _month_expr(spec.dialect, col),
                        "tables": [tname],
                    }
                )
            if f["type"] in ("INTEGER", "REAL"):
                mname = _safe_ident(f"{tname}_{f['name']}_sum")
                metrics_yaml.append(
                    {
                        "name": mname,
                        "display": f"{tname}.{f['name']} 求和",
                        "expression": f"SUM({col})",
                        "tables": [tname],
                        "caveats": "草稿·启发式推测口径，请确认",
                    }
                )
        # 每张表至少一个指标，保证 load_catalog 不因 metrics 为空而报错
        cm = _safe_ident(f"{tname}_row_count")
        metrics_yaml.append(
            {
                "name": cm,
                "display": f"{tname} 记录数",
                "expression": "COUNT(*)",
                "tables": [tname],
                "caveats": "草稿·启发式推测口径，请确认",
            }
        )

    return {
        # tables.yaml 同时承载表与维度
        "tables.yaml": {"tables": tables_yaml, "dimensions": dimensions_yaml},
        "metrics.yaml": {"metrics": metrics_yaml},
        "synonyms.yaml": {"synonyms": [], "value_synonyms": [], "time_terms": []},
        "baseline_rules.yaml": {
            "default": {
                "granularity": "auto",
                "min_granularity": "month",
                "mom": False,
                "yoy": False,
            }
        },
        # finding_rules.yaml 是 load_catalog 的可选读取项，但一旦存在就会按
        # 指标名校验；连新库时必须一并替换为最小默认，避免旧库的异常规则引用
        # 到新库不存在的指标而让整个语义层加载失败。
        "finding_rules.yaml": {"default": {}},
    }
