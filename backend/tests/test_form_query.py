"""复杂查询表单（FR-P-1 ~ FR-P-4）的校验、直填与端到端。

覆盖三件事：

1. 提交前的白名单校验——未登记的字段、算子、取值必须在进链路之前被挡下，
   且错误信息要能指出是哪一项、可用的是什么；
2. route 的表单分支——条件已确定，这条分支一次模型都不该调，槽位与问句由
   翻译层直接填；
3. 端到端——同一张图、同一份落库，表单入口与对话入口的出参结构一致。

桩模式会掩盖「条件没落进语句」这类缺陷（mock SQL 从语义层模板派生，不带
区域过滤），因此本文件的端到端断言只检查链路跑通与结构正确，不检查条件是否
落进语句——那是第三期强断言与真实模型回归的事。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.form_query import (
    build_form_payload,
    build_slots_from_form,
    render_condition_block,
    render_form_question,
)
from app.agent.nodes.generate import generate_node
from app.agent.nodes.route import route_node
from app.api.form_query import FormQueryRequest
from app.llm.schemas import SqlDraft
from app.semantic.filters import validate_form_query
from app.semantic.loader import SEMANTIC_DIR, Catalog, SemanticError, load_catalog, recall

# 与方案 §5.9 的走查用例一致：多条件 AND + IN 多值 + 区间 + TopN
BASE_PAYLOAD: dict[str, Any] = {
    "session_id": "s-001",
    "metric": "gmv",
    "dimensions": ["category_name"],
    "conditions": [
        {"field": "orders.region", "op": "in", "value": ["华东", "华南"]},
        {"field": "orders.channel", "op": "eq", "value": "app"},
        {
            "field": "orders.created_at",
            "op": "between",
            "value": "2026-01-01",
            "value_to": "2026-07-01",
        },
    ],
    "top_n": 10,
}


def _payload(**overrides: Any) -> dict[str, Any]:
    data = {**BASE_PAYLOAD, "conditions": [dict(c) for c in BASE_PAYLOAD["conditions"]]}
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# 一、提交前校验
# ---------------------------------------------------------------------------


def test_valid_payload_passes(catalog: Catalog) -> None:
    assert validate_form_query(BASE_PAYLOAD, catalog) == []


def test_request_model_accepts_base_payload() -> None:
    """请求模型的字段形态与校验器一致，避免两层各说一套。"""
    req = FormQueryRequest(**BASE_PAYLOAD)
    assert req.metric == "gmv"
    assert req.conditions[0].op == "in"


@pytest.mark.parametrize(
    "field,expected",
    [
        ("orders.coupon_id", "未登记为可用条件"),
        ("region", "未登记为可用条件"),
    ],
)
def test_unregistered_field_is_rejected(
    catalog: Catalog, field: str, expected: str
) -> None:
    payload = _payload(conditions=[{"field": field, "op": "eq", "value": "x"}])
    problems = validate_form_query(payload, catalog)
    assert any(expected in p for p in problems)
    # 错误信息必须带上可用字段，否则用户不知道该改成什么
    assert any("orders.region" in p for p in problems)


def test_unsupported_operator_is_rejected(catalog: Catalog) -> None:
    payload = _payload(
        conditions=[{"field": "orders.region", "op": "between", "value": "2026-01-01", "value_to": "2026-02-01"}]
    )
    problems = validate_form_query(payload, catalog)
    assert any("不支持算子 between" in p for p in problems)


def test_unknown_enum_value_is_rejected(catalog: Catalog) -> None:
    payload = _payload(conditions=[{"field": "orders.region", "op": "eq", "value": "火星"}])
    problems = validate_form_query(payload, catalog)
    assert any("没有取值「火星」" in p for p in problems)


def test_between_requires_both_bounds(catalog: Catalog) -> None:
    payload = _payload(
        conditions=[{"field": "orders.created_at", "op": "between", "value": "2026-01-01"}]
    )
    problems = validate_form_query(payload, catalog)
    assert any("缺少结束日期" in p for p in problems)


def test_reversed_interval_is_rejected(catalog: Catalog) -> None:
    payload = _payload(
        conditions=[
            {
                "field": "orders.created_at",
                "op": "between",
                "value": "2026-07-01",
                "value_to": "2026-01-01",
            }
        ]
    )
    problems = validate_form_query(payload, catalog)
    assert any("不早于" in p for p in problems)


def test_bad_date_format_is_rejected(catalog: Catalog) -> None:
    payload = _payload(conditions=[{"field": "orders.created_at", "op": "gte", "value": "2026/01/01"}])
    problems = validate_form_query(payload, catalog)
    assert any("需要日期取值" in p for p in problems)


def test_dimension_outside_metric_is_rejected(catalog: Catalog) -> None:
    """退款原因不是 gmv 声明的维度，按它分组会得到无意义的分母。"""
    payload = _payload(dimensions=["refund_reason"])
    problems = validate_form_query(payload, catalog)
    assert any("不支持按" in p for p in problems)


def test_unknown_metric_is_rejected(catalog: Catalog) -> None:
    problems = validate_form_query(_payload(metric="净利润"), catalog)
    assert any("未在语义层登记" in p for p in problems)


def test_top_n_and_order_by_are_checked(catalog: Catalog) -> None:
    assert any("top_n" in p for p in validate_form_query(_payload(top_n=5000), catalog))
    assert any("direction" in p for p in validate_form_query(_payload(order_by={"field": "成交总额", "direction": "乱序"}), catalog))


def test_too_many_conditions_are_rejected(catalog: Catalog) -> None:
    conds = [{"field": "orders.region", "op": "eq", "value": "华东"}] * 13
    problems = validate_form_query(_payload(conditions=conds), catalog)
    assert any("超过上限" in p for p in problems)


# ---------------------------------------------------------------------------
# 二、条件解析与渲染
# ---------------------------------------------------------------------------


def test_conditions_are_resolved_to_standard_values(catalog: Catalog) -> None:
    """显示值（金卡）与库内值（gold）都能提交，落进条件的是库内值。"""
    payload = _payload(conditions=[{"field": "users.level", "op": "in", "value": ["金卡", "白金卡"]}])
    form = build_form_payload(payload, catalog)
    conds = form["form_conditions"]
    assert len(conds) == 1
    assert conds[0]["values"] == ["gold", "platinum"]
    assert conds[0]["join"] == "users.id = orders.user_id"


def test_time_range_merges_into_conditions(catalog: Catalog) -> None:
    payload = _payload(conditions=[], time_range={"start": "2026-01-01", "end": "2026-07-01"})
    conds = build_form_payload(payload, catalog)["form_conditions"]
    assert len(conds) == 1
    assert conds[0]["field"] == "orders.created_at"
    assert (conds[0]["start"], conds[0]["end"]) == ("2026-01-01", "2026-07-01")


def test_duplicated_time_range_is_not_appended_twice(catalog: Catalog) -> None:
    payload = _payload(time_range={"start": "2026-01-01", "end": "2026-07-01"})
    conds = build_form_payload(payload, catalog)["form_conditions"]
    assert len(conds) == 3


def test_predicate_and_block_rendering(catalog: Catalog) -> None:
    payload = _payload()
    form = build_form_payload(payload, catalog)
    block = render_condition_block(form["form_conditions"], catalog, form["form_query"])
    assert "必须满足的查询条件" in block
    assert "orders.region IN ('华东', '华南')" in block
    assert "orders.channel = 'app'" in block
    assert "orders.created_at >= '2026-01-01' AND orders.created_at < '2026-07-01'" in block
    # 分组维度一并给出，避免模型自选顺序（F6）
    assert "商品类目" in block


def test_condition_block_is_empty_for_chat_path(catalog: Catalog) -> None:
    """自由提问路径不该出现条件节：模型自己抽条件，再看一遍清单会改坏。"""
    assert render_condition_block([], catalog, None) == ""


def test_join_hint_is_rendered_for_cross_table_condition(catalog: Catalog) -> None:
    payload = _payload(conditions=[{"field": "users.level", "op": "eq", "value": "gold"}])
    form = build_form_payload(payload, catalog)
    block = render_condition_block(form["form_conditions"], catalog, form["form_query"])
    assert "users.id = orders.user_id" in block


# ---------------------------------------------------------------------------
# 三、route 表单分支
# ---------------------------------------------------------------------------


class _NeverCalledGateway:
    """表单分支一次模型都不该调。被调用即抛错，比事后统计次数更直接。"""

    @property
    def mock(self) -> bool:
        return True

    def complete_json(self, prompt: str, schema: type, tag: str | None = None) -> Any:
        raise AssertionError(f"表单路径不应调用模型：{schema}")


def test_route_form_branch_fills_slots_without_llm(catalog: Catalog) -> None:
    form = build_form_payload(BASE_PAYLOAD, catalog)
    state: dict[str, Any] = {"query_source": "form", **form}
    update = route_node(state, gateway=_NeverCalledGateway(), catalog=catalog)  # type: ignore[arg-type]

    assert update["intent"] == "query"
    slots = update["slots"]
    assert slots["metrics"] == ["成交总额"]
    assert slots["dimensions"] == ["商品类目"]
    assert slots["top_n"] == 10
    assert slots["time_range"] == "2026-01-01 至 2026-07-01"
    # 过滤条件进 filters，全量取值留在 form_conditions 里供强断言用
    assert {"field": "销售大区", "value": "华东"} in slots["filters"]
    assert update["question"] == update["rewritten_question"]
    assert "表单条件" in update["stages"][0]["detail"]


def test_rendered_question_recalls_the_metric(catalog: Catalog) -> None:
    """渲染问句要能被召回认出指标，否则口径标注是空的。"""
    form = build_form_payload(BASE_PAYLOAD, catalog)
    question = render_form_question(form["form_query"], form["form_conditions"], catalog)
    hit = recall(question, catalog)
    assert [m["name"] for m in hit.metrics] == ["gmv"]


def test_generate_prompt_carries_condition_list(catalog: Catalog) -> None:
    form = build_form_payload(BASE_PAYLOAD, catalog)
    captured: list[str] = []

    class _CaptureGateway:
        settings = SimpleNamespace(sql_dialect="sqlite")

        def complete_json(self, prompt: str, schema: type, tag: str | None = None) -> Any:
            captured.append(prompt)
            return SqlDraft(sql="SELECT 1 AS 占位", reasoning="", metric_used=["成交总额"])

    state: dict[str, Any] = {
        "query_source": "form",
        "question": "占位问句",
        "messages": [],
        "retrieved": {"context": "上下文"},
        **form,
    }
    generate_node(state, gateway=_CaptureGateway(), catalog=catalog)  # type: ignore[arg-type]
    assert captured
    assert "orders.region IN ('华东', '华南')" in captured[0]


# ---------------------------------------------------------------------------
# 四、加载期校验
# ---------------------------------------------------------------------------


def _copy_semantic_dir(tmp_path: Path) -> Path:
    target = tmp_path / "semantic"
    target.mkdir()
    for path in SEMANTIC_DIR.glob("*.yaml"):
        shutil.copy(path, target / path.name)
    return target


def test_bad_join_hint_fails_at_load(tmp_path: Path) -> None:
    base = _copy_semantic_dir(tmp_path)
    (base / "filters.yaml").write_text(
        "filter_fields:\n"
        "  - field: users.level\n"
        "    display: 会员等级\n"
        "    ops: [eq]\n"
        "    values_from: enum\n"
        "    join: users.id = orders.nonexistent\n",
        encoding="utf-8",
    )
    with pytest.raises(SemanticError) as exc:
        load_catalog(base)
    assert "join 指向不存在的字段" in str(exc.value)


def test_enum_source_without_enum_fails_at_load(tmp_path: Path) -> None:
    base = _copy_semantic_dir(tmp_path)
    (base / "filters.yaml").write_text(
        "filter_fields:\n"
        "  - field: orders.created_at\n"
        "    display: 下单时间\n"
        "    ops: [eq]\n"
        "    values_from: enum\n",
        encoding="utf-8",
    )
    with pytest.raises(SemanticError) as exc:
        load_catalog(base)
    assert "没有 enum" in str(exc.value)


def test_registered_fields_are_loaded(catalog: Catalog) -> None:
    assert "orders.region" in catalog.filter_by_name
    assert catalog.filter_by_name["orders.created_at"]["ops"] == ["between", "gte", "lte"]


# ---------------------------------------------------------------------------
# 五、端到端
# ---------------------------------------------------------------------------


def test_form_query_end_to_end(client: Any, require_mock: None) -> None:
    sid = client.post("/api/session", json={"title": "表单用例"}).json()["id"]
    resp = client.post("/api/query/form", json={**BASE_PAYLOAD, "session_id": sid})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["query_source"] == "form"
    assert data["slots"]["metrics"] == ["成交总额"]
    assert data["row_count"] >= 1
    assert len(data["form_conditions"]) == 3
    # 落库与回看用的问句来自渲染，不是用户原话
    assert "成交总额" in data["original_question"]
    assert data["turn_id"]


def test_form_query_rejects_unregistered_field(client: Any, require_mock: None) -> None:
    sid = client.post("/api/session", json={"title": "表单用例"}).json()["id"]
    payload = _payload(session_id=sid, conditions=[{"field": "orders.coupon_id", "op": "eq", "value": "x"}])
    resp = client.post("/api/query/form", json=payload)
    assert resp.status_code == 400
    assert "orders.coupon_id" in resp.json()["detail"]["errors"][0]


def test_form_query_requires_existing_session(client: Any, require_mock: None) -> None:
    resp = client.post("/api/query/form", json={**BASE_PAYLOAD, "session_id": "not-exist"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 六、互斥条件与提交前预演
# ---------------------------------------------------------------------------


def test_exclusive_conditions_are_rejected(catalog: Catalog) -> None:
    """订单状态与退款原因不同时成立，组合后结果恒为空集。"""
    payload = _payload(
        conditions=[
            {"field": "orders.status", "op": "eq", "value": "paid"},
            {"field": "refunds.reason", "op": "eq", "value": "质量问题"},
        ]
    )
    problems = validate_form_query(payload, catalog)
    assert any("不能同时使用" in p for p in problems)
    assert any("订单状态" in p and "退款原因" in p for p in problems)


def test_one_sided_exclusion_fails_at_load(tmp_path: Path) -> None:
    """只在一侧写 exclude_with 会漏掉一半组合，加载期就应报错。"""
    base = _copy_semantic_dir(tmp_path)
    (base / "filters.yaml").write_text(
        "filter_fields:\n"
        "  - field: orders.region\n"
        "    display: 销售大区\n"
        "    ops: [eq]\n"
        "    values_from: enum\n"
        "    exclude_with: [orders.channel]\n"
        "  - field: orders.channel\n"
        "    display: 下单渠道\n"
        "    ops: [eq]\n"
        "    values_from: enum\n",
        encoding="utf-8",
    )
    with pytest.raises(SemanticError) as exc:
        load_catalog(base)
    assert "只登记了一侧" in str(exc.value)


def test_exclusion_to_unregistered_field_fails_at_load(tmp_path: Path) -> None:
    base = _copy_semantic_dir(tmp_path)
    (base / "filters.yaml").write_text(
        "filter_fields:\n"
        "  - field: orders.region\n"
        "    display: 销售大区\n"
        "    ops: [eq]\n"
        "    values_from: enum\n"
        "    exclude_with: [orders.nope]\n",
        encoding="utf-8",
    )
    with pytest.raises(SemanticError) as exc:
        load_catalog(base)
    assert "未登记的字段" in str(exc.value)


def test_filters_endpoint_carries_values_and_exclusions(client: Any) -> None:
    """表单的取值下拉直接取这份数据，不在前端另写一份取值体系。"""
    items = client.get("/api/semantic/filters").json()["items"]
    by_field = {i["field"]: i for i in items}
    assert "orders.region" in by_field
    assert len(by_field["orders.region"]["values"]) >= 2
    # 日期字段没有 enum，取值留给日期选择器
    assert by_field["orders.created_at"]["values"] == []
    assert "refunds.reason" in by_field["orders.status"]["exclude_with"]


def test_duplicate_field_is_rejected(catalog: Catalog) -> None:
    """同一字段加两行是「且」的关系，恒为空集；多值应当用 in。"""
    payload = _payload(
        conditions=[
            {"field": "orders.region", "op": "eq", "value": "华东"},
            {"field": "orders.region", "op": "eq", "value": "华南"},
        ]
    )
    problems = validate_form_query(payload, catalog)
    assert any("出现了 2 次" in p for p in problems)
    assert any("in" in p for p in problems)


def test_time_range_conflicts_with_time_condition(catalog: Catalog) -> None:
    """顶部区间与条件行里的下单时间是同一个字段，同时给会互相矛盾。"""
    payload = _payload(
        conditions=[
            {"field": "orders.created_at", "op": "gte", "value": "2026-02-01"}
        ],
        time_range={"start": "2026-01-01", "end": "2026-03-01"},
    )
    problems = validate_form_query(payload, catalog)
    assert any("二选一" in p for p in problems)


def test_preview_renders_question_and_predicates(client: Any) -> None:
    resp = client.post("/api/query/form/preview", json=_payload())
    assert resp.status_code == 200
    data = resp.json()
    assert "成交总额" in data["question"]
    assert "orders.region IN ('华东', '华南')" in data["predicates"]
    # 区间渲染成一条谓词，起止两个边界都要在，左闭右开
    assert any(
        "orders.created_at >= '2026-01-01'" in p and "orders.created_at < '2026-07-01'" in p
        for p in data["predicates"]
    )
    # 谓词逐条对应条件描述，回显时两项一起呈现
    assert len(data["descriptions"]) == len(data["predicates"])


def test_preview_rejects_bad_payload(client: Any) -> None:
    resp = client.post(
        "/api/query/form/preview",
        json=_payload(conditions=[{"field": "orders.coupon_id", "op": "eq", "value": "x"}]),
    )
    assert resp.status_code == 400
    assert "orders.coupon_id" in resp.json()["detail"]["errors"][0]

# ---------------------------------------------------------------------------
# 七、失败归因的接口出参
# ---------------------------------------------------------------------------


def test_success_turn_has_no_attribution(client: Any, require_mock: None) -> None:
    """成功轮不产归因。

    多返回一个空对象等于让每个消费方（前端两处入口、历史回看）都得多处理
    一种空态，而起因只是「这一轮没失败」。
    """
    sid = client.post("/api/session", json={"title": "归因用例"}).json()["id"]
    resp = client.post("/api/query/form", json={**BASE_PAYLOAD, "session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["failure_attribution"] is None


def test_failed_turn_carries_attribution(
    client: Any, require_mock: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败轮的接口出参带归因，编码与 eval 报表同源。

    注入「成交总额写成销量」的语句：守卫放行（表名列名都登记过），条件核对
    无从谈起（根本没走到那一步），只有口径签名能抓到它。归到 signature 而不
    是笼统的失败，界面才知道该补指标定义而不是去改问句。
    """
    from app.api.deps import get_container

    container = get_container()
    original = container.gateway.complete_json

    def patched(prompt: str, schema: type, **kwargs: Any):
        if schema is SqlDraft:
            return SqlDraft(
                sql=(
                    "SELECT o.region AS 区域, SUM(oi.quantity) AS 成交总额"
                    " FROM orders o JOIN order_items oi ON oi.order_id = o.id"
                    " GROUP BY 区域"
                ),
                reasoning="注入的语句",
                metric_used=[],
            )
        return original(prompt, schema, **kwargs)

    monkeypatch.setattr(container.gateway, "complete_json", patched)
    sid = client.post("/api/session", json={"title": "归因用例"}).json()["id"]
    resp = client.post("/api/query/form", json={**BASE_PAYLOAD, "session_id": sid})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    attribution = data["failure_attribution"]
    assert attribution["kind"] == "signature"
    assert attribution["label"] == "口径签名不符"
    assert "metrics.yaml" in attribution["suggestion"]
