"""编排层的状态定义。

状态是节点之间的唯一数据契约。字段按生命周期分组，节点只写自己负责的那组，
读别人的字段，不修改别人的字段。这样任何一个节点都可以单独测试。

有两处需要留意：

1. messages 用 add_messages 归约，配合 Checkpointer 实现多轮记忆；
2. stages 用 operator.add 归约，节点把进度事件追加进去，接入层据此推送 SSE。
   事件只增不改，因此可以边执行边推送，不需要等整张图跑完。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages


class StageEvent(TypedDict):
    """一次节点进度事件。前端据此显示「正在生成查询」等状态。"""

    node: str
    label: str
    detail: str
    elapsed_ms: float


def new_stage(node: str, label: str, detail: str = "", elapsed_ms: float = 0.0) -> StageEvent:
    """构造一条进度事件。节点统一用它，保证 SSE 事件的字段形态一致。"""
    return StageEvent(node=node, label=label, detail=detail, elapsed_ms=round(elapsed_ms, 1))


class RetrievedContext(TypedDict, total=False):
    """retrieve 节点的产出。既供 generate 拼 prompt，也供前端标注口径。"""

    context: str  # 渲染好的语义层段落，直接嵌入 prompt
    metric_displays: list[str]
    caveats: list[str]
    tables: list[str]
    matched_terms: list[str]
    time_range: dict[str, str] | None


class AgentState(TypedDict, total=False):
    # —— 会话 ——
    messages: Annotated[list, add_messages]
    session_id: str
    question: str  # 当前轮问题，可能是 rewrite 后的完整问法
    original_question: str  # 用户原话，历史与日志用

    # —— 意图 ——
    intent: Literal["query", "chitchat", "clarify"] | None
    intent_reason: str
    clarify_question: str | None

    # —— 语义召回 ——
    retrieved: RetrievedContext

    # —— SQL 生命周期 ——
    sql: str | None
    # 守卫改写前的原始语句。前端展示给用户的是实际执行的那条，回溯时看这条
    original_sql: str | None
    sql_error: str | None
    guard_warnings: list[str]
    retry_count: int
    fix_history: list[dict[str, str]]

    # —— 结果集 ——
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    query_elapsed_ms: float

    # —— 输出 ——
    chart: dict[str, Any] | None
    insight: str | None
    highlights: list[str]
    metric_used: list[str]
    answer: str | None

    # —— 控制 ——
    status: Literal["ok", "clarify", "chitchat", "error"]
    error: str | None
    stages: Annotated[list[StageEvent], operator.add]


def initial_state(session_id: str, question: str) -> AgentState:
    """一轮对话的初始状态。

    每轮只带 messages 与问题进入图，其余字段由节点填充，避免上一轮的
    rows / sql 泄漏到本轮造成误读。
    """
    return AgentState(
        session_id=session_id,
        question=question,
        original_question=question,
        retry_count=0,
        guard_warnings=[],
        fix_history=[],
        columns=[],
        rows=[],
        row_count=0,
        truncated=False,
        query_elapsed_ms=0.0,
        highlights=[],
        metric_used=[],
        stages=[],
    )
