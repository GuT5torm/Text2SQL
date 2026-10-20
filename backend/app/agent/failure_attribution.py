"""失败归因：把一轮失败归到四类之一。

四类对应四种修法，混在一起就判断不出下一轮该补什么：守卫拦下要补语义层登记，
口径不符要补指标定义，条件没落进语句要补提示词或强断言，剩下的才轮到换模型。

这里是运行时与评测的同一来源：接口结果里的 `failure_attribution` 与 eval 报表的
归因列由同一段逻辑产出。判断原先只写在 `eval/run_eval.py` 里，界面上拿不到，于是
出现「报表说这一轮是口径问题、界面只给一句通用重试建议」——同一轮失败在两处说法
不一致，而用户只看得到后者。

判定按证据强弱排序，不按发生顺序：slot 最硬（缺口类型写在结果里），guard 有 R
编号，signature 只有文案关键字可依，三者都不是才落 generate。generate 实际是
「没有更具体的证据」，把它当兜底而不是当结论。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

GUARD = "guard"
SIGNATURE = "signature"
SLOT = "slot"
GENERATE = "generate"

ATTRIBUTIONS: tuple[str, ...] = (GUARD, SIGNATURE, SLOT, GENERATE)

# 界面上给人看的标签与修法建议。标签说「哪里出的」，建议说「该改什么」——
# 只给标签等于让用户自己去猜修法。
LABELS: dict[str, str] = {
    GUARD: "守卫拦下",
    SIGNATURE: "口径签名不符",
    SLOT: "条件没落进语句",
    GENERATE: "生成侧未收敛",
}

SUGGESTIONS: dict[str, str] = {
    GUARD: "补语义层登记表或字段",
    SIGNATURE: "补 metrics.yaml 的指标定义",
    SLOT: "补提示词条件清单或强断言",
    GENERATE: "换模型或调生成参数",
}

# 表单条件强断言写进结果的缺口类型，比 error 文案更可靠
FORM_GAP_KIND = "form_condition"
# validate 节点强断言失败时的开头，语句里出现它就不必再看别的证据
FORM_MARK = "表单条件未全部落到语句"
SIGNATURE_MARKS = ("口径", "签名")
GUARD_MARKS = ("守卫",)
_RULE_PATTERN = re.compile(r"R\d")

__all__ = [
    "ATTRIBUTIONS",
    "GENERATE",
    "GUARD",
    "LABELS",
    "SIGNATURE",
    "SLOT",
    "SUGGESTIONS",
    "attribute_failure",
    "describe_attribution",
]


def attribute_failure(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """把一轮结果归到四类之一。

    入参既可以是图的终态（state），也可以是 `summarize_result` 之后的结果结构：
    两者都带 status / slot_gaps / sql_error 三项，其余字段一概不看。
    非失败轮返回 None——成功轮带一个空归因只会让前端多一个判断分支。
    """
    if not payload:
        return None
    if str(payload.get("status") or "") != "error":
        return None

    error = str(payload.get("sql_error") or payload.get("error") or "")
    kinds = _gap_kinds(payload.get("slot_gaps"))

    if FORM_GAP_KIND in kinds or FORM_MARK in error:
        return _build(
            SLOT,
            evidence="表单条件未全部落到语句" if FORM_GAP_KIND in kinds else error,
        )
    if _RULE_PATTERN.search(error) or any(m in error for m in GUARD_MARKS):
        return _build(GUARD, evidence=error)
    if any(m in error for m in SIGNATURE_MARKS):
        return _build(SIGNATURE, evidence=error)
    return _build(GENERATE, evidence=error or "无更具体的失败证据")


def describe_attribution(kind: str) -> dict[str, str]:
    """把归因编码翻成标签与修法建议。未知编码落 generate，不抛异常。"""
    safe = kind if kind in LABELS else GENERATE
    return {"kind": safe, "label": LABELS[safe], "suggestion": SUGGESTIONS[safe]}


def _build(kind: str, *, evidence: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": LABELS[kind],
        "suggestion": SUGGESTIONS[kind],
        "evidence": evidence,
    }


def _gap_kinds(gaps: Any) -> set[str]:
    """取缺口类型集合。

    结果里的 slot_gaps 是 dict（validate 走 as_dict），图终态里也可能是对象；
    两种形态都要能吃下，否则归因会静默退到 generate。
    """
    kinds: set[str] = set()
    for gap in gaps or []:
        if isinstance(gap, Mapping):
            value = gap.get("kind")
        else:
            value = getattr(gap, "kind", None)
        if value is not None:
            kinds.add(str(value))
    return kinds
