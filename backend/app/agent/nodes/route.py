"""route 节点：意图识别与澄清判断。

产出三件事：这一问是不是取数问题、省略主语的追问补全成什么、要不要反问用户。
三者都来自一次模型调用，不额外增加指代消解节点。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.prompts import format_history, render_prompt
from app.agent.state import AgentState, new_stage
from app.llm.gateway import Gateway, LLMError
from app.llm.schemas import RouteDecision

logger = logging.getLogger(__name__)

NODE = "route"


def route_node(state: AgentState, *, gateway: Gateway, history_window: int = 6) -> dict[str, Any]:
    started = time.monotonic()
    question = state.get("question") or ""
    prompt = render_prompt(
        "route",
        history=format_history(state.get("messages") or [], history_window) or "（无）",
        question=question,
    )

    try:
        decision = gateway.complete_json(prompt, RouteDecision, tag=NODE)
    except LLMError as exc:
        # 路由失败不该让整轮问答失败。降级为按取数处理，把问题交给下游，
        # 由守卫与语义层去约束，比直接给用户报错更有用。
        logger.warning("意图识别失败，降级为取数处理：%s", exc)
        return {
            "intent": "query",
            "intent_reason": f"意图识别调用失败，已按取数处理：{exc}",
            "stages": [
                new_stage(NODE, "理解问题", "识别服务异常，按取数继续", _ms(started))
            ],
        }

    update: dict[str, Any] = {
        "intent": decision.intent,
        "intent_reason": decision.reason,
        "stages": [
            new_stage(
                NODE,
                "理解问题",
                _describe(decision),
                _ms(started),
            )
        ],
    }

    if decision.intent == "clarify" and not (decision.clarify_question or "").strip():
        # 模型说要反问但没给问题，反问就无从进行，退回取数
        logger.info("模型判定 clarify 但未给出反问内容，改判 query")
        update["intent"] = "query"
        update["intent_reason"] = "判定为需要澄清但未给出具体问题，已按取数处理"

    if update["intent"] == "clarify":
        update["clarify_question"] = decision.clarify_question.strip()
    else:
        update["clarify_question"] = None

    rewritten = (decision.rewritten_question or "").strip()
    if rewritten and rewritten != question:
        # 补全后的问题写回 question，下游节点与落库记录都用它
        update["question"] = rewritten

    return update


def _describe(decision: RouteDecision) -> str:
    if decision.intent == "chitchat":
        return "判定为闲聊"
    if decision.intent == "clarify":
        return "信息不足，需要反问用户"
    return "判定为取数问题"


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
