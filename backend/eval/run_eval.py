"""评测执行器。

跑八组：

    recall       语义层口径召回。不经过模型，结论可直接引用。
    pipeline     端到端链路。模型在本环境为桩实现，SQL 来自预置模板，
                 因此本组验证的是链路与图表构造，不验证模型生成能力。
    safety       守卫拦截。把危险语句注入 generate 节点，模拟模型被诱导的
                 情形，验证执行前的最后一道防线独立生效。
    datalineage  数据血缘：来源表与数据时效，对应 FR-D-1、FR-D-2、
                 FR-D-4。FR-D-5（时间范围与语句一致性）登记在第 7 批，
                 待 `time_range_mismatch` 字段落库后补一条用例。
    baseline     对比基准：环比同比是否给出、不完整周期是否隔离、规则表是否
                 生效，每轮额外执行的查询是否收敛在上限内。对应 FR-M-1 至
                 FR-M-3。基准计算是服务端确定性规则，桩模式下同样有效。
    drilldown    下钻与异常标记：问句是否只在上一轮槽位上加深一层、拼出来的
                 句子是否真的查得到东西、findings 字段是否齐备、不完整周期下
                 是否隔离了偏离类异常。对应 FR-L-1 至 FR-L-3。
    robust       结论数字核对、时间区间一致性、脱敏管道（FR-B-3 / FR-D-5 /
                 FR-I-3）。
    alert        异常订阅：命中、未命中、失败可区分与同周期幂等（FR-O-1~O-5）。
    multiturn    多轮改写与槽位结构化（FR-A-1~A-5）。
    advanced     窗口函数与 CTE（FR-J-1），桩模式下全部跳过。
    outofscope   答非所问的两类终态：域外问题不生成查询、归因问题不给数字。
                 与其余各组相反，本组验的是「系统什么时候不该给数」。

各组各自得出一项指标，互不替代：口径召回准确率、端到端可执行率、危险语句
拦截率、血缘字段完整率、对比基准判定率、下钻与异常标记判定率、核对与脱敏
完整率、异常订阅判定率。合成一个总数会把性质完全不同的东西平均掉，因此不做。

跑法：

    .venv/Scripts/python.exe eval/run_eval.py
    .venv/Scripts/python.exe eval/run_eval.py --group recall --verbose
    .venv/Scripts/python.exe eval/run_eval.py --today 2026-10-14
    .venv/Scripts/python.exe eval/run_eval.py --group datalineage --verbose

时间锚点：不传 --today 时取库内 data_as_of（orders 与 refunds 两表中较早的
最大 created_at），与生产链路同一来源——问答里「上个月」按数据截止日换算，
评测要验的就是这条路径，锚点取系统日期会验出一条生产不会走的行为。库不可用
或取不到截止日时退回评测集的 reference_today。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
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
from app.agent.drilldown import build_drilldown  # noqa: E402
from app.agent.graph import summarize_result  # noqa: E402
from app.agent.state import AgentState, initial_state  # noqa: E402
from app.agent.alert_eval import evaluate_rule  # noqa: E402
from app.api.deps import build_container  # noqa: E402
from app.db.masking import mask_result  # noqa: E402
from app.db.repository import AlertRuleRecord  # noqa: E402
from app.db.sqlite_repo import SqliteQueryRepository  # noqa: E402
from app.llm.schemas import SqlDraft  # noqa: E402
from app.observability import counters, reset_counters  # noqa: E402
from app.semantic.expression_check import (  # noqa: E402
    check_expression_signatures,
    signatures_for,
)
from app.semantic.loader import UNSUPPORTED_CAVEAT_PREFIX, Catalog, load_catalog, recall  # noqa: E402

T2SQL_ROOT = BACKEND_DIR.parent
CASES_PATH = Path(__file__).resolve().parent / "cases.yaml"
REPORT_PATH = Path(__file__).resolve().parent / "report.json"

PASS = "PASS"
FAIL = "FAIL"

# FR-J-3：评测进程内汇总每次图调用的查询耗时（query_elapsed_ms），
# 报告顶层给 p50/p95。桩模式下同样有真实的 SQL 执行耗时。
_LATENCIES: list[float] = []

# FR-J-5：评测复用缓存。进程级字典，由 reset_telemetry 清空。
_INVOKE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def reset_telemetry() -> None:
    _LATENCIES.clear()
    _INVOKE_CACHE.clear()
    reset_counters()


def percentile(values: list[float], pct: float) -> float | None:
    """最近秩法百分位，0~100。无样本返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
    return round(ordered[idx], 1)

# 评测用例 expect 子字段白名单。
#
# 三组各自的允许字段。来源有三层：
#   1. 已有字段——cases.yaml 历史已用，未在本批变更
#   2. FR-K-1 引入——dimensions / granularity / sql_contains / must_join，
#      把 pipeline 断言从「status + 行数 + 图型」提升到「指标、维度、粒度、
#      JOIN、关键子串」五维
#   3. RouteDecision.slots 占位——filters / order_by / top_n 是 FR-A-5 槽位
#      化要求的字段名，本批不消费、仅为 FR-K-3 的同构约束预留入口；后续
#      RouteDecision.slots 落地后字段名必须从此白名单内选取，cases.yaml
#      与运行时两处共用同一组字段名
EXPECT_FIELDS: dict[str, set[str]] = {
    "recall": {
        "metrics", "dimensions", "time_range", "value_hints", "empty",
    },
    "pipeline": {
        "status", "min_rows", "chart_type", "requires_llm",
        "dimensions", "granularity", "sql_contains", "must_join",
        "filters", "order_by", "top_n",
        # FR-D-4 补充：期望 caveats 里出现/不出现未收录维度提示
        "dimension_caveat",
        # 口径断言：SQL 的聚合表达式必须满足这里列出的指标的
        # expression_signature。与运行时 validate 节点共用同一个校验器与
        # 同一份签名定义，见 app/semantic/expression_check.py。
        #
        # 这一维补的是既有断言的盲区：status / min_rows / chart_type 只能证明
        # 「跑出了结果」，dimensions / must_join 只能证明「用了对的表与列」，
        # 都不能证明「算的是对的量」。问题问毛利而语句算成交额时，四条断言
        # 全部成立——结果是能执行、有数据、图也画得出，用户拿到的却是错的数。
        "metric_signature",
    },
    "safety": {
        "inject_sql", "blocked", "status", "max_rows",
    },
    # FR-D-1 期望 source_tables 含指定表（集合子集，不要求顺序）；FR-D-2 期望
    # data_freshness 字段齐备且日期为 ISO 格式；FR-D-4 在桩实现下因 metric_used
    # 恒为空而默认通过，故不单独开字段。time_range_consistent（FR-D-5）
    # 期望 SQL 时间条件与召回区间一致：true / false / null。
    "datalineage": {
        "status", "min_rows", "requires_llm",
        "source_tables",
        "freshness_present", "freshness_orders_cutoff_iso", "freshness_refunds_cutoff_iso",
        "time_range_consistent",
    },
    # 第 7 批 FR-B-3 / FR-D-5 / FR-I-3 健壮性核对：
    #   number_pending_min   number_checks（库外待查数字）至少 N 条
    #   number_clean         不允许出现任何待查数字
    #   time_range_consistent 时间区间一致性期望值
    #   masked               经脱敏管道后 masked 标记期望值
    #   masked_fields_any    masked_fields 至少包含其中一个物理列
    "robust": {
        "status", "min_rows", "requires_llm",
        "number_pending_min", "number_clean",
        "time_range_consistent", "masked", "masked_fields_any",
    },
    # FR-O：case.rule 描述规则；expect 只断言行内字段。
    #   eval_status   求值记录状态 ok / error
    #   hit           是否命中
    #   idempotent    同周期重复求值不得产生新行（id 相同）
    "alert": {
        "eval_status", "hit", "idempotent",
    },
    # FR-M-1 至 FR-M-3：
    #   granularity       期望的基准粒度
    #   mom_present       期望是否给出环比（false 时要求整条基准被跳过）
    #   yoy_present       同上，针对同比
    #   mom_comparable    期望 mom.comparable 的取值；不完整周期下必须为 false
    #   partial_period    期望的本期完整度标记
    #   note              期望返回体里的一句话结论，逐字比较
    #   missing_contains  missing_reasons 中应出现的子串，用于确认「为什么没给」
    #   max_queries       本轮允许的 SQL 执行总次数上限（主查询 1 次 + 基准 3 次）
    "baseline": {
        "status", "min_rows", "requires_llm",
        "granularity", "mom_present", "yoy_present", "mom_comparable",
        "partial_period", "note", "missing_contains", "max_queries",
    },
    # FR-L-1 至 FR-L-3、FR-L-5：
    #   question          下钻问句的逐字期望值
    #   next_dimension    期望加深到的那一层维度配置名
    #   only_one_level    断言只加深一层：加深后的维度不等于当前维度
    #   available         期望是否拼得出问句（false 用于「已是最细维度」）
    #   findings_min      findings 至少要有几条
    #   findings_rules    findings 中应出现的 rule 取值
    #   findings_absent   findings 中不出现的 rule 取值，用于「不完整周期下
    #                     不得给出偏离类异常」这类反向断言
    #   findings_fields   findings 每项必须具备的字段
    #   deepens_to        下钻问句跑完后，结果集应出现的新维度列（子串即可）
    "drilldown": {
        "status", "min_rows", "requires_llm",
        "question", "next_dimension", "only_one_level", "available",
        "findings_min", "findings_rules", "findings_absent", "findings_fields",
        "deepens_to",
    },
    # FR-A-1 至 FR-A-5 多轮与槽位：
    #   sql_contains   SQL 文本中必须出现的子串
    #   top_n          桩 slots.top_n 期望值
    #   slots_metrics_any      slots.metrics 至少包含其中之一
    #   slots_dimensions_any   slots.dimensions 至少包含其中之一
    #   slots_filters_any      slots.filters 至少包含其中一项（子串匹配）
    #   slots_time_range       slots.time_range 期望值（逐字比较）
    "multiturn": {
        "status", "min_rows", "requires_llm",
        "sql_contains", "top_n",
        "slots_metrics_any", "slots_dimensions_any",
        "slots_filters_any", "slots_time_range",
    },
    # FR-J-1 窗口函数与 CTE：
    #   sql_contains   SQL 文本中必须出现的子串（ROW_NUMBER / LAG / WITH）
    "advanced": {
        "status", "min_rows", "requires_llm",
        "sql_contains",
    },
    # 答非所问的两类终态（域外 / 归因）：
    #   status           期望终态 out_of_scope / attribution / ok
    #   intent           期望意图，与 status 分开断言：两者同名但由不同节点产出
    #   no_sql           断言本轮没有生成任何语句。这是本组的核心——
    #                    这两类问题一旦生成 SQL，用户拿到的就是一个无关数字
    #   answer_contains  答案里必须出现的子串（替代问法、判定依据等）
    "outofscope": {
        "status", "intent", "no_sql", "answer_contains",
    },
    "slotcoverage": {
        "slot_gap_kinds",
    },
}

# 时间粒度 → SQL 特征模式。判定规则是「SQL 文本中出现任一模式即认为
# 采用了该粒度」，模式匹配不区分大小写。quarter 的判定放宽为含除以 3
# 的算术或 quarter 字样，避免被具体写法锁死。
GRANULARITY_PATTERNS: dict[str, tuple[str, ...]] = {
    "day": (r"strftime\('%Y-%m-%d'", r"\bdate\(", r"DATE\("),
    "week": (r"strftime\('%Y-%W'", r"strftime\('%W'", r"strftime\('%Y-%w'"),
    "month": (r"strftime\('%Y-%m'", r"strftime\('%m'", r"strftime\('%Y-%m-"),
    "quarter": (r"\bquarter\b", r"\)\s*/\s*3\b", r"\)\s+div\s+3\b"),
    "year": (r"strftime\('%Y'", r"\bYEAR\("),
}


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
    _validate_expect_fields(raw)
    return raw


def _validate_expect_fields(suite: dict[str, Any]) -> None:
    """校验所有 case.expect 的字段名落在 EXPECT_FIELDS 白名单内。

    防止两类问题：
    - 漏写：用例里写了某字段但 run_* 函数没消费，断言形同虚设
    - 拼写：期望字段名与运行时字段名不一致，断言永远走不到
    校验失败直接 SystemExit，让「改一处漏两处」在评测启动时就暴露。
    """
    groups = suite.get("groups") or {}
    for group_name, group_def in groups.items():
        allowed = EXPECT_FIELDS.get(group_name)
        if allowed is None:
            continue  # 未知组别不在校验范围内，跳过
        for case in group_def.get("cases") or []:
            expect = case.get("expect") or {}
            unknown = sorted(set(expect) - allowed)
            if unknown:
                raise SystemExit(
                    f"评测集字段名校验失败：{group_name}.{case.get('id')} 的 expect "
                    f"包含未登记字段 {unknown}；允许的字段为 {sorted(allowed)}"
                )


def _sql_contains(sql: str, needles: list[str]) -> list[str]:
    """返回 needles 中未在 sql 出现的项。全出现返回空列表。"""
    sql_lower = sql.lower()
    return [n for n in needles if n.lower() not in sql_lower]


def _must_join(sql: str, tables: list[str]) -> list[str]:
    """返回 tables 中未在 sql 中作为 JOIN 目标出现的表名。

    匹配规则：'JOIN <table>' 或 'JOIN ... AS <table>' 等任意 JOIN 形式
    的子串，大小写不敏感。LEFT/RIGHT/INNER/OUTER/CROSS JOIN 都覆盖。
    """
    sql_lower = sql.lower()
    missing: list[str] = []
    for table in tables:
        # 用单词边界粗略匹配：JOIN 之后到下一个逗号/换行/ON/WHERE 之前含表名
        needle = f"join {table.lower()}"
        if needle not in sql_lower:
            missing.append(table)
    return missing


def _has_granularity(sql: str, granularity: str) -> bool:
    """判定 SQL 是否采用了给定时间粒度。

    通过 GRANULARITY_PATTERNS 中的正则模式匹配。任一模式命中即通过，
    不区分大小写。无法识别时返回 False。
    """
    import re

    patterns = GRANULARITY_PATTERNS.get(granularity)
    if not patterns:
        return False
    return any(re.search(p, sql, flags=re.IGNORECASE) for p in patterns)


_FUNCTION_TOKENS = {"date", "datetime", "strftime", "julianday", "coalesce", "cast", "case"}


def _columns_from_expression(expression: str) -> set[str]:
    """从语义层维度的 expression 里抽出物理列名候选。

    `date(orders.created_at)` -> {"created_at"}；`users.level` -> {"level"}。
    函数名与表名 Kyiv Advisor 一并丢弃，只留下末段标识符。
    """
    found: set[str] = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", expression):
        tail = token.split(".")[-1]
        if tail.lower() not in _FUNCTION_TOKENS:
            found.add(tail.lower())
    return found


def _has_dimensions(
    sql: str,
    columns: list[str],
    dimensions: list[str],
    catalog: Any = None,
) -> list[str]:
    """返回 dimensions 中未在 sql 与结果集列名里出现的项。

    两处都查：SQL 的 SELECT/GROUP BY 列表（子串匹配），以及结果集列名。

    catalog 用于把语义层的维度标识名解析成物理列：`order_date` 的 expression
    是 `date(orders.created_at)`，模型写的是 `date(o.created_at)`，两者语义
    等价但字面不同。只按标识名匹配会把「用了正确列」判成「没用」，2026-09-22
    实测 p006 / p008 就栽在这里。解析后同时接受：标识名、expression 里的
    物理列名、以及中文 display（模型常用 display 起别名）。
    """
    sql_lower = sql.lower()
    cols_lower = [str(c).lower() for c in columns]
    by_name = catalog.dimension_by_name if catalog is not None else {}
    missing: list[str] = []
    for d in dimensions:
        candidates: set[str] = {d.lower()}
        item = by_name.get(d)
        if item is not None:
            # dimension_by_name 里可能是 dict（loader 直接加载的形态），
            # 也可能是 dataclass，两种都取到值即可。
            get = item.get if isinstance(item, dict) else lambda k: getattr(item, k, None)
            expression = get("expression") or ""
            if expression:
                candidates |= _columns_from_expression(expression)
            display = get("display")
            if display:
                candidates.add(str(display).lower())
        if not any(c in sql_lower or c in cols_lower for c in candidates):
            missing.append(d)
    return missing


def _has_source_tables(actual: list[str], expected: list[str]) -> list[str]:
    """集合子集判定：expected 中未出现在 actual 的项。

    顺序无关。重复项视为一个。返回缺失项列表；为空即视为通过。
    """
    actual_set = {str(t).lower() for t in actual}
    return [t for t in expected if str(t).lower() not in actual_set]


def _is_iso_date(value: Any) -> bool:
    """判定 value 是否为 YYYY-MM-DD 形式的合法日期。"""
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


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

    FR-J-5：评测复用。对相同问题复用同一轮结果，仅评测范围内生效，
    不引入运行时缓存。缓存键为 (session_id, question)，inject_sql 非空时不缓存。
    """
    # FR-J-5：结果复用缓存。仅在评测进程内生效，不引入运行时缓存。
    # inject_sql 非空（safety 组注入危险语句）时不走缓存，因为注入的语句
    # 是被测对象，每次都需要独立执行。
    cache_key = (session_id, question) if inject_sql is None else None
    if cache_key is not None and cache_key in _INVOKE_CACHE:
        return _INVOKE_CACHE[cache_key]

    inputs: dict[str, Any] = dict(initial_state(session_id, question))
    inputs["messages"] = [HumanMessage(content=question)]

    gateway = container.gateway
    original = gateway.complete_json

    if inject_sql is None:
        final = container.graph.invoke(inputs, config=thread_config(session_id))
        result = summarize_result(final)
        elapsed = result.get("query_elapsed_ms")
        if isinstance(elapsed, (int, float)) and elapsed > 0:
            _LATENCIES.append(float(elapsed))
        # FR-J-5：缓存结果，同一问题不再重跑图
        if cache_key is not None:
            _INVOKE_CACHE[cache_key] = result
        return result

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
            # 注意：skipped 的 passed 标为 False，避免与「通过」混淆；该条
            # 仍留在 outcomes 与 report 的 cases 数组里，保证 cases 长度与
            # total + skipped 一致（FR-K-2）。
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=False,
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
        sql = result.get("sql") or ""
        observed = {
            "status": status,
            "row_count": row_count,
            "chart_type": chart.get("chart_type"),
            "columns": result.get("columns"),
            "sql": sql,
            "retry_count": result.get("retry_count"),
            "stages": [s.get("node") for s in result.get("stages") or []],
            "error": result.get("sql_error") or result.get("error_message"),
            "caveats": result.get("caveats") or [],
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

        # FR-K-1 新增的四维断言。状态非 ok 时跳过（其它原因已记录），
        # 让失败信息指向更根源的问题而不是堆叠多条。
        if status == "ok":
            want_dims = expect.get("dimensions")
            if want_dims:
                missing_dims = _has_dimensions(
                    sql, result.get("columns") or [], want_dims, container.catalog
                )
                if missing_dims:
                    reasons.append(
                        f"维度：SQL 与结果集均未出现 {missing_dims}（期望来自语义层召回）"
                    )
            want_gran = expect.get("granularity")
            if want_gran and not _has_granularity(sql, want_gran):
                reasons.append(
                    f"粒度：期望 {want_gran}，SQL 中未出现对应的时间分组模式"
                )
            want_contains = expect.get("sql_contains")
            if want_contains:
                missing_contains = _sql_contains(sql, want_contains)
                if missing_contains:
                    reasons.append(f"关键子串：SQL 未包含 {missing_contains}")
            want_joins = expect.get("must_join")
            if want_joins:
                missing_joins = _must_join(sql, want_joins)
                if missing_joins:
                    reasons.append(f"JOIN：SQL 未与 {missing_joins} 表连接")
            want_sig = expect.get("metric_signature")
            if want_sig:
                known = {m.get("name"): m for m in container.catalog.metrics}
                unknown = [n for n in want_sig if n not in known]
                if unknown:
                    # 用例里写了不存在的指标名。报出来而不是静默跳过——
                    # 拼错名字的断言如果被忽略，这一维就等于没写。
                    reasons.append(f"口径：用例引用了未登记的指标 {unknown}")
                else:
                    problems = check_expression_signatures(
                        sql, signatures_for([known[n] for n in want_sig])
                    )
                    if problems:
                        reasons.append("口径：" + "；".join(problems))

        # 未收录维度提示（FR-D-4 补充）：这条断言与链路是否跑通无关，
        # 因此放在 status 判断之外——系统应当在不报错时也把口径风险说清楚。
        want_caveat = expect.get("dimension_caveat")
        if want_caveat is not None:
            hit = [
                c
                for c in (result.get("caveats") or [])
                if c.startswith(UNSUPPORTED_CAVEAT_PREFIX)
            ]
            if want_caveat and not hit:
                reasons.append(
                    "未收录维度：期望给出口径提示，实际 caveats 里没有"
                    f"（{result.get('caveats') or '空'}）"
                )
            if not want_caveat and hit:
                reasons.append(f"未收录维度：本轮不该报口径提示，实际报了 {hit}")

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
# datalineage 组
# ---------------------------------------------------------------------------


def run_dataline(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """血缘字段断言。

    与 pipeline 走同一张图，但只关心来源表与数据时效两个家族字段，不
    重复判 status / chart_type / 行数（这些已在 pipeline 组覆盖）。

    FR-D-1 的 source_tables 是 SQL `FROM`/`JOIN` 的真实表名集合，与语义层
    召回的 `retrieved.tables` 是两个概念——后者回答「候选」，前者回答
    「实际访问」。本组只验后者，前者由 recall 组语义层覆盖。

    FR-D-2 的 data_freshness 在可视化节点计算、随轮次落库。本组验证该
    字段非空且关键键（orders_cutoff / refunds_cutoff / threshold_days）
    齐备，orders_cutoff 与 refunds_cutoff 为 ISO 日期。

    FR-D-5 的 time_range_consistent 在编排层计算：SQL 时间条件覆盖召回区间
    才为 true。桩模板大多不带时间过滤，故含时间语义的问句期望值多为 false。
    """
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        expect = case.get("expect") or {}
        if mock and case.get("requires_llm"):
            # 与 run_pipeline 同语义：桩实现走不到时按 skipped 处理，
            # 不计入分母，但保留在 outcomes 与报告 cases 数组里
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=False,
                    reasons=[],
                    observed={"skipped": "桩实现走不到该分支"},
                    skipped=True,
                )
            )
            continue
        session_id = f"eval-{case['id']}"
        result = _invoke(container, session_id, case["question"], None)

        freshness = result.get("data_freshness")
        observed = {
            "status": result.get("status"),
            "row_count": result.get("row_count") or 0,
            "source_tables": list(result.get("source_tables") or []),
            "freshness": freshness,
        }
        reasons: list[str] = []

        if expect.get("status") and result.get("status") != expect["status"]:
            reasons.append(
                f"终态：期望 {expect['status']}，实际 {result.get('status')}"
            )
        min_rows = expect.get("min_rows")
        if min_rows is not None and (result.get("row_count") or 0) < min_rows:
            reasons.append(
                f"行数：期望 ≥ {min_rows}，实际 {result.get('row_count') or 0}"
            )

        want_tables = expect.get("source_tables")
        if want_tables is not None:
            missing = _has_source_tables(observed["source_tables"], list(want_tables))
            if missing:
                reasons.append(
                    f"来源表：期望含 {sorted(want_tables)}，"
                    f"实际 {observed['source_tables']}（缺 {missing}）"
                )

        if expect.get("freshness_present"):
            if not isinstance(freshness, dict):
                reasons.append("数据时效：期望为 dict，实际缺失或类型不符")
            else:
                for key in ("orders_cutoff", "refunds_cutoff", "threshold_days"):
                    if key not in freshness:
                        reasons.append(f"数据时效：缺少字段 {key}")

        if expect.get("freshness_orders_cutoff_iso"):
            orders_cutoff = (freshness or {}).get("orders_cutoff") if isinstance(freshness, dict) else None
            if not _is_iso_date(orders_cutoff):
                reasons.append(
                    f"数据时效.orders_cutoff：期望 ISO 日期（YYYY-MM-DD），实际 {orders_cutoff!r}"
                )

        if expect.get("freshness_refunds_cutoff_iso"):
            refunds_cutoff = (freshness or {}).get("refunds_cutoff") if isinstance(freshness, dict) else None
            if not _is_iso_date(refunds_cutoff):
                reasons.append(
                    f"数据时效.refunds_cutoff：期望 ISO 日期（YYYY-MM-DD），实际 {refunds_cutoff!r}"
                )

        if "time_range_consistent" in expect:
            observed["time_range_consistent"] = result.get("time_range_consistent")
            if result.get("time_range_consistent") != expect["time_range_consistent"]:
                reasons.append(
                    "时间区间一致性：期望 "
                    f"{expect['time_range_consistent']}，实际 {result.get('time_range_consistent')}"
                )

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
# baseline 组（FR-M-1 至 FR-M-3）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _count_queries(repo: Any) -> Any:
    """统计一轮里 SQL 的实际执行次数。

    基准计算会让同一轮多跑最多三次语句（本期标量、环比、同比）。这项开销
    直接计入 FR-J-3 的 p95 延迟，因此做成断言而不是写在文档里的承诺：
    口径改坏了导致查询次数翻倍时，这里有反馈。
    """
    counter = {"n": 0}
    original = repo.execute_readonly

    def wrapped(sql: str, *args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        return original(sql, *args, **kwargs)

    repo.execute_readonly = wrapped
    try:
        yield counter
    finally:
        del repo.__dict__["execute_readonly"]


def run_baseline(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """对比基准断言。

    与 pipeline 走同一张图，只看基准相关字段。断言分三类：
      1. 给没给（mom / yoy / note）——FR-M-1 要求「无法计算时输出无对比期数据」
      2. 能不能用（comparable / deviation）——FR-M-2 要求不完整周期不参与结论
      3. 按不按规则（granularity / missing_contains）——FR-M-3 的规则表

    时间断言不写绝对日期：链路里的「今天」取系统日期，写死 2026-08 会在
    下个月整组变红。改为断言结构与状态，只有粒度、标记与文案是确定的。
    """
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        expect = case.get("expect") or {}
        if mock and case.get("requires_llm"):
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=False,
                    reasons=[],
                    observed={"skipped": "桩实现走不到该分支"},
                    skipped=True,
                )
            )
            continue

        session_id = f"eval-{case['id']}"
        with _count_queries(container.repo) as counter:
            result = _invoke(container, session_id, case["question"], None)
        query_count = counter["n"]

        baselines = result.get("baselines") or {}
        mom = baselines.get("mom")
        yoy = baselines.get("yoy")
        observed = {
            "status": result.get("status"),
            "row_count": result.get("row_count") or 0,
            "granularity": result.get("granularity"),
            "partial_period": bool(result.get("partial_period")),
            "note": baselines.get("note") or "",
            "current_value": baselines.get("current_value"),
            "mom": mom,
            "yoy": yoy,
            "missing_reasons": list(baselines.get("missing_reasons") or []),
            "query_count": query_count,
        }
        reasons: list[str] = []

        if expect.get("status") and observed["status"] != expect["status"]:
            reasons.append(
                f"终态：期望 {expect['status']}，实际 {observed['status']}"
            )
        min_rows = expect.get("min_rows")
        if min_rows is not None and observed["row_count"] < min_rows:
            reasons.append(f"行数：期望 ≥ {min_rows}，实际 {observed['row_count']}")

        if "granularity" in expect and observed["granularity"] != expect["granularity"]:
            reasons.append(
                f"粒度：期望 {expect['granularity']}，实际 {observed['granularity']}"
            )
        if "partial_period" in expect and observed["partial_period"] != bool(
            expect["partial_period"]
        ):
            reasons.append(
                f"本期完整度：期望 partial_period={expect['partial_period']}，"
                f"实际 {observed['partial_period']}"
            )
        for key, actual in (("mom_present", mom is not None), ("yoy_present", yoy is not None)):
            if key in expect and bool(expect[key]) != actual:
                reasons.append(f"{key}：期望 {expect[key]}，实际 {actual}")

        if "mom_comparable" in expect:
            if mom is None:
                reasons.append(
                    f"mom_comparable：期望 {expect['mom_comparable']}，但本轮没有环比可判定"
                )
            elif bool(mom.get("comparable")) != bool(expect["mom_comparable"]):
                reasons.append(
                    f"mom.comparable：期望 {expect['mom_comparable']}，实际 {mom.get('comparable')}"
                )
            if mom is not None and expect["mom_comparable"] is False and mom.get("deviation") is not None:
                reasons.append(
                    f"mom.deviation：不完整周期下应为空，实际 {mom.get('deviation')}"
                )

        if "note" in expect and observed["note"] != expect["note"]:
            reasons.append(f"note：期望「{expect['note']}」，实际「{observed['note']}」")

        for needle in expect.get("missing_contains") or []:
            if not any(needle in r for r in observed["missing_reasons"]):
                reasons.append(
                    f"missing_reasons：未出现「{needle}」，实际 {observed['missing_reasons']}"
                )

        max_queries = expect.get("max_queries")
        if max_queries is not None and query_count > max_queries:
            reasons.append(f"查询次数：期望 ≤ {max_queries}，实际 {query_count}")

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


def run_drilldown(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """下钻与异常标记断言（FR-L-1 至 FR-L-3）。

    一条用例做两件事：按 trigger 构造下钻问句并断言它「只在上一轮槽位上加深
    一层」，再把问句丢回既有链路跑一遍，确认拼出来的句子真的能查到东西。

    触发点分两种：finding 走「加深一层」，cell 走「同口径 + 维度值过滤」，
    后者不加深，因此 next_dimension 期望为空。没有 trigger 的用例只跑链路，
    用来断言 findings 的结构与判定规则。
    """
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        expect = case.get("expect") or {}
        trigger = case.get("trigger") or {}
        reasons: list[str] = []
        question = case.get("question") or ""
        observed: dict[str, Any] = {}

        if trigger:
            plan = build_drilldown(
                source=trigger.get("source", "finding"),
                metric=trigger.get("metric"),
                dimension=trigger.get("dimension"),
                slice_value=trigger.get("slice"),
                catalog=container.catalog,
                time_label=trigger.get("time_label"),
            )
            question = plan.question
            observed = {
                "question": plan.question,
                "next_dimension": plan.next_dimension,
                "available": plan.available,
                "reason": plan.reason,
            }
            if "question" in expect and plan.question != expect["question"]:
                reasons.append(f"问句：期望「{expect['question']}」，实际「{plan.question}」")
            if "next_dimension" in expect and plan.next_dimension != expect["next_dimension"]:
                reasons.append(
                    f"加深维度：期望 {expect['next_dimension']}，实际 {plan.next_dimension}"
                )
            if "available" in expect and plan.available != bool(expect["available"]):
                reasons.append(f"可用：期望 {expect['available']}，实际 {plan.available}")
            if expect.get("only_one_level") and plan.next_dimension:
                # 只加深一层的可观测形式：新维度不等于当前维度，且问句里只出现一次「按…分布」
                if plan.next_dimension == trigger.get("dimension"):
                    reasons.append(
                        f"只加深一层：加深后的维度仍是 {plan.next_dimension}，没有前进"
                    )
                if plan.question.count("分布") > 1:
                    reasons.append("只加深一层：问句里出现多处「分布」，疑似一次加深了多层")

        if mock and case.get("requires_llm"):
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=question or case.get("question", ""),
                    intent=case.get("intent", ""),
                    passed=False,
                    reasons=[],
                    observed={**observed, "skipped": "桩实现走不到该分支"},
                    skipped=True,
                )
            )
            continue

        run_pipeline_check = any(
            key in expect
            for key in ("status", "min_rows", "findings_min", "findings_rules",
                        "findings_absent", "findings_fields", "deepens_to")
        )
        if question and run_pipeline_check:
            session_id = f"eval-{case['id']}"
            result = _invoke(container, session_id, question, None)
            findings = result.get("findings") or []
            observed.update(
                {
                    "status": result.get("status"),
                    "row_count": result.get("row_count") or 0,
                    "columns": result.get("columns") or [],
                    "findings_count": len(findings),
                    "findings_rules": sorted({f.get("rule") for f in findings if f.get("rule")}),
                }
            )
            if expect.get("status") and observed["status"] != expect["status"]:
                reasons.append(f"终态：期望 {expect['status']}，实际 {observed['status']}")
            min_rows = expect.get("min_rows")
            if min_rows is not None and observed["row_count"] < min_rows:
                reasons.append(f"行数：期望 ≥ {min_rows}，实际 {observed['row_count']}")
            if expect.get("findings_min") is not None and len(findings) < expect["findings_min"]:
                reasons.append(
                    f"异常标记：期望 ≥ {expect['findings_min']} 条，实际 {len(findings)} 条"
                )
            for rule in expect.get("findings_rules") or []:
                if rule not in {f.get("rule") for f in findings}:
                    reasons.append(
                        f"判定规则：未出现 rule={rule}，实际 {observed['findings_rules']}"
                    )
            for rule in expect.get("findings_absent") or []:
                if rule in {f.get("rule") for f in findings}:
                    reasons.append(
                        f"判定规则：不应出现 rule={rule}，实际 {observed['findings_rules']}"
                    )
            for field in expect.get("findings_fields") or []:
                if findings and any(field not in f for f in findings):
                    reasons.append(f"字段缺失：findings 中有项缺 {field}")
            for needle in expect.get("deepens_to") or []:
                if not any(needle in c for c in observed["columns"]):
                    reasons.append(f"结果列：未出现「{needle}」，实际 {observed['columns']}")

        outcomes.append(
            CaseOutcome(
                case_id=case["id"],
                question=question or case.get("question", ""),
                intent=case.get("intent", ""),
                passed=not reasons,
                reasons=reasons,
                observed=observed,
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# robust 组（FR-B-3 结论数字核对 / FR-D-5 时间区间 / FR-I-3 脱敏）
# ---------------------------------------------------------------------------


def run_robust(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """健壮性核对断言。

    图本身只产出 number_checks / time_range_consistent；脱敏管道在接入层
    （chat.py），评测在这里显式跑一遍 mask_result，与线上 SSE / sync 同一
    函数、同一点位，避免另写脱敏逻辑造成评测与生产分叉。
    """
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        expect = case.get("expect") or {}
        if mock and case.get("requires_llm"):
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=False,
                    reasons=[],
                    observed={"skipped": "桩实现走不到该分支"},
                    skipped=True,
                )
            )
            continue

        result = _invoke(container, f"eval-{case['id']}", case["question"], None)
        # 与 chat 接入层相同：先拿图终态，再过脱敏管道。
        mask_result(result, container.catalog)

        pending = list(result.get("number_checks") or [])
        masked_fields = list(result.get("masked_fields") or [])
        observed = {
            "status": result.get("status"),
            "row_count": result.get("row_count") or 0,
            "number_pending": len(pending),
            "number_checks": pending,
            "time_range_consistent": result.get("time_range_consistent"),
            "masked": bool(result.get("masked")),
            "masked_fields": masked_fields,
        }
        reasons: list[str] = []

        if expect.get("status") and observed["status"] != expect["status"]:
            reasons.append(f"终态：期望 {expect['status']}，实际 {observed['status']}")
        min_rows = expect.get("min_rows")
        if min_rows is not None and observed["row_count"] < min_rows:
            reasons.append(f"行数：期望 ≥ {min_rows}，实际 {observed['row_count']}")
        if expect.get("number_pending_min") is not None:
            n = int(expect["number_pending_min"])
            if len(pending) < n:
                reasons.append(f"待查数字：期望 ≥ {n} 条，实际 {len(pending)} 条 {pending}")
        if expect.get("number_clean") and pending:
            reasons.append(f"不应出现待查数字，实际 {len(pending)} 条：{pending}")
        if "time_range_consistent" in expect:
            if observed["time_range_consistent"] != expect["time_range_consistent"]:
                reasons.append(
                    "时间区间一致性：期望 "
                    f"{expect['time_range_consistent']}，实际 {observed['time_range_consistent']}"
                )
        if "masked" in expect and observed["masked"] != expect["masked"]:
            reasons.append(f"脱敏标记：期望 {expect['masked']}，实际 {observed['masked']}")
        wanted_fields = expect.get("masked_fields_any")
        if wanted_fields:
            if not any(any(want in str(f) for want in wanted_fields) for f in masked_fields):
                reasons.append(
                    f"脱敏列：期望含 {wanted_fields} 任一，实际 {masked_fields}"
                )

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
# multiturn 组（FR-A-1 至 FR-A-5）
# ---------------------------------------------------------------------------


def run_multiturn(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """多轮改写、排序/TopN、数值过滤、槽位结构化断言。

    桩模式下 _mock_route_decision 按关键词填充 slots，可断言槽位结构
    与 SQL 关键子串。requires_llm=true 的条目在桩模式按 skipped 处理。
    """
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        expect = case.get("expect") or {}
        if mock and case.get("requires_llm"):
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=False,
                    reasons=[],
                    observed={"skipped": "桩实现走不到该分支"},
                    skipped=True,
                )
            )
            continue

        session_id = f"eval-{case['id']}"
        result = _invoke(container, session_id, case["question"], None)

        status = result.get("status")
        sql = result.get("sql") or ""
        slots = result.get("slots") or {}
        observed = {
            "status": status,
            "row_count": result.get("row_count") or 0,
            "sql": sql,
            "slots": slots,
        }
        reasons: list[str] = []

        if expect.get("status") and status != expect["status"]:
            reasons.append(f"终态：期望 {expect['status']}，实际 {status}")
        min_rows = expect.get("min_rows")
        if min_rows is not None and (result.get("row_count") or 0) < min_rows:
            reasons.append(f"行数：期望 ≥ {min_rows}，实际 {result.get('row_count') or 0}")

        # SQL 关键子串
        want_contains = expect.get("sql_contains")
        if want_contains and status == "ok":
            missing = _sql_contains(sql, want_contains)
            if missing:
                reasons.append(f"关键子串：SQL 未包含 {missing}")

        # FR-A-5：槽位断言
        if status == "ok":
            want_topn = expect.get("top_n")
            if want_topn is not None and slots.get("top_n") != want_topn:
                reasons.append(
                    f"slots.top_n：期望 {want_topn}，实际 {slots.get('top_n')}"
                )

            want_metrics_any = expect.get("slots_metrics_any")
            if want_metrics_any:
                got_metrics = [str(m) for m in (slots.get("metrics") or [])]
                if not any(w in got_metrics for w in want_metrics_any):
                    reasons.append(
                        f"slots.metrics：期望含 {want_metrics_any} 之一，实际 {got_metrics}"
                    )

            want_dims_any = expect.get("slots_dimensions_any")
            if want_dims_any:
                got_dims = [str(d) for d in (slots.get("dimensions") or [])]
                if not any(w in got_dims for w in want_dims_any):
                    reasons.append(
                        f"slots.dimensions：期望含 {want_dims_any} 之一，实际 {got_dims}"
                    )

            want_filters_any = expect.get("slots_filters_any")
            if want_filters_any:
                got_filters_str = json.dumps(slots.get("filters") or [], ensure_ascii=False)
                missing_filters = [
                    f for f in want_filters_any
                    if not all(str(v) in got_filters_str for v in f.values())
                ]
                if missing_filters:
                    reasons.append(
                        f"slots.filters：期望含 {missing_filters} 之一，"
                        f"实际 {slots.get('filters')}"
                    )

            want_time = expect.get("slots_time_range")
            if want_time is not None and slots.get("time_range") != want_time:
                reasons.append(
                    f"slots.time_range：期望「{want_time}」，实际「{slots.get('time_range')}」"
                )

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
# advanced 组（FR-J-1 窗口函数与 CTE）
# ---------------------------------------------------------------------------


def run_advanced(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """窗口函数与 CTE 高级用例断言（FR-J-1）。

    桩模板不覆盖窗口函数与 CTE，全部标 requires_llm=true，桩模式下按
    skipped 处理。切到真实模型后自动生效。
    """
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        expect = case.get("expect") or {}
        if mock and case.get("requires_llm"):
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=False,
                    reasons=[],
                    observed={"skipped": "桩实现走不到该分支"},
                    skipped=True,
                )
            )
            continue

        session_id = f"eval-{case['id']}"
        result = _invoke(container, session_id, case["question"], None)

        status = result.get("status")
        sql = result.get("sql") or ""
        observed = {
            "status": status,
            "row_count": result.get("row_count") or 0,
            "sql": sql,
        }
        reasons: list[str] = []

        if expect.get("status") and status != expect["status"]:
            reasons.append(f"终态：期望 {expect['status']}，实际 {status}")
        min_rows = expect.get("min_rows")
        if min_rows is not None and (result.get("row_count") or 0) < min_rows:
            reasons.append(f"行数：期望 ≥ {min_rows}，实际 {result.get('row_count') or 0}")

        want_contains = expect.get("sql_contains")
        if want_contains and status == "ok":
            missing = _sql_contains(sql, want_contains)
            if missing:
                reasons.append(f"关键子串：SQL 未包含 {missing}")

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
# alert 组（FR-O-1 至 FR-O-5）
# ---------------------------------------------------------------------------

_ALERT_EVAL_AT = datetime(2026, 9, 15, 10, 0, 0)


def run_alert(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """异常订阅求值断言。

    每条用例在 case.rule 里给规则定义，runner 负责：落规则 → 求值 → 断言 →
    删规则。求值时刻固定（与评测时间锚点同一天），保证报告可复现；同一用例
    再求一次用于验证 FR-O-3 幂等。
    """
    outcomes: list[CaseOutcome] = []
    for case in cases:
        expect = case.get("expect") or {}
        rule_def = case.get("rule") or {}
        rule = AlertRuleRecord(
            metric=str(rule_def.get("metric", "")),
            dimensions=list(rule_def.get("dimensions") or []),
            comparison=str(rule_def.get("comparison", "gt")),
            threshold=float(rule_def.get("threshold", 0)),
            severity=str(rule_def.get("severity", "notice")),
        )
        saved = container.store.upsert_alert_rule(rule)
        try:
            alert = evaluate_rule(container, saved, _ALERT_EVAL_AT)
            again = evaluate_rule(container, saved, _ALERT_EVAL_AT)
            observed = {
                "status": alert.status,
                "hit": alert.hit,
                "reason": alert.reason,
                "hits": alert.hits,
                "first_id": alert.id,
                "second_id": again.id,
                "idempotent": alert.id == again.id and alert.id != 0,
            }
            reasons: list[str] = []
            if expect.get("eval_status") and alert.status != expect["eval_status"]:
                reasons.append(
                    f"求值状态：期望 {expect['eval_status']}，实际 {alert.status}"
                    f"（reason={alert.reason}）"
                )
            if "hit" in expect and alert.hit != expect["hit"]:
                reasons.append(f"命中：期望 {expect['hit']}，实际 {alert.hit}")
            if expect.get("idempotent") and not observed["idempotent"]:
                reasons.append(
                    f"幂等失败：同周期两次求值产生不同行 {alert.id} / {again.id}"
                )
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case.get("question", ""),
                    intent="alert",
                    passed=not reasons,
                    reasons=reasons,
                    observed=observed,
                )
            )
        finally:
            container.store.delete_alert_rule(saved.id)
    return outcomes


# ---------------------------------------------------------------------------
# outofscope 组（域外 / 归因两类终态）
# ---------------------------------------------------------------------------


def run_outofscope(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """答非所问的两类终态断言。

    与 pipeline 相反的一组：本组验的是「系统什么时候不该给数」。这两类问题
    此前会被当成普通取数处理，生成一条语法正确、能执行、有图表的 SQL，
    用户拿到一个与自己问题无关的数字且全程无报错——是所有失败形态里最难
    被发现的一种。

    桩模式下这两个分支由 gateway 的关键词表触发，真实模式下由 route 节点
    判定，因此本组在两种模式下都断言，不标 requires_llm。
    """
    outcomes: list[CaseOutcome] = []
    for case in cases:
        expect = case.get("expect") or {}
        result = _invoke(container, f"eval-{case['id']}", case["question"], None)

        status = result.get("status")
        intent = result.get("intent")
        sql = (result.get("sql") or "").strip()
        answer = result.get("answer") or ""
        observed = {
            "status": status,
            "intent": intent,
            "sql": sql,
            "answer": answer,
            "stages": [s.get("node") for s in result.get("stages") or []],
        }
        reasons: list[str] = []

        if "status" in expect and status != expect["status"]:
            reasons.append(f"终态：期望 {expect['status']}，实际 {status}")
        if "intent" in expect and intent != expect["intent"]:
            reasons.append(f"意图：期望 {expect['intent']}，实际 {intent}")
        if expect.get("no_sql") and sql:
            reasons.append(
                "本轮不该生成语句，实际生成了 SQL（问题会被当成取数回答）："
                + " ".join(sql.split())[:120]
            )
        missing = [n for n in (expect.get("answer_contains") or []) if n not in answer]
        if missing:
            reasons.append(f"答案：未出现 {missing}")

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
# slotcoverage 组（槽位覆盖核对）
# ---------------------------------------------------------------------------


def run_slotcoverage(cases: list[dict[str, Any]], container: Any) -> list[CaseOutcome]:
    """route 抽出的槽位有没有落进 SQL。

    这一组验的是「语句合法、口径也对，但答的不是问的那件事」能否被看出来。
    它不阻断执行（假告警会把正确结果改成放弃），所以断言的是核对结果本身
    是否准确，而不是终态。

    桩实现的语句由语义层模板派生，不带时间谓词也不带区域过滤，因此同一问句
    在两种模式下的缺口集合不同：桩模式下的期望值标 mock_only，真实模式下
    按 skipped 处理，避免用一个模式的数字去衡量另一个模式。
    """
    outcomes: list[CaseOutcome] = []
    mock = bool(container.gateway.mock)
    for case in cases:
        if not mock and case.get("mock_only"):
            outcomes.append(
                CaseOutcome(
                    case_id=case["id"],
                    question=case["question"],
                    intent=case.get("intent", ""),
                    passed=False,
                    reasons=[],
                    observed={"skipped": "该断言只在桩模式下成立"},
                    skipped=True,
                )
            )
            continue

        expect = case.get("expect") or {}
        result = _invoke(container, f"eval-{case['id']}", case["question"], None)
        gaps = result.get("slot_gaps") or []
        kinds = sorted({str(g.get("kind")) for g in gaps})
        want = sorted({str(k) for k in (expect.get("slot_gap_kinds") or [])})

        reasons: list[str] = []
        if kinds != want:
            reasons.append(f"槽位缺口：期望 {want or '无'}，实际 {kinds or '无'}")

        outcomes.append(
            CaseOutcome(
                case_id=case["id"],
                question=case["question"],
                intent=case.get("intent", ""),
                passed=not reasons,
                reasons=reasons,
                observed={"slot_gaps": gaps, "kinds": kinds, "sql": result.get("sql") or ""},
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


def _outcome_to_dict(outcome: CaseOutcome) -> dict[str, Any]:
    """把 CaseOutcome 序列化为报告条目。

    skipped 字段提到外层（不再藏进 observed），便于消费方一眼区分。
    """
    return {
        "id": outcome.case_id,
        "question": outcome.question,
        "intent": outcome.intent,
        "passed": outcome.passed,
        "skipped": outcome.skipped,
        "reasons": outcome.reasons,
        "observed": outcome.observed,
    }


def _assert_invariants(group_name: str, summary: dict[str, Any], outcomes: list[CaseOutcome]) -> None:
    """校验报告自洽性：cases 长度 == total + skipped（FR-K-2）。

    避免「分母里少一条、cases 数组里多一条」再次发生。
    """
    cases_len = len(outcomes)
    total = summary["total"]
    skipped = summary.get("skipped", 0)
    if cases_len != total + skipped:
        raise SystemExit(
            f"{group_name} 组报告自洽校验失败：cases 长度 {cases_len} "
            f"≠ total {total} + skipped {skipped}"
        )


def _default_today(meta: dict[str, Any]) -> tuple[date, str]:
    """默认时间锚点：库内 data_as_of；取不到时退回评测集 reference_today。

    与生产一致：问答里的相对时间词按数据截止日换算，评测要验的就是这条路径。
    库不可用（未造数）或两表都空时退回 reference_today，此时结论只对编写
    用例时的数据形态有效，跑出来的报告会带上来源，不会和线上行为混淆。
    """
    try:
        as_of = SqliteQueryRepository().data_as_of()
    except Exception:  # noqa: BLE001 - 库缺失时不该挡住 recall 组的纯配置核对
        as_of = None
    if as_of is not None:
        return as_of, "库内 data_as_of"
    ref = meta.get("reference_today")
    if not ref:
        raise SystemExit(
            "未指定 --today，业务库也取不到 data_as_of，评测集没有 reference_today 可退回"
        )
    return date.fromisoformat(str(ref)), "评测集 reference_today"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑评测集")
    parser.add_argument(
        "--group",
        choices=(
            "recall", "pipeline", "safety", "datalineage", "baseline",
            "drilldown", "robust", "alert", "multiturn", "advanced",
            "outofscope", "slotcoverage", "all",
        ),
        default="all",
        help="只跑某一组，默认全跑",
    )
    parser.add_argument(
        "--today",
        help="时间锚点 YYYY-MM-DD，默认取库内 data_as_of（取不到时退回评测集 reference_today）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印每条的观察值")
    parser.add_argument("--output", default=str(REPORT_PATH), help="报告落盘路径")
    args = parser.parse_args(argv)

    suite = load_cases()
    meta = suite.get("meta") or {}
    if args.today:
        today = date.fromisoformat(args.today)
        today_source = "--today 参数"
    else:
        today, today_source = _default_today(meta)

    print(f"评测集：{meta.get('name')}")
    print(
        f"时间锚点：{today.isoformat()}（{today_source}）"
        + (f"（{meta.get('reference_weekday', '')}）" if meta.get("reference_weekday") else "")
    )
    print(f"评测时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    groups = suite.get("groups") or {}
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reference_today": today.isoformat(),
        "llm_mock": None,
        "groups": {},
    }

    need_recall = args.group in ("recall", "all")
    need_graph = args.group in (
        "pipeline", "safety", "datalineage", "baseline",
        "drilldown", "robust", "alert", "multiturn", "advanced", "outofscope",
        "slotcoverage", "all",
    )

    container = None
    if need_graph:
        container = build_container()
        reset_telemetry()
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
            "cases": [_outcome_to_dict(o) for o in outcomes],
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
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("pipeline", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("safety", "all"):
        assert container is not None
        cases = groups.get("safety", {}).get("cases") or []
        outcomes = run_safety(cases, container)
        print_outcomes(f"safety　{groups.get('safety', {}).get('description', '')}", outcomes, args.verbose)
        summary = print_summary_simple("\n危险语句拦截率", outcomes)
        report["groups"]["safety"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("safety", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("datalineage", "all"):
        assert container is not None
        cases = groups.get("datalineage", {}).get("cases") or []
        outcomes = run_dataline(cases, container)
        print_outcomes(
            f"datalineage　{groups.get('datalineage', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n数据血缘字段完整率", outcomes)
        report["groups"]["datalineage"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("datalineage", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("baseline", "all"):
        assert container is not None
        cases = groups.get("baseline", {}).get("cases") or []
        outcomes = run_baseline(cases, container)
        print_outcomes(
            f"baseline　{groups.get('baseline', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n对比基准判定率", outcomes)
        report["groups"]["baseline"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("baseline", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("drilldown", "all"):
        assert container is not None
        cases = groups.get("drilldown", {}).get("cases") or []
        outcomes = run_drilldown(cases, container)
        print_outcomes(
            f"drilldown　{groups.get('drilldown', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n下钻与异常标记判定率", outcomes)
        report["groups"]["drilldown"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("drilldown", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("robust", "all"):
        assert container is not None
        cases = groups.get("robust", {}).get("cases") or []
        outcomes = run_robust(cases, container)
        print_outcomes(
            f"robust　{groups.get('robust', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n核对与脱敏完整率", outcomes)
        report["groups"]["robust"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("robust", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("alert", "all"):
        assert container is not None
        cases = groups.get("alert", {}).get("cases") or []
        outcomes = run_alert(cases, container)
        print_outcomes(
            f"alert　{groups.get('alert', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n异常订阅判定率", outcomes)
        report["groups"]["alert"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("alert", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("multiturn", "all"):
        assert container is not None
        cases = groups.get("multiturn", {}).get("cases") or []
        outcomes = run_multiturn(cases, container)
        print_outcomes(
            f"multiturn　{groups.get('multiturn', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n多轮改写与槽位判定率", outcomes)
        report["groups"]["multiturn"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("multiturn", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("advanced", "all"):
        assert container is not None
        cases = groups.get("advanced", {}).get("cases") or []
        outcomes = run_advanced(cases, container)
        print_outcomes(
            f"advanced　{groups.get('advanced', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n窗口函数与 CTE 判定率", outcomes)
        report["groups"]["advanced"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("advanced", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("outofscope", "all"):
        assert container is not None
        cases = groups.get("outofscope", {}).get("cases") or []
        outcomes = run_outofscope(cases, container)
        print_outcomes(
            f"outofscope　{groups.get('outofscope', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n超范围与归因判定率", outcomes)
        report["groups"]["outofscope"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("outofscope", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if args.group in ("slotcoverage", "all"):
        assert container is not None
        cases = groups.get("slotcoverage", {}).get("cases") or []
        outcomes = run_slotcoverage(cases, container)
        print_outcomes(
            f"slotcoverage　{groups.get('slotcoverage', {}).get('description', '')}",
            outcomes,
            args.verbose,
        )
        summary = print_summary_simple("\n槽位覆盖核对准确率", outcomes)
        report["groups"]["slotcoverage"] = {
            **summary,
            "cases": [_outcome_to_dict(o) for o in outcomes],
        }
        _assert_invariants("slotcoverage", summary, outcomes)
        failures += summary["total"] - summary["passed"]

    if container is not None:
        # FR-J-3：报告顶层的进程级遥测。延迟分位来自所有图调用，token /
        # 成本来自网关累计，对比期查询次数来自基准节点计数；桩模式 token=0
        # 但字段结构必须在，切真实模型后无需改报告消费方。
        totals = container.gateway.totals()
        report["telemetry"] = {
            "samples": len(_LATENCIES),
            "query_latency_ms": {
                "p50": percentile(_LATENCIES, 50),
                "p95": percentile(_LATENCIES, 95),
                "avg": round(sum(_LATENCIES) / len(_LATENCIES), 1) if _LATENCIES else None,
                "max": round(max(_LATENCIES), 1) if _LATENCIES else None,
            },
            "llm_calls": totals["calls"],
            "prompt_tokens": totals["prompt_tokens"],
            "completion_tokens": totals["completion_tokens"],
            "estimated_cost": totals["estimated_cost"],
            "comparison_queries": counters().comparison_queries,
        }
        print(
            "\n遥测："
            f"p50 {report['telemetry']['query_latency_ms']['p50']} ms / "
            f"p95 {report['telemetry']['query_latency_ms']['p95']} ms；"
            f"token {totals['prompt_tokens']}+{totals['completion_tokens']}；"
            f"对比期查询 {counters().comparison_queries} 次"
        )

    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n报告已写入 {args.output}")
    print(f"未通过 {failures} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
