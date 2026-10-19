"""复杂查询表单入口。

POST /api/query/form 与 POST /api/chat 是姊妹路径：同一个 state、同一张图、
同一份落库，差异只在输入形态——指标、维度与过滤条件由表单直接给出，route 不再
让模型从问句里抽一遍。

表单没有「用户原话」，渲染出的那句可读问句就是原话：它同时承担语义召回
（指标口径标注）、落库与历史回看三件事。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agent.checkpointer import thread_config
from app.agent.form_query import (
    as_conditions,
    build_form_payload,
    render_form_question,
)
from app.agent.graph import summarize_result
from app.agent.state import initial_state
from app.api.chat import persist_turn
from app.api.deps import Container, get_container
from app.db.masking import mask_result
from app.semantic.filters import (
    describe_condition,
    render_predicate,
    validate_form_query,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["form-query"])


class FormCondition(BaseModel):
    """一条表单条件。算子与取值受语义层 filters.yaml 约束，提交前校验。"""

    field: str = Field(description="语义层登记的字段，如 orders.region")
    op: Literal["eq", "in", "between", "gte", "lte"] = Field(description="算子")
    value: str | list[str] | None = Field(default=None, description="取值；in 用数组")
    value_to: str | None = Field(default=None, description="between 的结束值")


class FormQueryBody(BaseModel):
    """表单查询的查询体。提交与预演共用，保证两处校验口径一致。"""

    metric: str = Field(description="指标，配置名或显示名")
    dimensions: list[str] = Field(default_factory=list, description="分组维度")
    conditions: list[FormCondition] = Field(default_factory=list)
    time_range: dict[str, str] | None = Field(
        default=None, description="{start, end}，左闭右开"
    )
    order_by: dict[str, Any] | None = None
    top_n: int | None = None


class FormQueryRequest(FormQueryBody):
    session_id: str = Field(description="会话ID，由 POST /api/session 创建")


def _reject(problems: list[str]) -> None:
    # 400 而不是 500：这是用户提交的条件与语义层不匹配，不是服务端故障。
    # 错误逐条给出未登记项与可用项，让用户知道该改哪一个。
    raise HTTPException(status_code=400, detail={"errors": problems})


@router.post("/query/form/preview")
def preview_form(
    req: FormQueryBody, container: Container = Depends(get_container)
) -> dict[str, Any]:
    """提交前预演：把表单渲染成可读问句与谓词清单，不执行、不落库。

    渲染规则只有后端一份（filters.py 的 render_predicate / render_form_question），
    前端照抄一遍必然漂移。预演让用户在执行前看到「这几个条件会变成什么」，
    这是方案 P5 要求的提交前回显。
    """
    payload = req.model_dump()
    problems = validate_form_query(payload, container.catalog)
    if problems:
        _reject(problems)

    form = build_form_payload(payload, container.catalog)
    conditions = as_conditions(form["form_conditions"] or [])
    return {
        "question": render_form_question(
            form["form_query"], form["form_conditions"], container.catalog
        ),
        "predicates": [render_predicate(c) for c in conditions],
        "descriptions": [
            describe_condition(c, container.catalog) for c in conditions
        ],
        "conditions": form["form_conditions"],
    }


@router.post("/query/form")
def query_form(
    req: FormQueryRequest, container: Container = Depends(get_container)
) -> dict[str, Any]:
    """结构化查询。校验不通过时一次列出全部问题，不做部分执行。"""
    if container.store.get_session(req.session_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{req.session_id}")

    payload = req.model_dump()
    problems = validate_form_query(payload, container.catalog)
    if problems:
        _reject(problems)

    form = build_form_payload(payload, container.catalog)
    question = render_form_question(
        form["form_query"], form["form_conditions"], container.catalog
    )

    config = thread_config(req.session_id)
    inputs: dict[str, Any] = {
        **initial_state(req.session_id, question),
        "messages": [HumanMessage(content=question)],
        "query_source": "form",
        **form,
    }
    final = container.graph.invoke(inputs, config=config)
    result = summarize_result(final)
    # 与 /api/chat 同一脱敏点，两种入口出参一致
    mask_result(result, container.catalog)
    persist_turn(container, req.session_id, question, result)
    return result
