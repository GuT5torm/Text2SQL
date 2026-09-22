"""FR-F-1 空结果回灌与 FR-F-2 修正痕迹单测。

FR-F-1 最需要锁住的是「两类失败不共享配额」：空结果回灌若占用 retry_count，
原本能靠三次修正解决的报错会只修两次就放弃。另一条是回灌必须允许「确认无数据」
这个答案——不允许的话，系统为了凑出行数会把条件放宽到失真。

FR-F-2 锁住的是「修正过的轮次结论里有交代」，以及这件事不能反过来触发
FR-B-3 的误报：「经过 1 次修正」里的 1 是服务端事实，不是模型编的数字。
"""

from __future__ import annotations

from typing import Any

from app.agent.consistency import check_conclusion_numbers
from app.agent.graph import _after_execute, _after_fix
from app.agent.nodes.fix import fix_node
from app.agent.nodes.summarize import summarize_node
from app.llm.schemas import Insight, SqlDraft

ROUTE_STATE: dict[str, Any] = {"intent": "query"}


class FakeGateway:
    """只实现 complete_json 的替身。按 schema 类型返回预设输出。"""

    def __init__(self, draft: SqlDraft | None = None, insight: Insight | None = None) -> None:
        self._draft = draft
        self._insight = insight
        self.prompts: list[str] = []

    @property
    def mock(self) -> bool:
        return True

    def complete_json(self, prompt: str, schema: type, tag: str | None = None) -> Any:
        self.prompts.append(prompt)
        if schema is SqlDraft:
            return self._draft
        return self._insight


# ----------------------------------------------------------------------
# FR-F-1 路由
# ----------------------------------------------------------------------


def test_empty_result_goes_to_fix_within_quota() -> None:
    state = {"empty_result": True, "empty_retry": 0}
    assert _after_execute(state, max_retry=3, max_empty=1) == "fix"


def test_empty_result_quota_exhausted_falls_through() -> None:
    state = {"empty_result": True, "empty_retry": 1}
    assert _after_execute(state, max_retry=3, max_empty=1) == "visualize"


def test_empty_retry_disabled_by_config() -> None:
    # 配额配成 0 时空结果不再回灌，与旧行为一致
    state = {"empty_result": True, "empty_retry": 0}
    assert _after_execute(state, max_retry=3, max_empty=0) == "visualize"


def test_sql_error_routing_unaffected() -> None:
    state = {"sql_error": "no such column: x", "retry_count": 0}
    assert _after_execute(state, max_retry=3, max_empty=1) == "fix"


def test_confirmed_no_data_does_not_retry_again() -> None:
    state = {"empty_result": True, "no_data_confirmed": True, "empty_retry": 0}
    assert _after_execute(state, max_retry=3, max_empty=1) == "visualize"


def test_after_fix_routes_confirm_no_data_to_summarize() -> None:
    assert _after_fix({"no_data_confirmed": True}) == "summarize"
    assert _after_fix({"no_data_confirmed": False}) == "validate"
    assert _after_fix({}) == "validate"


# ----------------------------------------------------------------------
# FR-F-1 回灌节点
# ----------------------------------------------------------------------


def test_relax_rewrite_does_not_consume_retry_count() -> None:
    draft = SqlDraft(sql="SELECT region FROM orders", reasoning="把区间放宽到近三个月")
    gateway = FakeGateway(draft=draft)
    out = fix_node(
        {"empty_result": True, "retry_count": 0, "empty_retry": 0, "sql": "SELECT 1"},
        gateway=gateway,
    )
    assert out["empty_retry"] == 1
    assert out["empty_result"] is False
    # 关键：报错修正的配额一点没动
    assert "retry_count" not in out
    assert out["sql"] == "SELECT region FROM orders"


def test_confirm_no_data_sets_flag() -> None:
    draft = SqlDraft(confirm_no_data=True, sql="", reasoning="该区间内订单表无记录")
    gateway = FakeGateway(draft=draft)
    out = fix_node(
        {"empty_result": True, "retry_count": 0, "empty_retry": 0, "sql": "SELECT 1"},
        gateway=gateway,
    )
    assert out["no_data_confirmed"] is True
    assert out["empty_retry"] == 1
    assert "sql" not in out


def test_empty_prompt_mentions_hard_boundaries() -> None:
    """回灌 prompt 必须写明四条边界，否则模型会为了凑行数越界。"""
    gateway = FakeGateway(draft=SqlDraft(sql="SELECT 1", reasoning="x"))
    fix_node({"empty_result": True, "empty_retry": 0, "sql": "SELECT 1"}, gateway=gateway)
    prompt = gateway.prompts[0]
    for phrase in ("不得移除时间过滤", "不得移除维度过滤", "不得跨入未走完的周期", "只放宽一次"):
        assert phrase in prompt


def test_relax_failure_still_consumes_quota() -> None:
    class BoomGateway:
        @property
        def mock(self) -> bool:
            return True

        def complete_json(self, prompt: str, schema: type, tag: str | None = None) -> Any:
            from app.llm.gateway import LLMError

            raise LLMError("网关不可用")

    out = fix_node({"empty_result": True, "empty_retry": 0, "sql": "SELECT 1"}, gateway=BoomGateway())
    # 回灌自身失败也要计数，否则链路会在 fix 与 execute 之间空转
    assert out["empty_retry"] == 1


# ----------------------------------------------------------------------
# FR-F-1 结论文案
# ----------------------------------------------------------------------


def test_summarize_confirmed_no_data_wording() -> None:
    out = summarize_node(
        {
            "rows": [],
            "columns": [],
            "no_data_confirmed": True,
            "fix_reasoning": "该区间内订单表无记录",
            "retrieved": {},
        },
        gateway=FakeGateway(),
    )
    assert "确认无数据" in out["answer"]
    assert "该区间内订单表无记录" in out["answer"]


def test_summarize_empty_after_relaxation_wording() -> None:
    out = summarize_node(
        {"rows": [], "columns": [], "empty_retry": 1, "retrieved": {}},
        gateway=FakeGateway(),
    )
    assert "已尝试放宽条件重查 1 次" in out["answer"]
    assert "确认无数据" not in out["answer"]


# ----------------------------------------------------------------------
# FR-F-2 修正痕迹
# ----------------------------------------------------------------------


def test_conclusion_mentions_fix_trace() -> None:
    gateway = FakeGateway(insight=Insight(conclusion="华东最高。", highlights=[]))
    out = summarize_node(
        {
            "rows": [{"大区": "华东", "成交总额": 100}],
            "columns": ["大区", "成交总额"],
            "retry_count": 1,
            "fix_reasoning": "orders.created_at 写成了 create_at",
            "retrieved": {},
        },
        gateway=gateway,
    )
    assert "1 次自动修正" in out["answer"]
    assert "orders.created_at 写成了 create_at" in out["answer"]
    assert out["fix_note"] == {
        "attempts": 1,
        "reason": "orders.created_at 写成了 create_at",
    }


def test_no_fix_trace_when_first_try_succeeded() -> None:
    gateway = FakeGateway(insight=Insight(conclusion="华东最高。", highlights=[]))
    out = summarize_node(
        {
            "rows": [{"大区": "华东", "成交总额": 100}],
            "columns": ["大区", "成交总额"],
            "retry_count": 0,
            "retrieved": {},
        },
        gateway=gateway,
    )
    assert "修正" not in out["answer"]
    assert out["fix_note"] is None


def test_model_written_trace_is_not_duplicated() -> None:
    """模型已经在结论里写了修正，就不再追加第二句，避免两处说法打架。"""
    gateway = FakeGateway(
        insight=Insight(conclusion="华东最高。本轮经过 1 次修正。", highlights=[])
    )
    out = summarize_node(
        {
            "rows": [{"大区": "华东", "成交总额": 100}],
            "columns": ["大区", "成交总额"],
            "retry_count": 1,
            "fix_reasoning": "字段名写错",
            "retrieved": {},
        },
        gateway=gateway,
    )
    assert out["answer"].count("次自动修正") == 0


def test_fix_attempts_not_reported_as_unverified_number() -> None:
    """修正次数是服务端事实，与 row_count 同类，不该被 FR-B-3 判成待查。"""
    rows = [{"大区": "华东", "成交总额": 100}]
    # 修正 2 次、结果 1 行：不传 retry_count 时这个 2 在结果集里找不到出处
    flagged = check_conclusion_numbers("华东最高（经过 2 次自动修正）。", rows, row_count=1)
    assert [item["value"] for item in flagged] == [2.0]
    assert (
        check_conclusion_numbers(
            "华东最高（经过 2 次自动修正）。", rows, row_count=1, retry_count=2
        )
        == []
    )
