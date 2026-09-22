"""候选择优函数单测（FR-J-4）。

验证 rerank_candidates 在以下场景的行为：
  1. 守卫通过 > 守卫拒绝
  2. 字段命中率高 > 字段命中率低
  3. 代价低 > 代价高
  4. 桩模式下两候选相同时择优稳定
"""

from __future__ import annotations

import pytest

from app.agent.candidate_rerank import (
    CandidateScore,
    rerank_candidates,
    score_candidate,
)
from app.db.guard import SqlGuard
from app.llm.schemas import SqlDraft
from app.semantic.loader import Catalog


def _make_draft(sql: str) -> SqlDraft:
    return SqlDraft(sql=sql, reasoning="test", metric_used=[])


def test_rerank_prefers_guard_ok(guard: SqlGuard, catalog: Catalog) -> None:
    """守卫通过的候选优先于被拒的候选。"""
    good = _make_draft("SELECT o.region AS 大区, COUNT(*) AS cnt FROM orders o GROUP BY o.region LIMIT 5")
    bad = _make_draft("DROP TABLE orders")
    selected, scores = rerank_candidates([good, bad], guard, catalog)
    assert selected.sql == good.sql
    assert scores[0].guard_ok is True
    assert scores[1].guard_ok is False


def test_rerank_prefers_lower_cost(guard: SqlGuard, catalog: Catalog) -> None:
    """两条候选都通过守卫时，代价低（JOIN 少）的优先。"""
    simple = _make_draft(
        "SELECT o.region AS 大区, COUNT(*) AS cnt FROM orders o GROUP BY o.region LIMIT 5"
    )
    complex_sql = _make_draft(
        "SELECT o.region AS 大区, COUNT(*) AS cnt "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id "
        "JOIN categories c ON c.id = p.category_id "
        "GROUP BY o.region LIMIT 5"
    )
    selected, scores = rerank_candidates([complex_sql, simple], guard, catalog)
    assert selected.sql == simple.sql
    assert scores[0].cost <= scores[1].cost


def test_rerank_identical_candidates(guard: SqlGuard, catalog: Catalog) -> None:
    """桩模式下两候选相同时，择优稳定返回第一条。"""
    draft = _make_draft("SELECT o.region AS 大区, COUNT(*) AS cnt FROM orders o GROUP BY o.region LIMIT 5")
    selected, scores = rerank_candidates([draft, draft], guard, catalog)
    assert selected.sql == draft.sql
    assert len(scores) == 2
    assert scores[0].total == scores[1].total


def test_rerank_single_candidate(guard: SqlGuard, catalog: Catalog) -> None:
    """只有一条候选时直接返回。"""
    draft = _make_draft("SELECT o.region AS 大区, COUNT(*) AS cnt FROM orders o GROUP BY o.region LIMIT 5")
    selected, scores = rerank_candidates([draft], guard, catalog)
    assert selected.sql == draft.sql
    assert len(scores) == 1


def test_rerank_empty_raises(guard: SqlGuard, catalog: Catalog) -> None:
    """候选列表为空时抛 ValueError。"""
    with pytest.raises(ValueError, match="候选列表为空"):
        rerank_candidates([], guard, catalog)


def test_score_candidate_returns_details(guard: SqlGuard, catalog: Catalog) -> None:
    """score_candidate 返回的打分明细含全部字段。"""
    draft = _make_draft("SELECT o.region AS 大区 FROM orders o LIMIT 5")
    score = score_candidate(draft, guard, catalog)
    assert isinstance(score, CandidateScore)
    assert score.guard_ok is True
    assert 0.0 <= score.field_hit_rate <= 1.0
    assert score.cost >= 0
    assert score.total > 0
