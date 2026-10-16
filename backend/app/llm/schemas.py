"""结构化输出的 Pydantic 模型。

节点需要模型返回固定结构时，先用这里的模型声明契约，再由网关
（llm/gateway.py）负责解析、校验与失败重试。把契约集中在一处，
是为了让「模型该返回什么」与「节点怎么用」分开演进。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RouteDecision(BaseModel):
    """route 节点的输出：判断这一问属于哪一类。"""

    # out_of_scope / attribution 是两类「不进取数链路」的终态：
    # 前者是数据集里根本没有这个东西或根本不是取数任务，后者是问因果而
    # 单条聚合查询只能给结果、给不出原因。两者都必须在此显式声明，否则
    # 模型会把它塞进 query，下游照旧生成一条 SQL，用户拿到一个答非所问的数字。
    intent: Literal["query", "chitchat", "clarify", "out_of_scope", "attribution"] = Field(
        description=(
            "query=取数问题；chitchat=与数据无关的闲聊；clarify=信息不足需要反问；"
            "out_of_scope=超出本数据集能力范围；attribution=问原因/因果的问题"
        )
    )
    # 指代消解结果。「那上个月呢」这类省略主语的追问在这里被补全为完整问题，
    # 以免后续节点各自处理上下文，多花一次模型调用。
    rewritten_question: str | None = Field(
        default=None, description="把追问补全为完整问题；无需改写时留空"
    )
    clarify_question: str | None = Field(
        default=None, description="intent=clarify 时向用户提出的具体问题"
    )
    reason: str = Field(default="", description="判断依据，一句话")
    # FR-A-5：槽位结构化。把问题拆成 {指标, 维度, 过滤, 时间, 排序, TopN}
    # 六类，供前端标注与评测断言。模型在真实模式下负责填充；桩模式由
    # gateway._mock_json 按关键词识别后填充，切到真实模型后自动生效。
    slots: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "结构化槽位：metrics/dimensions/filters/time_range/order_by/top_n。"
            "未识别的键留空数组或空字符串，不要省略键名。"
        ),
    )


class SqlDraft(BaseModel):
    """generate 与 fix 节点的输出。"""

    # FR-F-1：空结果回灌时模型可以回答「确认无数据」而不给语句。这一路必须允许
    # sql 为空，否则「放宽条件也拿不到数据」这个正确判断会被 schema 挡回成错误。
    confirm_no_data: bool = Field(
        default=False,
        description="判定该条件下确实没有数据，不再生成语句。为真时 sql 可为空",
    )
    sql: str = Field(default="", description="单条 SQLite 查询语句，只输出这段 SQL")
    reasoning: str = Field(default="", description="为什么这样写，一到两句")
    metric_used: list[str] = Field(
        default_factory=list, description="本轮实际采用的指标口径显示名，用于前端标注"
    )

    @field_validator("sql")
    @classmethod
    def _clean(cls, v: str, info: Any) -> str:
        from app.db.guard import strip_sql_fence

        cleaned = strip_sql_fence(v)
        if not cleaned:
            # 判定「确认无数据」时允许空语句；其余情形空语句仍视为无效输出
            if getattr(info, "data", {}).get("confirm_no_data"):
                return ""
            raise ValueError("SQL 为空")
        return cleaned


class ChartSpec(BaseModel):
    """visualize 节点的输出。

    模型只负责「选类型 + 指定字段映射」，不直接产出 ECharts option。
    这样即使模型选错字段，前端字段校验与 chart/builder.py 的兜底仍能兜住，
    不会渲染出空白图表。
    """

    chart_type: Literal["line", "bar", "pie", "table"] = Field(description="图表类型")
    x_field: str | None = Field(default=None, description="维度字段，取自结果集列名")
    y_fields: list[str] = Field(default_factory=list, description="度量字段，取自结果集列名")
    series_field: str | None = Field(
        default=None, description="需要拆成多条系列的字段，单系列时留空"
    )
    title: str = Field(default="", description="图表标题")
    reason: str = Field(default="", description="选择该图型的依据，一句话")


class Insight(BaseModel):
    """summarize 节点的输出。"""

    conclusion: str = Field(description="经营结论，两到四句，只依据给定结果集")
    highlights: list[str] = Field(
        default_factory=list, description="值得单独点出的数字，每条一句话"
    )
    caveats: list[str] = Field(
        default_factory=list, description="本轮口径边界，从语义层给的说明中挑选相关的"
    )
