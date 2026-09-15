"""结构化输出的 Pydantic 模型。

节点需要模型返回固定结构时，先用这里的模型声明契约，再由网关
（llm/gateway.py）负责解析、校验与失败重试。把契约集中在一处，
是为了让「模型该返回什么」与「节点怎么用」分开演进。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RouteDecision(BaseModel):
    """route 节点的输出：判断这一问属于哪一类。"""

    intent: Literal["query", "chitchat", "clarify"] = Field(
        description="query=取数问题；chitchat=与数据无关的闲聊；clarify=信息不足需要反问"
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


class SqlDraft(BaseModel):
    """generate 与 fix 节点的输出。"""

    sql: str = Field(description="单条 SQLite 查询语句，只输出这段 SQL")
    reasoning: str = Field(default="", description="为什么这样写，一到两句")
    metric_used: list[str] = Field(
        default_factory=list, description="本轮实际采用的指标口径显示名，用于前端标注"
    )

    @field_validator("sql")
    @classmethod
    def _clean(cls, v: str) -> str:
        from app.db.guard import strip_sql_fence

        cleaned = strip_sql_fence(v)
        if not cleaned:
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
