"""指标口径签名校验。

背景：语义层召回命中了指标、口径边界（caveats）也挂对了，生成环节仍可能把
聚合表达式写歪。典型形态是「口径退化」——问毛利只算成交额、问客单价只算
成交额、问订单量只算成交额。这类语句能执行、结果有数据、图表正常，全程没有
报错，是唯一会让用户拿着错误数字而不自知的失败形态。

本模块只做一件事：拿 SQL 与命中指标声明的 expression_signature 比对，返回
不符合的说明。纯函数，不执行 SQL、不调用模型，因此可以脱离编排层单独单测。

签名声明在 metrics.yaml 里，运行时守卫（agent/nodes/validate.py）与评测断言
（eval/run_eval.py）共用同一份，口径知识不重复维护。

与 app/db/guard.py 的分工：guard 管语句安不安全（表字段白名单、单条 SELECT、
LIMIT、超时），本模块管口径对不对。两者互不替代——一条语句完全可以既安全
又答非所问。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# 聚合节点集合，require_plain_aggregate 判定用。覆盖 SQL 里常见的五种聚合。
_AGGREGATES = (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max)

_WS_RE = re.compile(r"\s+")
# 函数名与左括号之间、左括号与首个参数之间都可能出现空格：`COUNT ( DISTINCT`
# 与 `COUNT(DISTINCT` 是同一个表达式。两侧都要吃，只吃前侧会留下
# `count( distinct`，仍然匹配不上签名里的 `count(distinct`。
_PAREN_SPACE_RE = re.compile(r"\s*\(\s*")


def normalize_sql(sql: str) -> str:
    """规范化 SQL 文本，供签名的子串匹配使用。

    三件事：转小写、压缩空白、吃掉左括号两侧的空格。最后一条是必需的：
    `COUNT ( DISTINCT o.id)` 与 `COUNT(DISTINCT o.id)` 是同一个表达式的两种
    写法，模型两种都会产出，签名里的 `count(distinct` 必须都能匹配上。
    """
    return _PAREN_SPACE_RE.sub("(", _WS_RE.sub(" ", sql.lower()))


def _has_plain_aggregate(tree: exp.Expression, exclude: list[str]) -> bool:
    """是否存在一个聚合调用，其参数不含 exclude 里的任何标识。

    判定取「存在一个」而非「全部」，这是刻意的。同一轮可能命中多个指标，
    例如问「各品类的销量和成交总额」，语句里既有 SUM(quantity) 也有
    SUM(quantity * unit_price)，只要前者在就不应判为口径不符。只有当一个
    干净的聚合都没有——即销量被整体写成了金额——才判不符。
    """
    tokens = [e.lower() for e in exclude]
    for node in tree.find_all(*_AGGREGATES):
        if not any(t in normalize_sql(node.sql(dialect="sqlite")) for t in tokens):
            return True
    return False


def check_expression_signatures(
    sql: str, signatures: list[dict[str, Any]]
) -> list[str]:
    """按签名校验 SQL 的聚合口径，返回不符合项的说明。

    signatures 的元素形如
        {"name": "gross_profit", "display": "毛利",
         "require_all": ["cost_price"],
         "require_plain_aggregate": {"exclude": ["unit_price"]}}
    通常由 retrieve 节点从命中的指标里摘出来放进状态。

    返回空列表表示全部符合。任何一项不符都会产出一条可直接回灌给 fix 节点的
    中文说明——fix 要靠它知道该改什么，措辞必须具体到字段名。
    """
    if not sql.strip() or not signatures:
        return []

    normalized = normalize_sql(sql)

    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception as exc:  # noqa: BLE001
        # 语句在进本模块前已经过 guard 的 AST 校验，正常路径上必然可解析。
        # 这里解析失败说明上游行为异常，不在此处报错：口径校验不该成为
        # 一个新的失败来源，放行交给后续环节。
        logger.warning("口径校验跳过：语句无法解析（%s）", exc)
        return []
    if tree is None:
        return []

    problems: list[str] = []
    for sig in signatures:
        label = sig.get("display") or sig.get("name") or "未命名指标"

        missing = [
            str(token)
            for token in (sig.get("require_all") or [])
            if str(token).lower() not in normalized
        ]
        if missing:
            problems.append(
                f"{label}的口径不成立：语句中未出现 {missing}，"
                f"该指标的聚合表达式缺少必要成分"
            )
            # 必要成分都缺，纯度判定不再有意义，跳过以免堆叠两条同源说明
            continue

        exclude = [
            str(token)
            for token in (
                (sig.get("require_plain_aggregate") or {}).get("exclude") or []
            )
        ]
        if exclude and not _has_plain_aggregate(tree, exclude):
            problems.append(
                f"{label}的口径不成立：所有聚合都含有 {exclude}，"
                f"该指标要求至少有一个聚合不含这些字段"
            )

    return problems


def signatures_for(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从语义层指标定义里摘出口径签名，供节点放入状态。

    未声明 expression_signature 的指标直接跳过——签名是可选的，
    没有签名表示该指标不参与校验，而不是判为不符。
    """
    out: list[dict[str, Any]] = []
    for m in metrics:
        sig = m.get("expression_signature")
        if not sig:
            continue
        out.append(
            {
                "name": m.get("name"),
                "display": m.get("display"),
                "require_all": list(sig.get("require_all") or []),
                "require_plain_aggregate": dict(
                    sig.get("require_plain_aggregate") or {}
                ),
            }
        )
    return out
