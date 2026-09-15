"""LangGraph 图定义：节点注册、边、条件路由。

图的结构：

    route ──┬─ chitchat ─► respond ─► END
            ├─ clarify  ─► clarify ─► END
            └─ query ─► retrieve ─► generate ─► validate ─┬─ 通过 ─► execute
                                                          └─ 失败 ─► fix ─┐
                                              execute ─┬─ 成功 ─► visualize ─► summarize ─► END
                                                       └─ 失败 ─► fix ──────┘（回 validate）

retry_count 在 validate 与 execute 之间共享，由 fix 节点递增，上限取自配置
（sql_max_retry）。它的语义是「已经修正了几次」，因此计在产生修正的那一侧；
放在失败侧递增会出现「配置写 3、放弃提示语写 4」这类差一错误。
达到上限后进入 give_up，返回包含最后一次语句与报错的可读说明，而不是无限循环
消耗 token。

依赖全部通过关键字参数注入到节点，图本身不创建任何对象。这样同一张图可以用
真实网关跑，也可以用桩实现或测试替身跑，图定义不需要分叉。
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Callable

from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph

from app.agent.nodes.execute import execute_node
from app.agent.nodes.fix import fix_node
from app.agent.nodes.generate import generate_node
from app.agent.nodes.retrieve import retrieve_node
from app.agent.nodes.route import route_node
from app.agent.nodes.summarize import summarize_node
from app.agent.nodes.validate import validate_node
from app.agent.nodes.visualize import visualize_node
from app.agent.state import ENTRY_NODE, AgentState, new_stage
from app.chart.builder import build_chart
from app.db.guard import SqlGuard
from app.db.repository import QueryRepository
from app.llm.gateway import Gateway
from app.semantic.loader import Catalog

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 终态节点
#
# respond / clarify / give_up 三个节点不调用模型、不访问数据库，只做固定文案
# 组装，因此直接写在图定义里。为三个没有依赖的三行函数各开一个文件，会让
# nodes/ 目录的「一文件一职责」变成「一文件一空壳」，反而看不出真正的结构。
# ----------------------------------------------------------------------

CHITCHAT_REPLY = (
    "我是这个电商经营数据集的查询助手，可以回答成交总额、销量、订单量、客单价、"
    "退款率、毛利这类问题。\n\n"
    "可以试着这样问：\n"
    "- 上个季度华东区各品类的成交总额\n"
    "- 各渠道近三个月的订单量\n"
    "- 今年以来各区域的退款率"
)


def respond_node(state: AgentState) -> dict[str, Any]:
    return {
        "status": "chitchat",
        "answer": CHITCHAT_REPLY,
        "messages": [AIMessage(content=CHITCHAT_REPLY)],
        "stages": [new_stage("respond", "回复", "非取数问题，已给出可用问法示例")],
    }


def clarify_node(state: AgentState) -> dict[str, Any]:
    question = state.get("clarify_question") or "能补充一下时间范围和分析维度吗？"
    return {
        "status": "clarify",
        "answer": question,
        "messages": [AIMessage(content=question)],
        "stages": [new_stage("clarify", "反问", "问题信息不足，等待用户补充")],
    }


def give_up_node(state: AgentState) -> dict[str, Any]:
    """重试耗尽后的兜底。

    不静默失败：把最后一次的语句与报错一并呈现，用户或开发者据此能判断问题出在
    字段理解、口径定义还是数据本身。
    """
    error = state.get("sql_error") or "未知原因"
    sql = (state.get("sql") or "").strip()
    attempts = int(state.get("retry_count") or 0)

    parts = [
        f"这条问题经过 {attempts} 次修正仍未得到可执行的查询，暂时无法给出结果。",
        "",
        f"最后一次的失败原因：{error}",
    ]
    if sql:
        parts += ["", "最后一次尝试的语句：", "```sql", sql, "```"]
    parts.append("")
    parts.append(
        "可以换一种更具体的问法，或缩小时间范围与分析维度后重试。"
        "如果是口径问题，请在 semantic/metrics.yaml 中补充该指标的定义。"
    )
    answer = "\n".join(parts)

    return {
        "status": "error",
        "error": error,
        "answer": answer,
        "messages": [AIMessage(content=answer)],
        "stages": [
            new_stage("give_up", "放弃", f"重试 {attempts} 次未通过：{error[:80]}")
        ],
    }


# ----------------------------------------------------------------------
# 条件路由
# ----------------------------------------------------------------------

def _after_route(state: AgentState) -> str:
    intent = state.get("intent") or "query"
    if intent == "chitchat":
        return "respond"
    if intent == "clarify":
        return "clarify"
    return "retrieve"


def _after_generate(state: AgentState) -> str:
    # 生成阶段就失败（网关异常）时不再走校验，直接给出可读说明
    if state.get("status") == "error" or not (state.get("sql") or "").strip():
        return "give_up"
    return "validate"


def _after_validate(state: AgentState, max_retry: int) -> str:
    if not state.get("sql_error"):
        return "execute"
    return "fix" if int(state.get("retry_count") or 0) < max_retry else "give_up"


def _after_execute(state: AgentState, max_retry: int) -> str:
    if not state.get("sql_error"):
        return "visualize"
    return "fix" if int(state.get("retry_count") or 0) < max_retry else "give_up"


# ----------------------------------------------------------------------
# 图装配
# ----------------------------------------------------------------------

def build_graph(
    *,
    gateway: Gateway,
    catalog: Catalog,
    guard: SqlGuard,
    repo: QueryRepository,
    checkpointer: Any = None,
    history_window: int = 6,
    max_retry: int = 3,
) -> Any:
    graph = StateGraph(AgentState)

    graph.add_node("route", partial(route_node, gateway=gateway, history_window=history_window))
    graph.add_node("retrieve", partial(retrieve_node, catalog=catalog))
    graph.add_node(
        "generate", partial(generate_node, gateway=gateway, history_window=history_window)
    )
    graph.add_node("validate", partial(validate_node, guard=guard))
    graph.add_node("execute", partial(execute_node, repo=repo))
    graph.add_node("fix", partial(fix_node, gateway=gateway))
    graph.add_node("visualize", partial(visualize_node, gateway=gateway))
    graph.add_node("summarize", partial(summarize_node, gateway=gateway))
    graph.add_node("respond", respond_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("give_up", give_up_node)

    # 入口节点名取自 state.ENTRY_NODE：进度归约靠它判断新一轮是否开始
    graph.set_entry_point(ENTRY_NODE)

    graph.add_conditional_edges(
        "route",
        _after_route,
        {"retrieve": "retrieve", "respond": "respond", "clarify": "clarify"},
    )
    graph.add_edge("retrieve", "generate")
    graph.add_conditional_edges(
        "generate", _after_generate, {"validate": "validate", "give_up": "give_up"}
    )
    graph.add_conditional_edges(
        "validate",
        partial(_after_validate, max_retry=max_retry),
        {"execute": "execute", "fix": "fix", "give_up": "give_up"},
    )
    graph.add_conditional_edges(
        "execute",
        partial(_after_execute, max_retry=max_retry),
        {"visualize": "visualize", "fix": "fix", "give_up": "give_up"},
    )
    # 修正后回到 validate 重新过一遍守卫，不允许绕过校验直接执行
    graph.add_edge("fix", "validate")

    graph.add_edge("visualize", "summarize")
    graph.add_edge("summarize", END)
    graph.add_edge("respond", END)
    graph.add_edge("clarify", END)
    graph.add_edge("give_up", END)

    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


def summarize_result(state: dict[str, Any]) -> dict[str, Any]:
    """把图终态整理成接入层要返回的结构。

    放在图定义旁边而不是接入层，是因为「什么算一轮的产出」属于编排层的契约；
    接入层只负责把它序列化成 HTTP 响应。
    """
    chart = state.get("chart") or None
    return {
        "status": state.get("status") or "ok",
        "answer": state.get("answer") or "",
        "insight": state.get("insight") or "",
        "highlights": list(state.get("highlights") or []),
        "sql": state.get("sql") or "",
        "original_sql": state.get("original_sql") or "",
        "sql_error": state.get("sql_error") or "",
        "guard_warnings": list(state.get("guard_warnings") or []),
        "columns": list(state.get("columns") or []),
        "rows": list(state.get("rows") or []),
        "row_count": int(state.get("row_count") or 0),
        "truncated": bool(state.get("truncated")),
        "query_elapsed_ms": state.get("query_elapsed_ms") or 0.0,
        "chart": chart,
        "metric_used": list(state.get("metric_used") or []),
        "caveats": list((state.get("retrieved") or {}).get("caveats") or []),
        "time_range": (state.get("retrieved") or {}).get("time_range"),
        "retry_count": int(state.get("retry_count") or 0),
        "intent": state.get("intent"),
        "stages": list(state.get("stages") or []),
    }


__all__ = [
    "build_graph",
    "summarize_result",
    "respond_node",
    "clarify_node",
    "give_up_node",
    "build_chart",
]
