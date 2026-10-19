"""validate 节点：安全校验 + 口径校验。

两道关卡依次执行：

1. 安全——守卫的九条规则集中在 db/guard.py，拦表名字段名编造、多条语句、
   缺 LIMIT 等；
2. 口径——命中指标的聚合表达式是否成立，逻辑在 semantic/expression_check.py，
   拦「问毛利只算成交额」这类能执行、有数据、不报错的答非所问。

两处逻辑都不在本节点内实现，本节点只做两件事：把结果翻译成状态更新、在校验失败
时把报错记进修正历史。这样拆是为了让两者都能脱离编排层单独单测。

第三道是槽位覆盖核对：拿 route 抽出的槽位去比 SQL 的 AST，看问到的维度、过滤、
时间、排序、条数有没有落进语句（agent/slot_coverage.py）。它**不阻断执行**——
实测里「被过滤的字段又被要求分组」「按输出别名分组」两类情形会让正确的语句也
报出缺失，据此拦执行等于把正确结果改成放弃。它的作用是让「SQL 合法但答的不是
问的那件事」从看不见变成看得见：结果里带上、审计里留痕、评测里可断言。

第四道是表单条件强断言（agent/slot_coverage.check_form_conditions），只对
表单路径启用：条件是用户在界面上逐条选定的，漏写就是答非所问，因此它阻断并
回灌 fix；自由提问的槽位核对维持只记录，且表单路径下让位给强断言——两套判定
并存会互相矛盾（口语字面值「App」对不上库内标准值 app）。

重试计数由 fix 节点递增：retry_count 的语义是「已经修正了几次」，计在产生修正的
那一侧才不会出现「配置写 3、提示语写 4」这类差一错误。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from app.agent.slot_coverage import (
    check_form_conditions,
    check_slot_coverage,
    dimension_refs,
)
from app.agent.state import AgentState, new_stage
from app.audit.events import (
    EVENT_FORM_BLOCK,
    EVENT_GUARD_PASS,
    EVENT_GUARD_REWRITE,
    EVENT_METRIC_BLOCK,
    OUTCOME_DENIED,
    OUTCOME_OK,
    AuditEvent,
)
from app.audit.recorder import AuditRecorder, get_recorder
from app.db.guard import SqlGuard
from app.semantic.expression_check import check_expression_signatures
from app.semantic.loader import Catalog

logger = logging.getLogger(__name__)

NODE = "validate"


def validate_node(
    state: AgentState,
    *,
    guard: SqlGuard,
    catalog: Catalog | None = None,
    audit: AuditRecorder | None = None,
    # 时间词锚点，取自 QueryRepository.data_as_of（库内数据截止日）。
    # 槽位核对拿它换算「上个月」这类说法，与 retrieve 节点同一来源。
    today: dt.date | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    sql = state.get("sql") or ""
    recorder = audit or get_recorder()

    if not sql.strip():
        return _fail(state, "没有可校验的语句", started, "生成结果为空")

    result = guard.check(sql)

    if not result.ok:
        # 审计在返回之前写：这些语句永远不会被执行，除了这里没有第二个
        # 采集点，漏掉就再也查不到「系统曾经试图执行什么」。
        text = result.error_text()
        _record(
            recorder,
            state,
            AuditEvent.guard_block(
                sql_text=sql,
                message=text,
                source_tables=sorted(result.tables),
                duration_ms=_ms(started),
                detail={"errors": list(result.errors), "attempt": int(state.get("retry_count") or 0)},
            ),
        )
        logger.info("守卫拦截：%s", text)
        return _fail(state, text, started, "安全校验未通过")

    # 安全过关之后再看口径对不对。两步分开是刻意的：一条语句完全可以既安全
    # 又答非所问——问毛利只算成交额，AST 白名单一行都拦不住，因为它检查的是
    # 表名字段名是否登记过，不是聚合含义是否成立。
    #
    # 签名由 retrieve 从命中指标里摘出放进 retrieved，本节点只做比对。
    # 不符时返回的说明会进 fix_history，fix 据此改写；改写后仍回到本节点复查。
    signatures = (state.get("retrieved") or {}).get("metric_signatures") or []
    problems = check_expression_signatures(result.sql, signatures)
    if problems:
        error = "；".join(problems)
        _record(
            recorder,
            state,
            AuditEvent(
                event_type=EVENT_METRIC_BLOCK,
                outcome=OUTCOME_DENIED,
                sql_text=result.sql,
                message=error,
                source_tables=sorted(result.tables),
                duration_ms=_ms(started),
                detail={"problems": list(problems)},
            ),
        )
        logger.info("口径校验未通过：%s", error)
        return _fail(state, error, started, "口径校验未通过")

    # 第四道：表单条件强断言。只对表单路径启用——条件是用户在界面上确定的，
    # 漏写就是答非所问，必须阻断；自由提问的槽位核对维持只记录，因为它在
    # 「按输出别名分组」「过滤字段又被要求分组」两类形态下会误伤正确语句。
    form_conditions = list(state.get("form_conditions") or [])
    if state.get("query_source") == "form" and form_conditions:
        form_gaps = check_form_conditions(
            result.sql,
            form_conditions,
            catalog=catalog,
            grouped=dimension_refs(
                (state.get("form_query") or {}).get("dimensions") or [], catalog
            ),
        )
        if form_gaps:
            error = "表单条件未全部落到语句：" + "；".join(str(g) for g in form_gaps)
            _record(
                recorder,
                state,
                AuditEvent(
                    event_type=EVENT_FORM_BLOCK,
                    outcome=OUTCOME_DENIED,
                    sql_text=result.sql,
                    message=error,
                    source_tables=sorted(result.tables),
                    duration_ms=_ms(started),
                    detail={
                        "gaps": [g.as_dict() for g in form_gaps],
                        "attempt": int(state.get("retry_count") or 0),
                    },
                ),
            )
            logger.info("表单条件强断言未通过：%s", error)
            # 带上条件清单回灌 fix：修正要重写语句，顺手把漏掉的条件补上，
            # 不额外花一次调用。重试配额用尽后由 give_up 归因到 slot。
            return _fail(
                state,
                error,
                started,
                "表单条件核对未通过",
                extra={"slot_gaps": [g.as_dict() for g in form_gaps]},
            )

    warnings = list(result.warnings)
    detail = f"通过校验，涉及 {len(result.tables)} 张表"
    if result.rewritten:
        # 改写是守卫主动加的包裹，必须让用户看得见，否则「实际执行的 SQL」
        # 与模型输出的 SQL 会对不上
        detail += "，已补充行数上限"

    # 第三道：槽位覆盖。安全与口径都过了才做，语句都解析不了时它给不出有用的结论。
    gaps = check_slot_coverage(
        result.sql, state.get("slots") or {}, catalog=catalog, today=today
    )
    if state.get("query_source") == "form":
        # 表单路径的过滤条件交给下面的强断言，槽位核对让位：它按口语字面值
        # 比对（槽位记「App」，库内标准值是 app），照旧比对只会产出一条假的
        # 缺失，与强断言的判定互相矛盾。
        gaps = [g for g in gaps if g.kind != "filter"]
    gap_rows = [g.as_dict() for g in gaps]
    if gaps:
        detail += f"，有 {len(gaps)} 处槽位未落到语句"
        logger.info("槽位覆盖核对：%s", "；".join(str(g) for g in gaps))

    # 改写与直接通过分成两个事件类型：改写量是要盯的运维信号（说明模型
    # 没写 LIMIT），合进 pass 之后只能翻 detail 才能看出来。
    _record(
        recorder,
        state,
        AuditEvent(
            event_type=EVENT_GUARD_REWRITE if result.rewritten else EVENT_GUARD_PASS,
            outcome=OUTCOME_OK,
            sql_text=result.sql,
            message=detail,
            source_tables=sorted(result.tables),
            duration_ms=_ms(started),
            detail={
                "warnings": warnings,
                "original_sql": result.original_sql,
                "slot_gaps": gap_rows,
            },
        ),
    )

    return {
        "sql": result.sql,
        "original_sql": result.original_sql,
        "sql_error": None,
        "guard_warnings": warnings,
        # 槽位缺口随结果返回：前端据此提示「这一轮可能没覆盖你问到的某处」，
        # fix 节点据此在修正时一并补上
        "slot_gaps": gap_rows,
        "stages": [new_stage(NODE, "安全校验", detail, _ms(started))],
    }


def _record(recorder: AuditRecorder, state: AgentState, event: AuditEvent) -> None:
    """补上会话上下文后落审计。

    节点签名里没有 session_id 与 question——它们来自状态，而不是依赖注入。
    审计缺了会话归属就只能回答「执行了什么」，回答不了「谁问的」，所以在这里
    统一补齐，调用点不必各自记得传。
    """
    if event.session_id is None:
        event.session_id = state.get("session_id")
    if event.question is None:
        event.question = state.get("original_question") or state.get("question")
    recorder.record(event)


def _fail(
    state: AgentState,
    error: str,
    started: float,
    label: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = list(state.get("fix_history") or [])
    history.append({"stage": "validate", "sql": (state.get("sql") or "")[:2000], "error": error})
    return {
        "sql_error": error,
        "fix_history": history,
        "stages": [new_stage(NODE, "安全校验", f"{label}：{error[:80]}", _ms(started))],
        **(extra or {}),
    }


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
