"""结果集脱敏管道（FR-I-3）。

唯一入口是 :func:`mask_result`：拿到 summarize_result 的结果后，在
「写 rows_json」与「下发 SSE / HTTP 响应」之前统一过一遍，保证落库、本轮
返回与分享快照三处同源，且重复调用幂等。

掩码规则固定：
- phone  前 3 后 4：138****1234
- idcard 前 6 后 4：310100********1234
- other  定长占位：******

敏感字段的唯一来源是语义层（tables.yaml 的 sensitive/mask），本模块不自带
任何「看起来像手机号」的猜测正则——猜错会把普通编码字段也洗掉。
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

_PHONE_STARS = "****"
_IDCARD_STARS = "********"
_OTHER_PLACEHOLDER = "******"


def mask_scalar(kind: str, value: Any) -> Any:
    """按掩码种类处理一个单元格。非字符串原样返回，已脱敏（含 *）原样返回。"""
    if not isinstance(value, str):
        return value
    if "*" in value:
        # 幂等：分享快照会对已落库的 rows_json 再过一遍管道，
        # 不能把 138****1234 洗成 138*******。
        return value
    if kind == "phone":
        if len(value) < 7:
            return _OTHER_PLACEHOLDER
        return f"{value[:3]}{_PHONE_STARS}{value[-4:]}"
    if kind == "idcard":
        if len(value) < 11:
            return _OTHER_PLACEHOLDER
        return f"{value[:6]}{_IDCARD_STARS}{value[-4:]}"
    return _OTHER_PLACEHOLDER


def _alias_to_real(stmt: exp.Expression) -> dict[str, str]:
    """表别名 → 真实表名（与 guard 同构，但这里只关心投影列的归属）。"""
    mapping: dict[str, str] = {}
    for table in stmt.find_all(exp.Table):
        real = (table.name or "").lower()
        if real:
            mapping.setdefault(real, real)
        if table.alias:
            mapping[table.alias.lower()] = real
    return mapping


def _projection_kinds(sql: str, catalog: Any) -> dict[str, str]:
    """解析投影，得到「结果列名（小写）→ 掩码种类」。

    必须解析投影而不是只看结果列名：模型可能把 phone 起成中文别名
    （AS 手机号），列名兜底按物理名就匹配不到；反过来别名也可能与别的表
    字段重名。这里以「投影表达式引用的真实敏感列」为准。
    守卫改写后的语句形如 SELECT * FROM (原句) LIMIT n，内层 Select 的
    投影就是结果列，因此遍历所有 Select，Star 投影跳过。
    """
    kinds: dict[str, str] = {}
    if not sql:
        return kinds
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except Exception:
        return kinds
    for stmt in statements:
        alias_to_real = _alias_to_real(stmt)
        for select in stmt.find_all(exp.Select):
            for proj in select.expressions or []:
                if isinstance(proj, exp.Star):
                    continue
                if isinstance(proj, exp.Column):
                    column, output = proj, proj.alias_or_name
                elif isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Column):
                    column, output = proj.this, proj.alias
                else:
                    continue
                if not output:
                    continue
                table = alias_to_real.get(column.table.lower()) if column.table else None
                kind = catalog.mask_kind(table, column.name)
                if kind:
                    kinds[output.lower()] = kind
    return kinds


def _column_kinds(sql: str, columns: list[str], catalog: Any) -> dict[str, str]:
    """最终的结果列掩码表：投影解析优先，物理名/中文显示名兜底。"""
    projected = _projection_kinds(sql, catalog)
    kinds: dict[str, str] = {}
    for col in columns or []:
        kind = projected.get(str(col).lower()) or catalog.mask_kind(None, str(col))
        if kind:
            kinds[col] = kind
    return kinds


def _masked_field_labels(kinds: dict[str, str], catalog: Any) -> list[str]:
    """把命中的掩码种类还原成 表.字段 标签，用于响应里的 masked_fields。"""
    labels: set[str] = set()
    by_kind: dict[str, str] = {}
    for entry in catalog.sensitive_fields.values():
        by_kind.setdefault(entry["mask"], f"{entry['table']}.{entry['field']}")
    for kind in set(kinds.values()):
        label = by_kind.get(kind)
        if label:
            labels.add(label)
    return sorted(labels)


def mask_result(result: Any, catalog: Any) -> Any:
    """对一轮结果集就地脱敏，并写入 masked / masked_fields 标记。

    result 需是含 columns / rows（可选 sql）的 dict；rows 元素可以是 dict
    或 list。没有敏感列时只补标记、不动数据。幂等。
    """
    if not isinstance(result, dict):
        return result
    columns = list(result.get("columns") or [])
    rows = result.get("rows")
    kinds = _column_kinds(result.get("sql") or "", columns, catalog)
    existing_fields = list(result.get("masked_fields") or [])
    if kinds and isinstance(rows, list):
        index_kinds = {columns.index(col): kind for col, kind in kinds.items()}
        masked_rows: list[Any] = []
        for row in rows:
            if isinstance(row, dict):
                new_row = dict(row)
                for col, kind in kinds.items():
                    if col in new_row:
                        new_row[col] = mask_scalar(kind, new_row[col])
                masked_rows.append(new_row)
            elif isinstance(row, (list, tuple)):
                masked_rows.append(
                    [
                        mask_scalar(index_kinds[i], value) if i in index_kinds else value
                        for i, value in enumerate(row)
                    ]
                )
            else:
                masked_rows.append(row)
        result["rows"] = masked_rows
    labels = sorted(set(existing_fields) | set(_masked_field_labels(kinds, catalog)))
    result["masked_fields"] = labels
    result["masked"] = bool(labels)
    return result
