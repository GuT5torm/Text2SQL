"""失败归因的单测。

锁住三件事：

1. 四类各自能命中，且判据按证据强弱排队——slot 的证据最硬（缺口类型写在
   结果里），guard 靠 R 编号，signature 只有文案关键字，都不是才落 generate。
   顺序错了不会报错，只会把「该补提示词」说成「该换模型」，报表上照样有数字，
   于是按错的结论去改。
2. 成功轮不产归因。多返回一个空对象等于让每个消费方多处理一种空态。
3. 运行时与评测同一套编码：eval/run_eval.py 的 `_attribute` 必须是这里的
   一层薄包装，两边判定漂移过一次就没法对照界面与报表了。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

from app.agent.failure_attribution import (  # noqa: E402
    ATTRIBUTIONS,
    GENERATE,
    GUARD,
    LABELS,
    SIGNATURE,
    SLOT,
    SUGGESTIONS,
    attribute_failure,
    describe_attribution,
)
from app.agent.graph import summarize_result  # noqa: E402
from app.agent.slot_coverage import SlotGap  # noqa: E402


def _result(**overrides: object) -> dict:
    base: dict = {"status": "error", "slot_gaps": [], "sql_error": ""}
    base.update(overrides)
    return base


def test_guard_by_rule_number():
    got = attribute_failure(
        _result(sql_error="R4 表 secret_ledger 未在语义层登记")
    )
    assert got["kind"] == GUARD
    assert got["label"] == "守卫拦下"


def test_guard_by_keyword_without_rule_number():
    # 拦截文案里未必带 R 编号（改写类拒绝），「守卫」二字也是判据
    assert attribute_failure(_result(sql_error="守卫拒绝执行该语句"))["kind"] == GUARD


def test_signature_by_keyword():
    got = attribute_failure(
        _result(sql_error="成交总额的口径不成立：语句中未出现 ['unit_price']")
    )
    assert got["kind"] == SIGNATURE
    assert "metrics.yaml" in got["suggestion"]


def test_slot_by_gap_kind():
    # 缺口类型比文案硬：空文案也要能归到 slot
    got = attribute_failure(
        _result(slot_gaps=[{"kind": "form_condition", "expected": "下单渠道", "detail": ""}])
    )
    assert got["kind"] == SLOT
    assert got["evidence"]


def test_slot_by_error_text():
    got = attribute_failure(_result(sql_error="表单条件未全部落到语句：下单渠道"))
    assert got["kind"] == SLOT


def test_slot_wins_over_guard():
    # 强断言失败时 error 里可能带 R 编号（守卫先报过一次），slot 的证据更硬
    got = attribute_failure(
        _result(
            slot_gaps=[{"kind": "form_condition", "expected": "下单渠道", "detail": ""}],
            sql_error="R4 表 xxx 未登记",
        )
    )
    assert got["kind"] == SLOT


def test_generate_is_fallback():
    got = attribute_failure(_result(sql_error="连接超时"))
    assert got["kind"] == GENERATE
    assert got["evidence"] == "连接超时"


def test_non_error_status_has_no_attribution():
    assert attribute_failure({"status": "ok", "sql_error": ""}) is None
    assert attribute_failure({"status": "clarify"}) is None
    assert attribute_failure({}) is None
    assert attribute_failure(None) is None


def test_slot_gap_objects_are_accepted():
    # 图终态里的 slot_gaps 可能是对象，解析不出来会静默退到 generate
    gaps = [SlotGap(kind="form_condition", expected="销售大区", detail="语句里没有这个取值")]
    assert attribute_failure(_result(slot_gaps=gaps))["kind"] == SLOT


def test_describe_unknown_kind_falls_back():
    assert describe_attribution("nope")["kind"] == GENERATE
    assert set(LABELS) == set(ATTRIBUTIONS)
    assert set(SUGGESTIONS) == set(ATTRIBUTIONS)


def test_summarize_result_exposes_attribution():
    out = summarize_result(
        {
            "status": "error",
            "sql_error": "R4 表 secret_ledger 未在语义层登记",
            "slot_gaps": [],
        }
    )
    assert out["failure_attribution"]["kind"] == GUARD

    ok = summarize_result({"status": "ok", "rows": [], "columns": []})
    assert ok["failure_attribution"] is None


def test_eval_shares_the_same_encoding():
    """评测的归因必须是同一段逻辑，否则界面与报表会对同一轮给出两种说法。"""
    from eval import run_eval  # noqa: PLC0415

    sample = _result(sql_error="成交总额的口径不成立：缺少 unit_price")
    assert run_eval._attribute(sample) == attribute_failure(sample)["kind"]
    assert run_eval._attribute({"status": "ok"}) == "none"
    assert run_eval.ATTRIBUTIONS == ATTRIBUTIONS


@pytest.mark.parametrize("kind", ATTRIBUTIONS)
def test_every_kind_has_label_and_suggestion(kind: str):
    assert LABELS[kind]
    assert SUGGESTIONS[kind]
