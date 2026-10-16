"""域外与归因两类终态的单测。

锁住三件事：

1. 这两类意图必须终结在 route 之后，不能落进取数链路。判错的表现不是报错，
   而是一条语法正确、能执行、有图表的 SQL —— 用户拿到无关数字且全程无提示，
   是所有失败形态里最难被发现的一种，因此必须能测。
2. 终态不给语句。这两类问题的「有输出」恰恰是错误信号。
3. 归因终态的替代问法按槽位拼，不是一段通用建议。指标与时间取不到时要有兜底，
   兜底措辞变了用户就照着问不出东西。

桩实现的关键词识别也在这里断言：只改提示词而漏改桩实现，会出现桩模式下
新分支永不触发的评测假绿。
"""

from __future__ import annotations

from typing import Any

from app.agent.graph import (
    _after_route,
    attribution_node,
    out_of_scope_node,
)
from app.llm.gateway import _mock_route_decision
from app.llm.schemas import RouteDecision


def _route_prompt(question: str) -> str:
    """构造 route 节点提示词的桩实现输入。

    桩实现按「本轮问题：」之后的正文取关键词，因此只需要这一小段。
    """
    return f"## 输入\n\n最近对话：（无）\n\n本轮问题：\n{question}\n"


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------


def test_after_route_maps_new_intents() -> None:
    assert _after_route({"intent": "out_of_scope"}) == "out_of_scope"
    assert _after_route({"intent": "attribution"}) == "attribution"
    # 未识别意图仍进取数链路：路由失败不该让整轮问答失败
    assert _after_route({}) == "retrieve"


def test_route_decision_accepts_new_intents() -> None:
    for intent in ("out_of_scope", "attribution"):
        decision = RouteDecision(intent=intent, reason="x")
        assert decision.intent == intent


# ----------------------------------------------------------------------
# 终态节点
# ----------------------------------------------------------------------


def test_out_of_scope_terminal_has_no_sql() -> None:
    state: dict[str, Any] = {
        "question": "帮我预测下个月销量",
        "original_question": "帮我预测下个月销量",
        "intent_reason": "要的是尚未发生的数",
    }
    out = out_of_scope_node(state)
    assert out["status"] == "out_of_scope"
    assert "预测" in out["answer"]
    # 终态不给语句：这两类问题的「有输出」恰恰是错误信号
    assert not out.get("sql")
    assert out["messages"][0].content == out["answer"]


def test_attribution_terminal_uses_slots() -> None:
    state: dict[str, Any] = {
        "question": "为什么上个月销售额下降了",
        "original_question": "为什么上个月销售额下降了",
        "slots": {"metrics": ["成交总额"], "time_range": "上个月"},
    }
    out = attribution_node(state)
    assert out["status"] == "attribution"
    assert not out.get("sql")
    # 替代问法按槽位拼，不是一段通用建议
    assert "上个月" in out["answer"]
    assert "成交总额" in out["answer"]


def test_attribution_falls_back_when_slots_empty() -> None:
    """槽位为空时仍要给出可照着问的句子：兜底措辞变了用户就问不出东西。"""
    out = attribution_node({"slots": {}})
    assert out["status"] == "attribution"
    assert "成交额" in out["answer"]
    assert "区域" in out["answer"]
    assert "最近一个月" in out["answer"]


# ----------------------------------------------------------------------
# 桩实现关键词识别
# ----------------------------------------------------------------------


def test_mock_flags_out_of_scope() -> None:
    for question in ("帮我预测下个月销量", "库存还有多少", "怎么写爬虫", "预估下季度的成交总额"):
        decision = _mock_route_decision(_route_prompt(question))
        assert decision["intent"] == "out_of_scope", question


def test_mock_flags_attribution() -> None:
    for question in ("为什么上个月销售额下降了", "上个月退款率上升是什么原因"):
        decision = _mock_route_decision(_route_prompt(question))
        assert decision["intent"] == "attribution", question
        # 槽位保留：归因终态要用它拼替代问法
        assert decision["slots"]["metrics"]


def test_mock_keeps_reason_dimension_as_query() -> None:
    """「原因」是维度名而不是归因疑问。裸词不登记就是为了让这一条仍是取数。"""
    decision = _mock_route_decision(_route_prompt("各退款原因的退款率"))
    assert decision["intent"] == "query"
