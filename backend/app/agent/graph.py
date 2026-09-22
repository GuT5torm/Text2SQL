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

from app.agent.nodes.enrich import enrich_node
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
from app.db.guard import (
    SqlGuard,
    make_notice,
    rejected_notices_from_text,
)
from app.db.repository import QueryRepository
from app.llm.gateway import Gateway
from app.semantic.dimension_check import describe_substitution
from app.semantic.loader import UNSUPPORTED_CAVEAT_PREFIX, Catalog

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


def _after_execute(state: AgentState, max_retry: int, max_empty: int = 0) -> str:
    if state.get("sql_error"):
        return "fix" if int(state.get("retry_count") or 0) < max_retry else "give_up"
    # FR-F-1：空结果回灌用独立配额，且同一轮只允许放行一次（empty_result 在
    # fix 里被复位，回灌后再为空就直接出结论，不会第二次进 fix）。
    if state.get("empty_result") and not state.get("no_data_confirmed"):
        return "fix" if int(state.get("empty_retry") or 0) < max_empty else "visualize"
    return "visualize"


def _after_fix(state: AgentState) -> str:
    """fix 之后的去向。

    判定「确认无数据」时没有语句可再执行，直接进入 summarize；其余情形照旧
    回到 validate 重过守卫，不允许绕过校验直接执行。
    """
    if state.get("no_data_confirmed"):
        return "summarize"
    return "validate"


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
    max_empty: int = 1,
) -> Any:
    graph = StateGraph(AgentState)

    graph.add_node("route", partial(route_node, gateway=gateway, history_window=history_window))
    graph.add_node("retrieve", partial(retrieve_node, catalog=catalog))
    graph.add_node(
        "generate",
        partial(
            generate_node,
            gateway=gateway,
            history_window=history_window,
            guard=guard,
            catalog=catalog,
        ),
    )
    graph.add_node("validate", partial(validate_node, guard=guard))
    graph.add_node("execute", partial(execute_node, repo=repo))
    graph.add_node("enrich", partial(enrich_node, repo=repo, catalog=catalog))
    graph.add_node("fix", partial(fix_node, gateway=gateway))
    graph.add_node("visualize", partial(visualize_node, gateway=gateway, repo=repo))
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
        partial(_after_execute, max_retry=max_retry, max_empty=max_empty),
        {"visualize": "visualize", "fix": "fix", "give_up": "give_up"},
    )
    # FR-F-1：确认无数据时没有语句可执行，直接进 summarize；
    # 其余情形修正后回到 validate 重新过一遍守卫，不允许绕过校验直接执行
    graph.add_conditional_edges(
        "fix", _after_fix, {"validate": "validate", "summarize": "summarize"}
    )

    # execute 后跑 enrich：计算对比基准与异常判定，再走图表与总结
    # enrich 失败只影响衍生字段，不阻断主链路
    graph.add_edge("visualize", "enrich")
    graph.add_edge("enrich", "summarize")
    graph.add_edge("summarize", END)
    graph.add_edge("respond", END)
    graph.add_edge("clarify", END)
    graph.add_edge("give_up", END)

    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()



def _derive_node_path(stages: list[dict[str, Any]]) -> list[str]:
    """从 stages 提取节点序列去重 + 保序。同一节点多次出现（fix 回环）只取一次。"""
    seen: set[str] = set()
    out: list[str] = []
    for s in stages:
        name = s.get("node") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def derive_guard_notices(state: dict[str, Any]) -> list[dict[str, str]]:
    """把本轮守卫信息汇成结构化双通道（FR-I-2）。

    rejected：守卫/give_up 路径把 errors 用「；」拼进 sql_error，这里按同一
    规则还原；rewritten：validate 成功时透传的 R6 等改写告警。
    历史回放在 session.py 走同一函数，保证本轮与历史渲染一致。
    """
    notices = rejected_notices_from_text(state.get("sql_error"))
    notices.extend(
        make_notice("rewritten", w) for w in (state.get("guard_warnings") or [])
    )
    return notices


def _finalize_caveats(state: dict[str, Any]) -> list[str]:
    """FR-D-4 补充：未收录维度提示在拿到真实 SQL 后补上实际分组字段。

    召回阶段只能说「库里没有这个维度」，等 SQL 出来才知道系统实际用了哪个字段
    顶替。两件事放一起说，用户才核对得动。拿不到分组字段时保留召回阶段的原文，
    提示不会丢失。
    """
    retrieved = state.get("retrieved") or {}
    original = list(retrieved.get("caveats") or [])
    unsupported = list(retrieved.get("unsupported_dimensions") or [])
    if not unsupported:
        return original
    note = describe_substitution(state.get("sql") or "", unsupported)
    if not note:
        return original
    return [
        c for c in original if not c.startswith(UNSUPPORTED_CAVEAT_PREFIX)
    ] + [note]


def summarize_result(state: dict[str, Any]) -> dict[str, Any]:
    """把图终态整理成接入层要返回的结构。

    放在图定义旁边而不是接入层，是因为「什么算一轮的产出」属于编排层的契约；
    接入层只负责把它序列化成 HTTP 响应。
    """
    chart = state.get("chart") or None
    persist_rows = list(state.get("rows") or [])
    return {
        "status": state.get("status") or "ok",
        "answer": state.get("answer") or "",
        "insight": state.get("insight") or "",
        "highlights": list(state.get("highlights") or []),
        "sql": state.get("sql") or "",
        "original_sql": state.get("original_sql") or "",
        "sql_error": state.get("sql_error") or "",
        "guard_warnings": list(state.get("guard_warnings") or []),
        # FR-I-2：结构化守卫告警（rejected/rewritten 双通道）
        "guard_notices": derive_guard_notices(state),
        "columns": list(state.get("columns") or []),
        "rows": persist_rows,
        "row_count": int(state.get("row_count") or 0),
        # 重定义 truncated：true 表示执行时被 max_rows 截断。
        # 落库上限 PERSIST_ROWS 不算 truncated，是与执行端的另一种截断。
        "truncated": bool(state.get("truncated")),
        "query_elapsed_ms": state.get("query_elapsed_ms") or 0.0,
        "chart": chart,
        "metric_used": list(state.get("metric_used") or []),
        "caveats": _finalize_caveats(state),
        "time_range": (state.get("retrieved") or {}).get("time_range"),
        "retry_count": int(state.get("retry_count") or 0),
        "intent": state.get("intent"),
        "stages": list(state.get("stages") or []),
        # —— FR-C-1 写法说明 ——
        "sql_reasoning": state.get("sql_reasoning") or "",
        "fix_reasoning": state.get("fix_reasoning") or "",
        # —— FR-F-2 修正痕迹（修正过的轮次才有）——
        "fix_note": state.get("fix_note"),
        # —— FR-F-1 空结果回灌 ——
        "empty_retry": int(state.get("empty_retry") or 0),
        "no_data_confirmed": bool(state.get("no_data_confirmed")),
        # —— FR-D-1 来源表 ——
        "source_tables": list(state.get("source_tables") or []),
        # —— FR-D-2 数据时效 ——
        "data_freshness": state.get("data_freshness"),
        # —— FR-H-2 补全 ——
        "persist_row_count": int(state.get("persist_row_count") or 0),
        # node_path 不在 state 上累加（避免给 LangGraph 加 list reducer），
        # 而是从 stages 字段按节点名去重得到，与落库口径保持一致。
        "node_path": _derive_node_path(list(state.get("stages") or [])),
        # 第 6 批 (FR-L-1) 与第 4 批 (FR-M-1) 才落值的字段，先占位返回 None
        "findings": list(state.get("findings") or []),
        "baselines": state.get("baselines"),
        # FR-M-2 的两个标注字段提到顶层：前端判定「本期未完整」与粒度时
        # 不必深挖 baselines，也不必处理 baselines 为 None 的情形
        "partial_period": bool(state.get("partial_period")),
        "granularity": state.get("granularity"),
        # —— FR-B-3 结论数字待查 / FR-D-5 时间区间一致性 ——
        "number_checks": list(state.get("number_checks") or []),
        "time_range_consistent": state.get("time_range_consistent"),
        # —— FR-A-5 结构化槽位 ——
        "slots": state.get("slots") or {},
        # —— FR-H-3 原话与改写句 ——
        "original_question": state.get("original_question") or state.get("question") or "",
        "rewritten_question": state.get("rewritten_question") or "",
        # —— FR-E-4 修正历史 ——
        # fix_history 在 execute / validate / fix 三处累加，结构 [{stage, error}]。
        # 之前只渲染进 fix 节点 prompt，没外露给前端，FR-E-4 要求可见可核对。
        "fix_history": list(state.get("fix_history") or []),
    }


__all__ = [
    "build_graph",
    "summarize_result",
    "respond_node",
    "clarify_node",
    "give_up_node",
    "build_chart",
]
