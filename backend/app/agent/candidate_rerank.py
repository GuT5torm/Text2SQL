"""候选重排择优函数（FR-J-4）。

generate 节点可一次生成两条候选 SQL，择优函数按守卫通过情况、字段命中率
与执行代价选一条。桩模式下两候选相同，但择优函数本身可单测。

评分维度（权重可调）：
  1. 守卫通过（ok）   权重 100：通过守卫的候选优先于被拒的
  2. 字段命中率      权重 10：候选 SQL 中命中的语义层字段比例
  3. 执行代价          权重 1：SQL 复杂度（JOIN 数 + 子查询数），越少越好

总分 = 守卫分 + 字段命中分 - 代价分。选总分高的候选。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.guard import SqlGuard
from app.llm.schemas import SqlDraft
from app.semantic.loader import Catalog


@dataclass
class CandidateScore:
    """一条候选的打分明细。"""

    draft: SqlDraft
    guard_ok: bool
    field_hit_rate: float
    cost: int
    total: float

    def __repr__(self) -> str:
        return (
            f"CandidateScore(guard_ok={self.guard_ok}, "
            f"field_hit={self.field_hit_rate:.2f}, cost={self.cost}, "
            f"total={self.total:.1f})"
        )


# 权重常量。提取到模块级是为了单测能直接引用，不依赖魔法数字。
W_GUARD = 100.0
W_FIELD = 10.0
W_COST = 1.0


def _estimate_cost(sql: str) -> int:
    """粗估 SQL 执行代价：JOIN 数 + 子查询数。

    只做文本级计数，不解析 AST，因为择优是在守卫之前做的快速筛选。
    代价高 = JOIN 多或子查询嵌套深，优先选简单的。
    """
    sql_lower = sql.lower()
    join_count = sql_lower.count(" join ")
    subquery_count = sql_lower.count("(select") + sql_lower.count("( select")
    return join_count + subquery_count


def _field_hit_rate(sql: str, catalog: Catalog) -> float:
    """候选 SQL 中命中的语义层字段比例。

    把 SQL 文本中出现的语义层字段名数 / 语义层总字段数。
    这是一个粗略指标：越多字段出现在 SQL 中，说明候选越贴合语义层。
    """
    all_fields = catalog.all_field_refs()
    sql_lower = sql.lower()
    total_fields = 0
    hit_fields = 0
    for table, fields in all_fields.items():
        for f in fields:
            total_fields += 1
            if f in sql_lower:
                hit_fields += 1
    return hit_fields / total_fields if total_fields > 0 else 0.0


def score_candidate(
    draft: SqlDraft,
    guard: SqlGuard,
    catalog: Catalog,
) -> CandidateScore:
    """给一条候选打分。"""
    guard_result = guard.check(draft.sql)
    field_rate = _field_hit_rate(draft.sql, catalog)
    cost = _estimate_cost(draft.sql)
    total = (
        (W_GUARD if guard_result.ok else 0.0)
        + W_FIELD * field_rate
        - W_COST * cost
    )
    return CandidateScore(
        draft=draft,
        guard_ok=guard_result.ok,
        field_hit_rate=field_rate,
        cost=cost,
        total=total,
    )


def rerank_candidates(
    candidates: list[SqlDraft],
    guard: SqlGuard,
    catalog: Catalog,
) -> tuple[SqlDraft, list[CandidateScore]]:
    """从多条候选中择优。

    返回 (选中的候选, 全部候选的打分明细)。打分明细供日志与单测使用。
    候选数不足 1 时抛 ValueError；1 条时直接返回。
    """
    if not candidates:
        raise ValueError("候选列表为空，无法择优")

    scores = [score_candidate(c, guard, catalog) for c in candidates]
    # 按总分降序，选第一条
    scores.sort(key=lambda s: s.total, reverse=True)
    return scores[0].draft, scores
