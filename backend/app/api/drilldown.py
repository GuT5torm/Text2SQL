"""下钻接口。

POST /api/drilldown 返回下一轮的问句，不执行查询。

为什么只返回问句而不直接跑完整链路：下钻的本质是槽位继承，产出是「下一句
该怎么问」，执行仍走 /api/chat 的既有链路——同一套守卫、同一套超时、同一套
落库。接口直接跑链路会绕过前端对 SSE 进度的处理，也会让「下钻轮次与普通轮次
一样落库」变成两套写入路径。

字段与 findings 保持同名（metric / dimension / slice），前端可以直接把异常
标记原样提交，不需要做一层映射——字段名分叉是这类需求最容易埋的坑。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agent.drilldown import DrilldownPlan, build_drilldown
from app.api.deps import Container, get_container

router = APIRouter(prefix="/api", tags=["drilldown"])


class DrilldownRequest(BaseModel):
    source: Literal["finding", "cell"] = Field(description="触发点：异常标记或结果表单元格")
    metric: str | None = Field(default=None, description="主指标显示名，如「成交总额」")
    dimension: str | None = Field(default=None, description="当前切片所属维度，如「大区」")
    slice: str | None = Field(default=None, description="切片取值，如「华东」")
    time_label: str | None = Field(default=None, description="时间限定说法，如「上个月」")


@router.post("/drilldown")
def drilldown(
    req: DrilldownRequest, container: Container = Depends(get_container)
) -> dict[str, Any]:
    plan: DrilldownPlan = build_drilldown(
        source=req.source,
        metric=req.metric,
        dimension=req.dimension,
        slice_value=req.slice,
        catalog=container.catalog,
        time_label=req.time_label,
    )
    return {
        "question": plan.question,
        "available": plan.available,
        "source": plan.source,
        "metric": plan.metric,
        "dimension": plan.dimension,
        "slice": plan.slice_value,
        "next_dimension": plan.next_dimension,
        "next_dimension_display": plan.next_dimension_display,
        "reason": plan.reason,
    }
