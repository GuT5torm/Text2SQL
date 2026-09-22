"""编排层的状态定义。

状态是节点之间的唯一数据契约。字段按生命周期分组，节点只写自己负责的那组，
读别人的字段，不修改别人的字段。这样任何一个节点都可以单独测试。

有两处需要留意：

1. messages 用 add_messages 归约，配合 Checkpointer 实现多轮记忆；
2. stages 用 merge_stages 归约，节点把进度事件追加进去，接入层据此推送 SSE。
   事件只增不改，因此可以边执行边推送，不需要等整张图跑完。归约器没有直接用
   operator.add，原因见 merge_stages 的注释——operator.add 会让进度跨轮累积。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

# 图的入口节点名。图装配用它注册入口，进度归约用它判断新一轮是否开始，
# 两处必须指向同一个节点，否则 stages 的跨轮重置会失效。
ENTRY_NODE = "route"


class StageEvent(TypedDict):
    """一次节点进度事件。前端据此显示「正在生成查询」等状态。"""

    node: str
    label: str
    detail: str
    elapsed_ms: float


def merge_stages(left: list[StageEvent], right: list[StageEvent]) -> list[StageEvent]:
    """stages 的归约器：同一轮内追加，跨轮重置。

    改用自定义函数而不是 operator.add，是因为 Checkpointer 会把上一轮的终态
    一起保存下来，operator.add 的语义是「旧值加新值」，第二轮就会得到 14 条、
    第三轮 21 条，前端进度条越跑越长，且终态里的 stages 混入了上一轮的内容。

    判定依据是入口节点：route 每轮必定最先执行且只执行一次，它出现在 right 中
    即代表新一轮开始，此时丢弃 left。同一轮内其余节点（包括 fix 回环后的
    validate、execute）的事件不含 route，走正常的追加分支。
    """
    if any(s.get("node") == ENTRY_NODE for s in right):
        return list(right)
    return list(left) + list(right)


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
    original_question: str  # 用户原话，历史与日志用（FR-H-3：落库并回看）
    # FR-H-3：路由改写后的问句。与 question 同值时说明本轮没发生改写
    rewritten_question: str

    # —— 意图 ——
    intent: Literal["query", "chitchat", "clarify"] | None
    intent_reason: str
    clarify_question: str | None
    # FR-A-5：结构化槽位 {指标, 维度, 过滤, 时间, 排序, TopN}。
    # route 节点产出，供前端标注与评测断言。必须显式声明：LangGraph 只认
    # TypedDict 里出现过的键，未声明的键由节点返回后会被归约器静默丢弃。
    slots: dict[str, Any]

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

    # —— FR-C-1 写法说明 ——
    # generate 节点给出的 SQL 写法依据，供 SqlPanel 折叠项（与 stage.detail 解耦）
    sql_reasoning: str
    # fix 节点给出的最后一次修正理由，与 sql_reasoning 并存便于回看
    fix_reasoning: str
    # —— FR-F-2 修正痕迹 ——
    # {attempts, reason}，修正过的轮次随结论返回，供前端标注与断言
    fix_note: dict[str, Any] | None

    # —— FR-F-1 空结果回灌 ——
    # 与 retry_count 分开计数：空结果与报错是两类失败，共享配额会让报错修正
    # 少一次机会。上限取自配置，默认 1。
    empty_retry: int
    # 本轮执行成功但返回 0 行。为 True 时路由走 fix 的回灌分支
    empty_result: bool
    # fix 判定「确认无数据」后置位，此时不再回灌，按正常结论给出
    no_data_confirmed: bool

    # —— FR-D-1 来源表（实际执行 SQL 的 FROM/JOIN 解析）——
    source_tables: list[str]

    # —— FR-D-2 数据时效（库内 MAX + 配置兜底）——
    data_freshness: dict[str, Any] | None

    # —— FR-H-2 补全：执行后的实际行数，用于区分执行截断与回看不全 ——
    persist_row_count: int

    # —— FR-M-1 / FR-M-2 / FR-M-3 衍生字段 ——
    # baselines 是嵌套 dict（包含 mom / yoy / partial_period / current_period）
    # partial_period 与 granularity 提到顶层便于前端直接读，不必深挖 baselines
    #
    # 三者都必须显式声明：LangGraph 只认 TypedDict 里出现过的键，未声明的键
    # 由节点返回后会被归约器静默丢弃，表现为「节点算出来了但终态里是空的」。
    baselines: dict[str, Any] | None
    partial_period: bool
    granularity: str | None

    # —— FR-L-1 异常判定 ——
    findings: list[dict[str, Any]]

    # —— FR-B-3 结论数字待查清单 / FR-D-5 时间区间一致性 ——
    # 两者都是纯函数核对结果，未声明的键会被 LangGraph 静默丢弃。
    number_checks: list[dict[str, Any]]
    time_range_consistent: bool | None

    # —— 控制 ——
    status: Literal["ok", "clarify", "chitchat", "error"]
    error: str | None
    stages: Annotated[list[StageEvent], merge_stages]


def initial_state(session_id: str, question: str) -> AgentState:
    """一轮对话的初始状态。

    每轮只带 messages 与问题进入图，其余字段由节点填充，避免上一轮的
    rows / sql 泄漏到本轮造成误读。
    """
    return AgentState(
        session_id=session_id,
        question=question,
        original_question=question,
        rewritten_question=question,
        retry_count=0,
        empty_retry=0,
        empty_result=False,
        no_data_confirmed=False,
        guard_warnings=[],
        fix_history=[],
        columns=[],
        rows=[],
        row_count=0,
        truncated=False,
        query_elapsed_ms=0.0,
        highlights=[],
        metric_used=[],
        # 传空列表只是为了让字段存在。Checkpointer 里的上一轮内容会在 route
        # 节点产出第一条事件时被 merge_stages 丢弃，不需要在这里手动清空
        stages=[],
    )
