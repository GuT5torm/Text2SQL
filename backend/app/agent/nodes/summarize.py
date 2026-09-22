"""summarize 节点：结果集 → 经营结论。

结论只依据本轮结果集，口径边界从语义层召回结果里挑选，不允许模型自行补充。
最后把结论、口径、数据行数组装成 answer，作为一轮回答的正文。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agent.consistency import check_conclusion_numbers
from app.agent.prompts import format_rows, render_prompt
from app.agent.state import AgentState, new_stage
from app.llm.gateway import Gateway, LLMError
from app.llm.schemas import Insight
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

NODE = "summarize"
PREVIEW_N = 20


def _render_baselines(baselines: dict[str, Any] | None) -> str:
    """把基准渲染成 prompt 里的一小段。

    只列出可引用的数字，并显式标出不可引用的情形。模型在 prompt 里看到
    「本期未完整、该基准不参与结论生成」比在事后被校验拦下更可靠——后者
    只能判错，不能给正确的写法。
    """
    if not baselines:
        return "本轮无对比基准（未限定时间范围、本期数据不足或该指标按规则不输出基准）。"

    lines: list[str] = []
    current = baselines.get("current_value")
    period = baselines.get("current_period") or {}
    if current is not None:
        lines.append(
            f"本期（{period.get('start', '?')} 至 {period.get('end', '?')}"
            f"{'，' + period['label'] if period.get('label') else ''}）：{current}"
            + ("（本期未走完）" if baselines.get("partial_period") else "")
        )
    for key in ("mom", "yoy"):
        item = baselines.get(key)
        if not item:
            continue
        deviation = item.get("deviation")
        if deviation is None:
            lines.append(
                f"{item.get('label')}（{item.get('start')} 至 {item.get('end')}）："
                f"{item.get('value')}（偏离度不可用，不参与结论生成）"
            )
        else:
            lines.append(
                f"{item.get('label')}（{item.get('start')} 至 {item.get('end')}）："
                f"{item.get('value')}，偏离 {deviation * 100:.2f}%"
            )
    note = baselines.get("note")
    if note:
        lines.append(f"结论：{note}")
    for reason in baselines.get("missing_reasons") or []:
        lines.append(f"未给出的基准：{reason}")
    return "\n".join(f"- {line}" for line in lines)


def _render_findings(findings: list[dict[str, Any]] | None) -> str:
    """把异常标记渲染成 prompt 里的一小段。

    与基准同一套做法：模型只引用服务端算出的数值与判据。异常判定本身由
    findings.py 完成，prompt 这一节的作用是让结论与界面上的标记说同一件事——
    结论里说「华东偏高」而标记指向东北，比两边各说各话更难解释。
    """
    if not findings:
        return "本轮未触发异常判定（切片数不足、无对比基准或各项均未越过阈值）。"

    lines = []
    for f in findings:
        target = f.get("slice")
        scope = f"{target} 的" if target else "整体"
        metric = f.get("metric") or "该指标"
        lines.append(
            f"- [{f.get('severity')}] {scope}{metric} = {f.get('value')}，"
            f"判据：{f.get('reason')}"
        )
    return "\n".join(lines)


def _render_fix_trace(state: AgentState) -> str:
    """FR-F-2：把本轮修正次数与最后一次修正原因渲染成 prompt 的一节。

    修正过的轮次如果不交代，读者看到的结论与界面上「修正 N 次」的标注对不上，
    也无法判断这个数字是不是出自被换过的语句。
    """
    attempts = int(state.get("retry_count") or 0)
    if attempts <= 0:
        return "本轮 SQL 一次通过，没有发生修正。"
    reason = (state.get("fix_reasoning") or "").strip()
    return (
        f"本轮 SQL 共修正 {attempts} 次后才执行成功。\n"
        f"最后一次修正的原因：{reason or '（未给出原因）'}"
    )


def _fix_sentence(state: AgentState) -> str:
    """结论末尾的修正标注。模型已经写过「修正」时不重复追加。"""
    attempts = int(state.get("retry_count") or 0)
    if attempts <= 0:
        return ""
    reason = (state.get("fix_reasoning") or "").strip()
    tail = f"最后一次修正的原因：{reason}" if reason else "未记录修正原因"
    return f"（本轮查询经过 {attempts} 次自动修正后执行成功，{tail}。）"


def summarize_node(state: AgentState, *, gateway: Gateway) -> dict[str, Any]:
    started = time.monotonic()
    columns = list(state.get("columns") or [])
    rows = list(state.get("rows") or [])
    retrieved = state.get("retrieved") or {}
    question = state.get("question") or ""

    caveats = retrieved.get("caveats") or []
    time_range = (retrieved.get("time_range") or {}).get("resolved") or "未限定，覆盖库内全部数据"

    # FR-B-3：answer 定稿后统一跑数字核对，未命中只登记「待查」，不改正文。
    def _number_checks(answer_text: str) -> list[dict[str, Any]]:
        try:
            return check_conclusion_numbers(
                answer_text,
                rows,
                row_count=len(rows),
                elapsed_ms=state.get("query_elapsed_ms"),
                time_range=retrieved.get("time_range"),
                findings=state.get("findings"),
                baselines=state.get("baselines"),
                retry_count=int(state.get("retry_count") or 0),
            )
        except Exception:  # noqa: BLE001 - 核对失败不影响结论返回
            logger.warning("结论数字核对异常", exc_info=True)
            return []

    if not rows:
        # 空结果不是错误，但必须给出可读解释，否则用户会以为系统坏了。
        # FR-F-1：区分「确认无数据」与「放宽后仍为空」两种结论——前者是有依据的
        # 判定，后者是尽力之后的结果，两者的措辞不能混同。
        if state.get("no_data_confirmed"):
            reason = (
                (state.get("fix_reasoning") or "").strip()
                or "该条件下订单表与退款表均无匹配记录"
            )
            answer = f"确认无数据：{reason}。放宽条件会改变口径，因此按原条件给出这一结论。"
            detail = "判定确认无数据"
        else:
            tried = int(state.get("empty_retry") or 0)
            answer = (
                "查询执行成功，但没有返回数据。可能的原因：筛选条件过严、"
                "所选时间区间内确实没有成交记录，或维度取值与库内实际值不一致。"
            )
            if tried:
                answer += f"已尝试放宽条件重查 {tried} 次，仍未取到数据。"
            answer += "可以换一个时间范围或维度再试。"
            detail = "结果集为空"
        return {
            "insight": answer,
            "highlights": [],
            "answer": answer,
            "status": "ok",
            "number_checks": [],
            "messages": [AIMessage(content=answer)],
            "stages": [new_stage(NODE, "分析结果", detail, _ms(started))],
        }

    prompt = render_prompt(
        "summarize",
        caveats="\n".join(f"- {c}" for c in caveats) or "（本轮未命中已登记指标口径）",
        time_range=time_range,
        baselines=_render_baselines(state.get("baselines")),
        findings=_render_findings(state.get("findings")),
        fix_trace=_render_fix_trace(state),
        question=question,
        columns="、".join(columns),
        preview_n=PREVIEW_N,
        row_count=len(rows),
        rows_preview=format_rows(rows, columns, PREVIEW_N),
        sql=state.get("sql") or "",
    )

    insight: Insight | None = None
    try:
        insight = gateway.complete_json(prompt, Insight, tag=NODE)
    except LLMError as exc:
        logger.warning("结论生成失败：%s", exc)

    if insight is None:
        # 结论生成失败时退回可读的事实陈述，数据与图表照常呈现
        answer = f"查询返回 {len(rows)} 行结果，具体见上方图表与结果表。"
        return {
            "insight": answer,
            "highlights": [],
            "answer": answer,
            "status": "ok",
            "number_checks": _number_checks(answer),
            "messages": [AIMessage(content=answer)],
            "stages": [new_stage(NODE, "分析结果", "结论生成失败，已呈现原始结果", _ms(started))],
        }

    # FR-F-2：修正过的轮次在结论里交代次数与原因。模型已经写了就不再追加，
    # 避免出现两处说法不一致的修正说明。
    conclusion = insight.conclusion.strip()
    fix_sentence = _fix_sentence(state)
    if fix_sentence and "修正" not in conclusion:
        conclusion = f"{conclusion}{fix_sentence}"

    parts = [conclusion]
    if insight.highlights:
        parts.append("\n".join(f"- {h}" for h in insight.highlights))
    answer = "\n\n".join(p for p in parts if p)

    used_caveats = insight.caveats or caveats
    attempts = int(state.get("retry_count") or 0)

    return {
        "insight": conclusion,
        "highlights": insight.highlights,
        "answer": answer,
        # 结构化修正痕迹：与结论文本同源，供前端标注与评测断言
        "fix_note": (
            {"attempts": attempts, "reason": (state.get("fix_reasoning") or "").strip()}
            if attempts > 0
            else None
        ),
        "metric_used": list(state.get("metric_used") or []),
        "status": "ok",
        "number_checks": _number_checks(answer),
        "messages": [AIMessage(content=answer)],
        "stages": [
            new_stage(
                NODE,
                "分析结果",
                f"已生成 {len(rows)} 行数据的解读"
                + (f"，口径说明 {len(used_caveats)} 条" if used_caveats else ""),
                _ms(started),
            )
        ],
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
