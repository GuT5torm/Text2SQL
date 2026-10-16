"""FR-O-1 ~ FR-O-5 异常订阅：规则 API、求值分流、幂等与未读计数。"""

from __future__ import annotations

import datetime as dt

import pytest

from app.agent import alert_eval as alert_eval_module
from app.agent.alert_eval import DEFAULT_PERIOD_TEXT, build_question, evaluate_rule, quantize_eval_at
from app.config import get_settings
from app.db.repository import AlertRuleRecord


@pytest.fixture()
def alert_container(isolated_app_db):
    """与 client 夹具同源的临时容器，但不启 HTTP，求值测试直接调函数。"""
    from app.api.deps import build_container, reset_container

    if not get_settings().business_db_path.exists():
        pytest.skip("业务库不存在，请先运行 python data/seed.py")
    reset_container()
    container = build_container()
    yield container
    reset_container()


EVAL_AT = dt.datetime(2026, 9, 15, 10, 0, 0)


def _add_rule(container, **kwargs: object) -> AlertRuleRecord:
    payload = dict(
        metric="refund_rate",
        dimensions=["region"],
        comparison="gt",
        threshold=0,
        severity="attention",
    )
    payload.update(kwargs)
    return container.store.upsert_alert_rule(AlertRuleRecord(**payload))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FR-O-1 规则管理 API
# ---------------------------------------------------------------------------


def test_rule_crud_api(client) -> None:
    body = {
        "metric": "refund_rate",
        "dimensions": ["region"],
        "comparison": "gt",
        "threshold": 0.1,
        "severity": "attention",
    }
    resp = client.post("/api/alert/rules", json=body)
    assert resp.status_code == 200, resp.text
    rule_id = resp.json()["id"]

    listing = client.get("/api/alert/rules").json()["rules"]
    assert any(r["id"] == rule_id for r in listing)

    resp = client.post("/api/alert/rules", json={**body, "id": rule_id, "threshold": 0.2})
    assert resp.status_code == 200
    assert resp.json()["threshold"] == 0.2

    assert client.delete(f"/api/alert/rules/{rule_id}").status_code == 200
    assert client.delete(f"/api/alert/rules/{rule_id}").status_code == 404


def test_rule_rejects_unknown_metric(client) -> None:
    resp = client.post(
        "/api/alert/rules",
        json={"metric": "not_a_metric", "dimensions": [], "comparison": "gt", "threshold": 1},
    )
    assert resp.status_code == 400


def test_rule_rejects_bad_comparison(client) -> None:
    resp = client.post(
        "/api/alert/rules",
        json={"metric": "gmv", "dimensions": [], "comparison": "eq", "threshold": 1},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# FR-O-2 / O-5：命中、未命中、失败三态
# ---------------------------------------------------------------------------


def test_evaluation_hit_records_slices(alert_container) -> None:
    rule = _add_rule(alert_container, threshold=0)
    alert = evaluate_rule(alert_container, rule, EVAL_AT)
    assert alert.status == "ok"
    assert alert.hit is True
    assert alert.hits and alert.metric == "refund_rate"
    first = alert.hits[0]
    assert first["slice"] and "销售大区" in first["slice"]
    assert isinstance(first["value"], float)
    assert first["question"].endswith("退款率")


def test_evaluation_miss_is_ok_not_error(alert_container) -> None:
    rule = _add_rule(alert_container, threshold=999999)
    alert = evaluate_rule(alert_container, rule, EVAL_AT)
    # FR-O-5：没越线是「未命中」，绝不能写成 error
    assert alert.status == "ok"
    assert alert.hit is False
    assert alert.hits == []


def test_build_question_carries_default_period(alert_container) -> None:
    """整体口径的合成问句原本只有指标名，缺时间范围会被 route 判成 clarify。

    这也是那次「桩模式全绿、切真实模型后订阅连续失败」的根因，用一条语句
    级别的断言把两个分支都钉住。
    """
    overall = AlertRuleRecord(metric="gmv", dimensions=[], comparison="deviation", threshold=5)
    question, metric_display, dim_displays = build_question(alert_container.catalog, overall)
    assert question == f"{DEFAULT_PERIOD_TEXT}的成交总额"
    assert metric_display == "成交总额"
    assert dim_displays == []

    grouped = AlertRuleRecord(metric="gmv", dimensions=["category_name"], comparison="gt", threshold=1)
    question, _, dim_displays = build_question(alert_container.catalog, grouped)
    assert question == f"{DEFAULT_PERIOD_TEXT}各商品类目的成交总额"
    assert len(dim_displays) == 1


def test_evaluation_failure_when_slice_baseline_unavailable(alert_container) -> None:
    rule = _add_rule(
        alert_container,
        metric="gmv",
        dimensions=["region"],
        comparison="deviation",
        threshold=5,
    )
    alert = evaluate_rule(alert_container, rule, EVAL_AT)
    # 分组结果没有逐切片基准，不能假装算得出偏离度：这属于 error 不是未命中
    assert alert.status == "error"
    assert "切片" in alert.reason


def test_evaluation_failure_when_baseline_unusable(alert_container, monkeypatch) -> None:
    """偏离度拿不到可比基准时必须记 error。

    缺_None 分支在桩模式下已经走不到（补了默认时间窗后 mock SQL 也能给出
    基准），所以这里直接注入返回值，专测错误映射而不再依赖偶然的查询形态。
    """
    monkeypatch.setattr(
        alert_eval_module,
        "_baseline_deviation",
        lambda result: (None, "注入：无对比基准"),
    )
    rule = _add_rule(
        alert_container,
        metric="gmv",
        dimensions=[],
        comparison="deviation",
        threshold=5,
    )
    alert = evaluate_rule(alert_container, rule, EVAL_AT)
    # FR-O-5：基准不成立是失败，绝不能写成「未命中」
    assert alert.status == "error"
    assert alert.hit is False
    assert "注入" in alert.reason


def test_unknown_metric_rule_evaluates_to_error(alert_container) -> None:
    rule = AlertRuleRecord(metric="ghost_metric", comparison="gt", threshold=1, id=1)
    alert = evaluate_rule(alert_container, rule, EVAL_AT)
    # rule_id=1 在临时库不存在，行会写不进去（外键）——此时不应抛异常
    assert alert.status == "error"


# ---------------------------------------------------------------------------
# FR-O-3 幂等
# ---------------------------------------------------------------------------


def test_same_period_evaluation_is_idempotent(alert_container) -> None:
    rule = _add_rule(alert_container, threshold=0)
    first = evaluate_rule(alert_container, rule, EVAL_AT)
    second = evaluate_rule(alert_container, rule, EVAL_AT)
    assert first.id == second.id
    assert first.hits == second.hits

    events = alert_container.store.list_alerts(rule_id=rule.id)
    assert len(events) == 1


def test_different_periods_create_separate_rows(alert_container) -> None:
    rule = _add_rule(alert_container, threshold=0)
    first = evaluate_rule(alert_container, rule, EVAL_AT)
    second = evaluate_rule(alert_container, rule, EVAL_AT + dt.timedelta(hours=1))
    assert first.id != second.id


def test_quantize_floors_to_period() -> None:
    moment = dt.datetime(2026, 9, 15, 10, 47, 23)
    assert quantize_eval_at(moment, 60) == dt.datetime(2026, 9, 15, 10, 0, 0)
    assert quantize_eval_at(moment, 15) == dt.datetime(2026, 9, 15, 10, 45, 0)


# ---------------------------------------------------------------------------
# FR-O-4 未读计数与健康检查
# ---------------------------------------------------------------------------


def test_unseen_count_and_mark_seen(alert_container) -> None:
    rule = _add_rule(alert_container, threshold=0)
    alert = evaluate_rule(alert_container, rule, EVAL_AT)
    assert alert_container.store.count_unseen_alerts() >= 1
    assert alert_container.store.latest_alert_eval() is not None

    assert alert_container.store.mark_alert_seen(alert.id) is True
    assert alert_container.store.list_alerts(seen=True)
    assert all(a.seen for a in alert_container.store.list_alerts(seen=True))
    assert alert_container.store.mark_alert_seen(999999) is False


def test_consecutive_failures_counter(alert_container) -> None:
    # 连续两条失败（偏离度订阅不支持切片，天然走 error），不同周期
    rule = _add_rule(
        alert_container,
        metric="gmv",
        dimensions=["region"],
        comparison="deviation",
        threshold=5,
    )
    evaluate_rule(alert_container, rule, EVAL_AT)
    evaluate_rule(alert_container, rule, EVAL_AT + dt.timedelta(hours=1))
    assert alert_container.store.consecutive_alert_failures() == 2

    # 再来一条时刻更晚的成功记录，连续失败计数归零
    ok_rule = _add_rule(alert_container, threshold=999999)
    evaluate_rule(alert_container, ok_rule, EVAL_AT + dt.timedelta(hours=2))
    assert alert_container.store.consecutive_alert_failures() == 0


def test_check_endpoint_idempotent_and_reports_events(client) -> None:
    rule_id = client.post(
        "/api/alert/rules",
        json={
            "metric": "refund_rate",
            "dimensions": ["region"],
            "comparison": "gt",
            "threshold": 999999,
            "severity": "notice",
        },
    ).json()["id"]

    payload = {"rule_ids": [rule_id], "evaluated_at": "2026-09-15T10:00:00"}
    first = client.post("/api/alert/check", json=payload)
    second = client.post("/api/alert/check", json=payload)
    assert first.status_code == second.status_code == 200
    e1 = first.json()["events"][0]
    e2 = second.json()["events"][0]
    assert e1["id"] == e2["id"]
    assert e1["status"] == "ok" and e1["hit"] is False
    assert e1["severity"] == "notice"

    events = client.get(f"/api/alert/events?rule_id={rule_id}").json()
    assert events["unread"] >= 1
    assert any(e["id"] == e1["id"] for e in events["events"])

    marked = client.post(f"/api/alert/events/{e1['id']}/seen").json()
    assert marked["seen"] is True


def test_health_reports_alert_status(client) -> None:
    health = client.get("/api/health").json()
    for key in (
        "alert_rules",
        "alert_last_eval_at",
        "alert_consecutive_failures",
        "alert_failures_threshold",
        "alert_unhealthy",
        "alert_unread",
        "data_as_of",
    ):
        assert key in health
    assert health["checkpointer"] in {"memory", "sqlite"}
