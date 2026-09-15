"""评测执行器。

跑 three 组：

    recall    语义层口径召回。不经过模型，结论可直接引用。
    pipeline  端到端链路。模型在本环境为桩实现，.SQL 来自预置模板，
              因此本组验证的是链路与图表构造，不验证模型生成能力。
    safety    守卫拦截。把危险语句注入 generate 节点，模拟模型被诱导的
              情形，验证执行前的最后一道防线独立生效。

三组各自得出一项指标，互不替代：口径召回准确率、端到端可执行率、危险语句
拦截率。合成一个总数会把性质完全不同的东西平均掉，因此不做。

跑法：

    .venv/Scripts/python.exe eval/run_eval.py
    .venv/Scripts/python.exe eval/run_eval.py --group recall --verbose
    .venv/Scripts/python.exe eval/run_eval.py --today 2026-09-15
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from langchain_core.messages import HumanMessage  # noqa: E402
import yaml  # noqa: E402

from app.agent.checkpointer import thread_config  # noqa: E402
from app.agent.graph import summarize_result  # noqa: E402
from app.agent.state import AgentState, initial_state  # noqa: E402
from app.api.deps import build_container  # noqa: E402
from app.llm.schemas import SqlDraft  # noqa: E402
from app.semantic.loader import Catalog, load_catalog, recall  # noqa: E402

T2SQL_ROOT = BACKEND_DIR.parent
CASES_PATH = Path(__file__).resolve().parent / "cases.yaml"
REPORT_PATH = Path(__file__).resolve().parent / "report.json"

PASS = "PASS"
FAIL = "FAIL"


@dataclass
class CaseOutcome:
    """一条评测的实际结果。observed 里保留观察值，便于脱离实现看到差异。"""

    case_id: str
    question: str
    intent: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    @property
    def mark(self) -> str:
        if self.skipped:
            return "SKIP"
        return PASS if self.passed else FAIL


def load_cases() -> dict[str, Any]:
    raw = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    if int(raw.get("version", 0)) != 1:
        raise SystemExit(f"评测集版本不受支持：{raw.get('version')}")
    return raw


# ---------------------------------------------------------------------------
# recall 组
# ---------------------------------------------------------------------------


def run_recall(cases: list[dict[str, Any]], catalog: Catalog, today: date) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        hit = recall(case["question"], catalog, today)
        expect = case.get("expect") or {}

        got_metrics = [m["name"] for m in hit.metrics]
        got_dims = [d["name"] for d in hit.dimensions]
        got_time = (
            {
                "label": hit.time_range.label,
                "start": hit.time_range.start,
                "end": hit.time_range.end,
            }
            if hit.time_range
            else None
        )
        observed = {
            "metrics": got_metrics,
            "dimensions": got_dims,
            "time_range": got_time,
            "tables": [t["name"] for t in hit.tables],
            "value_hints": hit.value_hints,
            "matched_terms": hit.matched_terms,
        }
        reasons: list[str] = []

        if expect.get("empty"):
            if got_metrics or got_dims:
                reasons.append(f"期望不召回口径，实际命中指标 {got_metrics}、维度 {got_dims}")
        else:
            want_metrics = expect.get("metrics")
            if want_metrics is not None and set(want_metrics) != set(got_metrics):
                missing = sorted(set(want_metrics) - set(got_metrics))
                extra = sorted(set(got_metrics) - set(want_metrics))
                reasons.append(
                    f"指标：期望 {sorted(want_metrics)}，实际 {got_metrics}"
                    + (f"（漏 {missing}）" if missing else "")
                    + (f"（多 {extra}）" if extra else "")
                )

            want_dims = expect.get("dimensions")
            if want_dims is not None and set(want_dims) != set(got_dims):
                missing = sorted(set(want_dims) - set(got_dims))
                extra = sorted(set(got_dims) - set(want_dims))
                reasons.append(
                    f"维度：期望 {sorted(want_dims)}，实际 {got_dims}"
                    + (f"（漏 {missing}）" if missing else "")
                    + (f"（多 {extra}）" if extra else "")
                )

        want_time = expect.get("time_range")
        if want_time is not None:
            if got_time is None:
                reasons.append(f"时间：期望 {want_time['label']}，实际未解析出区间")
            elif got_time != want_time:
                reasons.append(
                    f"时间：期望 {want_time['label']} "
                    f"[{want_time['start']}, {want_time['end']})，"
                    f"实际 {got_time['label']} "
                    f"[{got_time['start']}, {got_time['end']})"
                )

        want_hints = expect.get("value_hints") or []
        if want_hints:
            joined = " | ".join(hit.value_hints)
            absent = [h for h in want_hints if h not in joined]
            if absent:
                reasons.append(f"取值提示：未出现 {absent}，实际 {hit.value_hints}")

        outcomes.append(
            CaseOutcome(
                case_id=case["id"],
                question=case["question"],
                intent=case.get("intent", ""),
                passed=not reasons,
                reasons=reasons,
                observed=observed,
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# pipeline 与 safety 组
# ---------------------------------------------------------------------------


def _invoke(container: Any, session_id: str, question: str, inject_sql: str | None) -> dict[str, Any]:
    """跑一张图。inject_sql 非空时接管 SqlDraft 的返回值。

    只替换 SqlDraft 这一个 schema：路由、图表、结论仍走原路径，否则注入的
    就不是「一条坏 SQL」而是整个链路的行为。
    """
    inputs: dict[str, Any] = dict(initial_state(session_id, question))
    inputs["messages"] = [HumanMessage(content=question)]

    gateway = container.gateway
    original = gateway.complete_json

    if inject_sql is None:
        final = container.graph.invoke(inputs, config=thread_config(session_id))
        return summarize_result(final)

    def patched_complete_json(prompt: str, schema: type, **kwargs: Any):
        if schema is SqlDraft:
            return SqlDraft(
                sql=inject_sql,
                reasoning="评测注入的危险语句",
                metric_used=[],
            )
        return original(prompt, schema, **kwargs)

    gateway.complete_json = patched_complete_json
    try:
        final = container.graph.invoke(inputs, config=thread_config(session_id))
        return summarize_result(final)
    finally:
        gateway.complete_json = original


def run_pipeline(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        expect = case.get("expect") or {}
        if mock and case.get("requires_llm"):
            # 桩实现的路由固定返回取数意图，这类分支在切到真实模型前跑不到，
            # 计入总数只会拉低一个与链路无关的数字。
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=True,
                    reasons=[],
                    observed={"skipped": "桩实现走不到该分支"},
                    skipped=True,
                )
            )
            continue
        session_id = f"eval-{case['id']}"
        result = _invoke(container, session_id, case["question"], None)

        status = result.get("status")
        row_count = result.get("row_count") or 0
        chart = result.get("chart") or {}
        observed = {
            "status": status,
            "row_count": row_count,
            "chart_type": chart.get("chart_type"),
            "columns": result.get("columns"),
            "sql": result.get("sql"),
            "retry_count": result.get("retry_count"),
            "stages": [s.get("node") for s in result.get("stages") or []],
            "error": result.get("sql_error") or result.get("error_message"),
        }
        reasons: list[str] = []

        if status != expect.get("status"):
            reasons.append(
                f"终态：期望 {expect.get('status')}，实际 {status}"
                + (f"（{observed['error']}）" if observed["error"] else "")
            )
        min_rows = expect.get("min_rows")
        if min_rows is not None and row_count < min_rows:
            reasons.append(f"行数：期望 ≥ {min_rows}，实际 {row_count}")
        want_chart = expect.get("chart_type")
        if want_chart is not None and chart.get("chart_type") != want_chart:
            reasons.append(f"图型：期望 {want_chart}，实际 {chart.get('chart_type')}")

        outcomes.append(
            CaseOutcome(
                case_id=case["id"],
                question=case["question"],
                intent=case.get("intent", ""),
                passed=not reasons,
                reasons=reasons,
                observed=observed,
            )
        )
    return outcomes


def run_safety(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        expect = case.get("expect") or {}
        inject_sql = case["inject_sql"]

        # 先单独问守卫：确认这条语句本身就该被拒，与图的行为分开看。
        # 两处结论不一致时，问题在节点而不是守卫。
        guard_verdict = container.guard.check(inject_sql)

        session_id = f"eval-{case['id']}"
        result = _invoke(container, session_id, case["question"], inject_sql)
        status = result.get("status")
        row_count = result.get("row_count") or 0
        sql = result.get("sql") or ""

        # 缺 LIMIT 的查询会被守卫改写成带上界的子查询。放行是设计意图，
        # 因此这里要确认的是「改写是否发生」，而不是「是否被拒绝」。
        rewritten = bool(sql) and sql.strip() != inject_sql.strip()

        observed = {
            "inject_sql": inject_sql,
            "guard_allowed": bool(guard_verdict.ok),
            "guard_errors": list(guard_verdict.errors),
            "guard_rewritten": rewritten,
            "status": status,
            "row_count": row_count,
            "executed_sql": sql,
            "retry_count": result.get("retry_count"),
            "stages": [s.get("node") for s in result.get("stages") or []],
        }
        reasons: list[str] = []

        executed = status == "ok" and row_count > 0
        want_blocked = expect.get("blocked")
        if want_blocked is True and executed:
            reasons.append(f"语句被执行成功，返回 {row_count} 行")
        if want_blocked is False and not executed:
            reasons.append(f"语句本应被限流后放行，实际未产出结果（status={status}）")
        if want_blocked is False and not rewritten:
            reasons.append("语句原样放行，守卫未做限流改写")
        max_rows = expect.get("max_rows")
        if max_rows is not None and row_count > max_rows:
            reasons.append(f"行数：期望 ≤ {max_rows}，实际 {row_count}")
        if expect.get("status") and status != expect["status"]:
            reasons.append(f"终态：期望 {expect['status']}，实际 {status}")
        if want_blocked is True and guard_verdict.ok:
            reasons.append("守卫未拒绝该语句，安全性依赖执行层而非校验层")

        outcomes.append(
            CaseOutcome(
                case_id=case["id"],
                question=case["question"],
                intent=case.get("intent", ""),
                passed=not reasons,
                reasons=reasons,
                observed=observed,
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# 计分与报告
# ---------------------------------------------------------------------------


def _ratio(hit: int, total: int) -> str:
    return f"{hit}/{total}" if total else "—"


def _percent(hit: int, total: int) -> str:
    return f"{hit / total * 100:.1f}%" if total else "—"


def summarise_recall(outcomes: list[CaseOutcome], cases: list[dict[str, Any]]) -> dict[str, Any]:
    """口径召回拆成四个子项。

    只看「整条是否通过」会把「指标对但时间错」和「全错」算成同一种失败，
    改起来没有抓手，因此每个子项单独统计。
    """
    by_id = {c["id"]: c.get("expect") or {} for c in cases}

    def subset(name: str) -> int:
        return sum(1 for c in cases if name in (by_id[c["id"]] or {}))

    counters = {"metrics": 0, "dimensions": 0, "time_range": 0, "value_hints": 0, "empty": 0}
    totals = {k: 0 for k in counters}

    passed_metrics = passed_dims = passed_time = passed_empty = 0
    for case, outcome in zip(cases, outcomes):
        expect = by_id[case["id"]]
        obs = outcome.observed
        if expect.get("empty"):
            totals["empty"] += 1
            passed_empty += int(not obs["metrics"] and not obs["dimensions"])
            continue
        if "metrics" in expect:
            totals["metrics"] += 1
            passed_metrics += int(set(expect["metrics"]) == set(obs["metrics"]))
        if "dimensions" in expect:
            totals["dimensions"] += 1
            passed_dims += int(set(expect["dimensions"]) == set(obs["dimensions"]))
        if "time_range" in expect:
            totals["time_range"] += 1
            passed_time += int(obs["time_range"] == expect["time_range"])
        if expect.get("value_hints"):
            totals["value_hints"] += 1

    return {
        "total": len(outcomes),
        "passed": sum(1 for o in outcomes if o.passed),
        "subitems": [
            ("指标命中", passed_metrics, totals["metrics"]),
            ("维度命中", passed_dims, totals["dimensions"]),
            ("时间解析", passed_time, totals["time_range"]),
            ("空召回避", passed_empty, totals["empty"]),
        ],
    }


def print_outcomes(title: str, outcomes: list[CaseOutcome], verbose: bool) -> None:
    head = str(title).strip().split("。")[0].strip()
    print(f"\n=== {head} ===")
    for outcome in outcomes:
        print(f"  {outcome.mark}  {outcome.case_id:<12} {outcome.question}")
        for reason in outcome.reasons:
            print(f"         └─ {reason}")
        if verbose:
            for key, value in outcome.observed.items():
                if key == "sql" or key == "inject_sql":
                    value = " ".join(str(value).split())[:120]
                print(f"         · {key}: {value}")


def print_summary_recall(outcomes: list[CaseOutcome], cases: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarise_recall(outcomes, cases)
    print(f"\n口径召回：{_ratio(summary['passed'], summary['total'])} 条完全命中")
    for name, hit, total in summary["subitems"]:
        if total:
            print(f"    {name}      {_ratio(hit, total):>8}    {_percent(hit, total):>7}")
    return summary


def print_summary_simple(title: str, outcomes: list[CaseOutcome]) -> dict[str, Any]:
    # 跳过的条目不进分母：它们在当前环境下没有产生任何观察值，计入只会稀释比例。
    active = [o for o in outcomes if not o.skipped]
    skipped = len(outcomes) - len(active)
    passed = sum(1 for o in active if o.passed)
    total = len(active)
    print(f"{title}：{_ratio(passed, total)}    {_percent(passed, total)}")
    if skipped:
        print(f"    另有 {skipped} 条在桩实现下走不到，未计入（切到真实模型后自动生效）")
    return {"total": total, "passed": passed, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑评测集")
    parser.add_argument(
        "--group",
        choices=("recall", "pipeline", "safety", "all"),
        default="all",
        help="只跑某一组，默认全跑",
    )
    parser.add_argument("--today", help="时间锚点 YYYY-MM-DD，默认取评测集的 reference_today")
    parser.add_argument("--verbose", action="store_true", help="打印每条的观察值")
    parser.add_argument("--output", default=str(REPORT_PATH), help="报告落盘路径")
    args = parser.parse_args(argv)

    suite = load_cases()
    meta = suite.get("meta") or {}
    today_str = args.today or meta.get("reference_today")
    today = date.fromisoformat(str(today_str))

    print(f"评测集：{meta.get('name')}")
    print(f"时间锚点：{today.isoformat()}" + (f"（{meta.get('reference_weekday', '')}）" if meta.get("reference_weekday") else ""))
    print(f"评测时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    groups = suite.get("groups") or {}
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reference_today": today.isoformat(),
        "llm_mock": None,
        "groups": {},
    }

    need_recall = args.group in ("recall", "all")
    need_graph = args.group in ("pipeline", "safety", "all")

    container = None
    if need_graph:
        container = build_container()
        report["llm_mock"] = bool(container.gateway.mock)
        if container.gateway.mock:
            print(
                "\n注意：当前为桩实现模式，pipeline 组的 SQL 来自预置模板，"
                "该组结论只反映链路可用性，不代表模型生成能力。"
            )

    failures = 0

    if need_recall:
        cases = groups.get("recall", {}).get("cases") or []
        catalog = load_catalog()
        outcomes = run_recall(cases, catalog, today)
        print_outcomes(f"recall　{groups.get('recall', {}).get('description', '')}", outcomes, args.verbose)
        summary = print_summary_recall(outcomes, cases)
        report["groups"]["recall"] = {
            **summary,
            "subitems": [
                {"name": n, "passed": h, "total": t} for n, h, t in summary["subitems"]
            ],
            "cases": [
                {
                    "id": o.case_id,
                    "question": o.question,
                    "intent": o.intent,
                    "passed": o.passed,
                    "reasons": o.reasons,
                    "observed": o.observed,
                }
                for o in outcomes
            ],
        }
        failures += summary["total"] - summary["passed"]

    if args.group in ("pipeline", "all"):
        assert container is not None
        cases = groups.get("pipeline", {}).get("cases") or []
        outcomes = run_pipeline(cases, container)
        print_outcomes(f"pipeline　{groups.get('pipeline', {}).get('description', '')}", outcomes, args.verbose)
        summary = print_summary_simple("\n端到端可执行率", outcomes)
        report["groups"]["pipeline"] = {
            **summary,
            "cases": [
                {
                    "id": o.case_id,
                    "question": o.question,
                    "intent": o.intent,
                    "passed": o.passed,
                    "reasons": o.reasons,
                    "observed": o.observed,
                }
                for o in outcomes
            ],
        }
        failures += summary["total"] - summary["passed"]

    if args.group in ("safety", "all"):
        assert container is not None
        cases = groups.get("safety", {}).get("cases") or []
        outcomes = run_safety(cases, container)
        print_outcomes(f"safety　{groups.get('safety', {}).get('description', '')}", outcomes, args.verbose)
        summary = print_summary_simple("\n危险语句拦截率", outcomes)
        report["groups"]["safety"] = {
            **summary,
            "cases": [
                {
                    "id": o.case_id,
                    "question": o.question,
                    "intent": o.intent,
                    "passed": o.passed,
                    "reasons": o.reasons,
                    "observed": o.observed,
                }
                for o in outcomes
            ],
        }
        failures += summary["total"] - summary["passed"]

    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n报告已写入 {args.output}")
    print(f"未通过 {failures} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
