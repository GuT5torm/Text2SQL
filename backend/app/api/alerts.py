"""异常订阅接口（FR-O-1 ~ FR-O-5）。

路由分两组：

规则管理（FR-O-1）
  GET    /api/alert/rules             规则列表（可只看启用）
  POST   /api/alert/rules             新建（不带 id）或更新（带 id）
  DELETE /api/alert/rules/{id}        删除

求值与事件（FR-O-2/3/5）
  POST   /api/alert/check             立即求值（默认全部启用规则），幂等
  GET    /api/alert/events            命中 / 失败事件列表（可按已读筛选）
  POST   /api/alert/events/{id}/seen  标记已读，供未读角标归零（FR-O-4）

进程内**没有**调度器：按 FRD，周期由外部计划任务调用 /api/alert/check 提供，
应用只保证同一周期内重复调用幂等。部署前提写进 README，不用代码绕开。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.alert_eval import (
    AlertRuleError,
    build_question,
    evaluate_rule,
    quantize_eval_at,
)
from app.api.deps import Container, get_container
from app.db.repository import AlertRecord, AlertRuleRecord

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alert", tags=["alert"])

_COMPARISONS = {"gt", "lt", "deviation"}
_SEVERITIES = {"notice", "attention"}


# ----------------------------------------------------------------------
# 请求 / 响应模型
# ----------------------------------------------------------------------


class RuleBody(BaseModel):
    id: int | None = Field(default=None, description="缺省为新建；带 id 为更新")
    metric: str = Field(min_length=1, max_length=100)
    dimensions: list[str] = Field(default_factory=list, max_length=4)
    comparison: str = Field(description="gt / lt / deviation")
    threshold: float
    severity: str = "notice"
    enabled: bool = True


class CheckBody(BaseModel):
    rule_ids: list[int] | None = Field(
        default=None, description="只求值指定规则；缺省求值全部启用规则"
    )
    evaluated_at: dt.datetime | None = Field(
        default=None, description="指定求值时刻（便于补跑）；缺省取当前时间并量化"
    )


class SeenBody(BaseModel):
    seen: bool = True


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------


def _rule_dict(rule: AlertRuleRecord) -> dict[str, Any]:
    return {
        "id": rule.id,
        "metric": rule.metric,
        "dimensions": rule.dimensions,
        "comparison": rule.comparison,
        "threshold": rule.threshold,
        "severity": rule.severity,
        "enabled": rule.enabled,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _alert_dict(alert: AlertRecord, rule: AlertRuleRecord | None = None) -> dict[str, Any]:
    return {
        "id": alert.id,
        "rule_id": alert.rule_id,
        "evaluated_at": alert.evaluated_at,
        "status": alert.status,
        "hit": alert.hit,
        "metric": alert.metric,
        "hits": alert.hits,
        "value": alert.value,
        "reason": alert.reason,
        "seen": alert.seen,
        "created_at": alert.created_at,
        "severity": rule.severity if rule else None,
    }


def _validate_rule_body(container: Container, body: RuleBody) -> AlertRuleRecord:
    if body.comparison not in _COMPARISONS:
        raise HTTPException(status_code=400, detail=f"comparison 必须是 {sorted(_COMPARISONS)} 之一")
    if body.severity not in _SEVERITIES:
        raise HTTPException(status_code=400, detail=f"severity 必须是 {sorted(_SEVERITIES)} 之一")
    if body.dimensions and len(set(body.dimensions)) != len(body.dimensions):
        raise HTTPException(status_code=400, detail="dimensions 不能重复")
    record = AlertRuleRecord(
        id=body.id or 0,
        metric=body.metric.strip(),
        dimensions=[d.strip() for d in body.dimensions],
        comparison=body.comparison,
        threshold=float(body.threshold),
        severity=body.severity,
        enabled=body.enabled,
    )
    # 新建 / 更新都要过一次语义层：订阅一个不存在的指标没有意义，
    # 在配置阶段就挡住，比求值时再报 error 更可解释。
    try:
        build_question(container.catalog, record)
    except AlertRuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record


# ----------------------------------------------------------------------
# 规则管理
# ----------------------------------------------------------------------


@router.get("/rules")
def list_rules(
    enabled: bool = False,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    rules = container.store.list_alert_rules(enabled_only=enabled)
    return {"rules": [_rule_dict(r) for r in rules]}


@router.post("/rules")
def upsert_rule(
    body: RuleBody,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    if body.id is not None and container.store.get_alert_rule(body.id) is None:
        raise HTTPException(status_code=404, detail=f"规则不存在：{body.id}")
    record = _validate_rule_body(container, body)
    saved = container.store.upsert_alert_rule(record)
    return _rule_dict(saved)


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, container: Container = Depends(get_container)) -> dict[str, Any]:
    if not container.store.delete_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"规则不存在：{rule_id}")
    return {"deleted": rule_id}


# ----------------------------------------------------------------------
# 求值与事件
# ----------------------------------------------------------------------


@router.post("/check")
def check_alerts(
    body: CheckBody | None = None,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    body = body or CheckBody()
    if body.rule_ids is not None:
        rules = [container.store.get_alert_rule(rid) for rid in dict.fromkeys(body.rule_ids)]
        missing = [rid for rid, r in zip(dict.fromkeys(body.rule_ids), rules) if r is None]
        if missing:
            raise HTTPException(status_code=404, detail=f"规则不存在：{missing}")
        rules = [r for r in rules if r is not None]
    else:
        # 只跑启用规则；禁用规则的历史事件仍可查
        rules = container.store.list_alert_rules(enabled_only=True)

    if body.evaluated_at is not None:
        evaluated_at = body.evaluated_at
    else:
        evaluated_at = quantize_eval_at(
            dt.datetime.now(), container.settings.alert_eval_period_minutes
        )

    events = [evaluate_rule(container, rule, evaluated_at) for rule in rules]
    rule_cache = {r.id: r for r in rules}
    return {
        "evaluated_at": evaluated_at,
        "checked": len(events),
        "events": [_alert_dict(e, rule_cache.get(e.rule_id)) for e in events],
    }


@router.get("/events")
def list_events(
    seen: bool | None = None,
    rule_id: int | None = None,
    limit: int = 100,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    alerts = container.store.list_alerts(seen=seen, rule_id=rule_id, limit=limit)
    rules = {r.id: r for r in container.store.list_alert_rules()}
    return {
        "events": [_alert_dict(a, rules.get(a.rule_id)) for a in alerts],
        "unread": container.store.count_unseen_alerts(),
    }


@router.post("/events/{alert_id}/seen")
def mark_event_seen(
    alert_id: int,
    body: SeenBody | None = None,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    body = body or SeenBody()
    ok = container.store.mark_alert_seen(alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"事件不存在：{alert_id}")
    return {"id": alert_id, "seen": body.seen, "unread": container.store.count_unseen_alerts()}
