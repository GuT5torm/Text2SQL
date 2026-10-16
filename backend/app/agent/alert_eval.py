"""FR-O 异常订阅的规则求值。

核心约束（FRD 4.14）：求值必须**复用现有图**，不能另写一套 SQL——订阅算出的
退款率与用户提问看到的退款率必须是同一个数，两套实现迟早分叉。因此本模块只做
三件事：

1. 把规则拼成一句自然语言（同一口径、同一语义层）；
2. 走 graph.invoke（同一守卫、同一超时、同一脱敏前的数值口径）；
3. 按 gt / lt / deviation 比对结果集，落一条求值记录。

FR-O-5：守卫拒绝、超时、口径不成立、图本身抛错，一律记 **status=error**，
绝不写成「未命中」；只有「查询成功但没有任何切片越线」才是 ok + hit=false。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from app.agent.checkpointer import thread_config
from app.agent.graph import summarize_result
from app.agent.state import initial_state
from app.api.deps import Container
from app.db.repository import AlertRecord, AlertRuleRecord

logger = logging.getLogger(__name__)

_COMPARISON_TEXT = {"gt": "高于", "lt": "低于", "deviation": "偏离度超过"}
_VALID_COMPARISONS = {"gt", "lt", "deviation"}

# 规则里没有时间窗字段，而单独一个指标名不成句：「成交总额」缺少时间范围，
# 走到 route 节点会被判成 clarify，FR-O-5 随即记为 error。桩实现不带时间谓词
# 也能出语句，所以这个缺陷只在真实模型下暴露。合成问句统一补一个完整的自然
# 周期，既让取数成立，也让 baselines 有可比对的完整本期（月初至今会被判
# partial_period 而不能参与环比）。
DEFAULT_PERIOD_TEXT = "上个月"


class AlertRuleError(ValueError):
    """规则本身无法求值（指标 / 维度 / 比对方式未登记）。记 error 而不是 404。"""


def quantize_eval_at(moment: dt.datetime, period_minutes: int) -> dt.datetime:
    """把求值时刻量化到调度周期起点，保证周期内重复调用幂等。

    外部计划任务可能重试，同一小时内的多次 POST /api/alert/check 必须落到
    同一个 evaluated_at，与 alerts 的 UNIQUE(rule_id, evaluated_at) 配合。
    """
    period = max(int(period_minutes), 1)
    floored = (moment.minute // period) * period
    return moment.replace(minute=floored, second=0, microsecond=0)


def _display_of(catalog: Any, kind: str, name: str) -> str:
    """指标 / 维度物理名 → 显示名；传入的本身就是显示名时原样返回。"""
    index = catalog.metric_by_name if kind == "metric" else catalog.dimension_by_name
    entry = index.get(name)
    if entry:
        return str(entry.get("display") or name)
    # 容忍前端直接传显示名
    displays = {e.get("display") for e in (catalog.metrics if kind == "metric" else catalog.dimensions)}
    if name in displays:
        return name
    if kind == "metric" and catalog.metric_name_by_display.get(name):
        return name
    raise AlertRuleError(f"{'指标' if kind == 'metric' else '维度'}未在语义层登记：{name}")


def build_question(catalog: Any, rule: AlertRuleRecord) -> tuple[str, str, list[str]]:
    """合成问句。

    返回 (问句, 指标显示名, 维度显示名列表)。整体口径（无维度）时问句里不加
    「各」，避免误导生成器拼 GROUP BY。

    两种情况都会带上 DEFAULT_PERIOD_TEXT。规则本身不保存时间窗，缺了它问句
    就只有一个指标名，route 会据此判为信息不足，偏离度类订阅永远拿不到
    baselines，gt / lt 类订阅也会在区间未定这件事上反问一句。
    """
    if rule.comparison not in _VALID_COMPARISONS:
        raise AlertRuleError(f"不支持的比对方式：{rule.comparison}")
    metric_display = _display_of(catalog, "metric", rule.metric)
    dim_displays = [_display_of(catalog, "dimension", d) for d in rule.dimensions]
    if dim_displays:
        question = f"{DEFAULT_PERIOD_TEXT}各{'、'.join(dim_displays)}的{metric_display}"
    else:
        question = f"{DEFAULT_PERIOD_TEXT}的{metric_display}"
    return question, metric_display, dim_displays


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip().replace(",", "").rstrip("%")
        if not text:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _metric_column(columns: list[str], metric_display: str, dim_displays: list[str]) -> str | None:
    """在结果集列里找指标列：优先按指标名命中，否则取第一个非维度的数值列。"""
    for col in columns:
        if str(col) == metric_display or metric_display in str(col):
            return col
    dim_set = {d for d in dim_displays}
    for col in columns:
        if str(col) not in dim_set:
            return col
    return columns[-1] if columns else None


def _slice_of(row: dict[str, Any], columns: list[str], metric_col: str | None, dim_displays: list[str]) -> dict[str, str]:
    """取该行各维度的切片值。维度列优先按显示名匹配，匹配不上按「非指标列」兜底。"""
    slice_cols = [c for c in columns if c != metric_col]
    if dim_displays:
        # 维度数与非指标列一致时按顺序对位（中文别名常与维度显示名不同，
        # 例如维度叫「销售大区」而 SQL 别名是「大区」）。
        if len(slice_cols) == len(dim_displays):
            return {disp: str(row.get(col)) for disp, col in zip(dim_displays, slice_cols) if row.get(col) is not None}
        picked: dict[str, str] = {}
        for disp in dim_displays:
            for col in columns:
                if col == metric_col:
                    continue
                if disp in str(col) or str(col) in disp:
                    picked[disp] = str(row.get(col))
                    break
        return picked
    return {}


def _slice_label(slice_obj: dict[str, str], dim_displays: list[str]) -> str:
    if not slice_obj:
        return "整体"
    if len(slice_obj) == 1:
        return next(iter(slice_obj.values()))
    return "、".join(f"{k}:{v}" for k, v in slice_obj.items())


def _baseline_deviation(result: dict[str, Any]) -> tuple[float | None, str]:
    """整体口径（无维度）时从 baselines 取环比偏离度；取不到视为无法求值。

    优先环比、其次同比；comparable=False 的本期未完整基准不参与判定。
    """
    baselines = result.get("baselines")
    if not isinstance(baselines, dict):
        return None, "缺少对比基准（环比 / 同比），无法判定偏离度"
    for key, label in (("mom", "环比"), ("yoy", "同比")):
        item = baselines.get(key)
        if isinstance(item, dict) and item.get("comparable") and item.get("deviation") is not None:
            return _to_float(item["deviation"]), label
    return None, "对比基准不成立（基准缺失或本期未完整），无法判定偏离度"


def evaluate_rule(
    container: Container,
    rule: AlertRuleRecord,
    evaluated_at: dt.datetime,
) -> AlertRecord:
    """对单条规则求值并（通过 store）幂等落库，返回求值记录。

    evaluated_at 由调用方量化好后传入；同一 (rule_id, evaluated_at) 重复
    求值时存储层直接返回旧记录，接口天然幂等。
    """
    metric = rule.metric

    # FR-O-3 幂等短路：同周期已有记录时直接返回，不重跑图。规则被删除重建
    # 后 SQLite 可能复用自增 id，因此幂等以存储层 (rule_id, evaluated_at)
    # 唯一约束为准，而不是靠 checkpoint 线程身份。
    for prior in container.store.list_alerts(rule_id=rule.id, limit=100):
        if prior.evaluated_at == evaluated_at:
            return prior

    try:
        question, metric_display, dim_displays = build_question(container.catalog, rule)
    except AlertRuleError as exc:
        return _persist(
            container,
            AlertRecord(
                rule_id=rule.id,
                evaluated_at=evaluated_at,
                status="error",
                metric=metric,
                reason=f"口径不成立：{exc}",
            ),
        )

    # 订阅线程不与任何用户会话共享历史，且每次求值独立：规则删建后自增 id
    # 会复用，若 thread_id 只含 rule_id + 周期，MemorySaver 会把上一条规则
    # 的图状态当成本轮 checkpoint 恢复，造成「拿旧答案判新规则」。
    session_id = f"alert-{rule.id}-{evaluated_at:%Y%m%d%H%M}-{uuid.uuid4().hex[:8]}"
    inputs: dict[str, Any] = {
        **initial_state(session_id, question),
        "messages": [HumanMessage(content=question)],
    }

    try:
        final = container.graph.invoke(inputs, config=thread_config(session_id))
        result = summarize_result(final)
    except Exception as exc:  # noqa: BLE001 - 编排层抛错属于求值失败，必须落 error
        logger.exception("规则 %s 求值异常", rule.id)
        return _persist(
            container,
            AlertRecord(
                rule_id=rule.id,
                evaluated_at=evaluated_at,
                status="error",
                metric=metric,
                reason=f"求值执行异常：{exc}",
            ),
        )

    # —— FR-O-5：失败与未命中严格分流 ——
    failure = _failure_reason(result)
    if failure:
        return _persist(
            container,
            AlertRecord(
                rule_id=rule.id,
                evaluated_at=evaluated_at,
                status="error",
                metric=metric,
                reason=failure,
            ),
        )

    columns = [str(c) for c in (result.get("columns") or [])]
    rows = list(result.get("rows") or [])
    metric_col = _metric_column(columns, metric_display, dim_displays)

    hits: list[dict[str, Any]] = []

    if rule.comparison == "deviation":
        if dim_displays:
            # 分组结果没有逐切片基准时不能假装能判偏离——这属于口径不成立。
            return _persist(
                container,
                AlertRecord(
                    rule_id=rule.id,
                    evaluated_at=evaluated_at,
                    status="error",
                    metric=metric,
                    reason="偏离度订阅暂不支持按维度切片（缺少逐切片对比基准）",
                ),
            )
        deviation, label = _baseline_deviation(result)
        if deviation is None:
            return _persist(
                container,
                AlertRecord(
                    rule_id=rule.id,
                    evaluated_at=evaluated_at,
                    status="error",
                    metric=metric,
                    reason=label,
                ),
            )
        if abs(deviation) > rule.threshold / 100.0:
            current = _to_float((result.get("baselines") or {}).get("current_value"))
            hits.append(
                {
                    "slice": None,
                    "value": current,
                    "baseline": {"label": label, "deviation": round(deviation, 6)},
                    "reason": f"{metric_display}{label}偏离 {deviation:+.1%}，超过阈值 {rule.threshold:g}%",
                    "question": question,
                }
            )
    else:
        for row in rows:
            value = _to_float(row.get(metric_col)) if metric_col else None
            if value is None:
                continue
            crossed = value > rule.threshold if rule.comparison == "gt" else value < rule.threshold
            if not crossed:
                continue
            slice_obj = _slice_of(row, columns, metric_col, dim_displays)
            label = _slice_label(slice_obj, dim_displays)
            hits.append(
                {
                    "slice": slice_obj or None,
                    "value": value,
                    "baseline": None,
                    "reason": (
                        f"{label}的{metric_display}为 {_fmt(value)}，"
                        f"{_COMPARISON_TEXT[rule.comparison]}阈值 {_fmt(rule.threshold)}"
                    ),
                    # FR-O-4：一键下钻问句（带切片值），前端直接回填首页提问框
                    "question": f"{label}{metric_display}",
                }
            )

    return _persist(
        container,
        AlertRecord(
            rule_id=rule.id,
            evaluated_at=evaluated_at,
            status="ok",
            hit=bool(hits),
            metric=metric,
            hits=hits,
            reason="" if hits else "未命中",
        ),
    )


def _failure_reason(result: dict[str, Any]) -> str:
    """判定一次图执行是否属于 FR-O-5 的「求值失败」。空字符串表示成功。"""
    if result.get("status") and result["status"] != "ok":
        return f"图执行未成功（status={result['status']}）"
    if result.get("sql_error"):
        # 守卫拒绝（R1-R5/R7/R9）、超时 / 扫描量（R8）都落在 sql_error
        return f"查询未通过：{result['sql_error']}"
    intent = result.get("intent")
    if intent and intent != "query":
        return f"口径不成立：意图被判定为 {intent}，未形成取数查询"
    if not result.get("metric_used"):
        return "口径不成立：未召回任何已登记指标"
    return ""


def _fmt(value: float) -> str:
    """阈值 / 命中值展示：整数不带小数点，其余保留合理精度。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _persist(container: Container, alert: AlertRecord) -> AlertRecord:
    try:
        return container.store.insert_alert_evaluation(alert)
    except Exception:  # noqa: BLE001 - 落库失败不影响把求值结果返回给调用方
        logger.exception("求值记录写入失败 rule=%s at=%s", alert.rule_id, alert.evaluated_at)
        return alert
