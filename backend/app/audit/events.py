"""审计事件的数据结构与类型常量。

本模块刻意不 import 项目内任何模块（标准库除外）。审计事件被存储层、编排层、
接入层三方引用，它一旦反过来依赖其中某一层就会形成 import 环；把依赖方向
定成「众人都依赖 events，events 谁也不依赖」是最省事的解法。

事件类型按「判定发生在哪一步」划分，不按结果好坏划分：守卫拦截与口径拦截
都在 validate 节点发生，但前者是安全、后者是语义，分成两个类型之后，排查
时能直接按类型筛出「哪几条是因为口径不对被拦的」。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

# —— 事件类型 ——
# 守卫三态分开而不是合成一条带 flag 的记录：拦截量与改写量是两类运维信号，
# 合并后要靠 detail 才能区分，SQL 里没法直接 group by。
EVENT_GUARD_BLOCK = "sql_guard_block"  # 守卫拒绝，语句不会被执行
EVENT_GUARD_REWRITE = "sql_guard_rewrite"  # 守卫通过但改写了语句（R6 补 LIMIT）
EVENT_GUARD_PASS = "sql_guard_pass"  # 通过且未改写
EVENT_METRIC_BLOCK = "sql_metric_block"  # 安全过关但口径签名不符
EVENT_FORM_BLOCK = "sql_form_condition_block"  # 表单条件未全部落到语句
EVENT_SQL_EXECUTE = "sql_execute"  # 实际执行成功
EVENT_SQL_ERROR = "sql_execute_error"  # 执行失败（含超时）

EVENT_TYPES: tuple[str, ...] = (
    EVENT_GUARD_BLOCK,
    EVENT_GUARD_REWRITE,
    EVENT_GUARD_PASS,
    EVENT_METRIC_BLOCK,
    EVENT_FORM_BLOCK,
    EVENT_SQL_EXECUTE,
    EVENT_SQL_ERROR,
)

# —— 结果 ——
OUTCOME_OK = "ok"
OUTCOME_DENIED = "denied"
OUTCOME_ERROR = "error"

OUTCOMES: tuple[str, ...] = (OUTCOME_OK, OUTCOME_DENIED, OUTCOME_ERROR)

# 单条 SQL 的留存上限。模型生成的语句通常在几百字符内，上限只为挡住极端
# 情况把审计表撑爆；截断时会在 detail 里留标记，不会静默丢失信息。
MAX_SQL_TEXT = 8000

_RULE_RE = re.compile(r"\bR(\d+)\b")


def extract_rule_id(text: str | None) -> str | None:
    """从守卫文案里取规则编号（R1–R9）。

    守卫把多条错误用「；」拼成一句，取第一个出现的编号作为主因。匹配不到时
    返回 None 而不是硬给一个值——口径拦截这类没有规则编号的事件应当如实
    留空，靠 event_type 区分。
    """
    if not text:
        return None
    match = _RULE_RE.search(text)
    return f"R{match.group(1)}" if match else None


@dataclass
class AuditEvent:
    """一条审计记录。

    event_type 与 outcome 必填且不设默认值：这两个字段决定这条记录能不能被
    检索到，缺失的审计等于没审计。其余字段允许为空，因为不是每次判定都能
    拿到——守卫拦截时就没有行数与耗时。

    ts 为 None 时由存储层填入落库时刻，调用方不必重复计算时间。
    """

    event_type: str
    outcome: str
    session_id: str | None = None
    question: str | None = None
    sql_text: str | None = None
    # 守卫规则编号（R1–R9）。口径拦截与执行事件为 None
    rule_id: str | None = None
    # 人可读的原因：守卫原文、口径不符说明、数据库原始报错
    message: str = ""
    source_tables: list[str] = field(default_factory=list)
    row_count: int | None = None
    duration_ms: float | None = None
    # 结构化补充：守卫错误列表、结果列名、是否截断等，随事件类型而变
    detail: dict[str, Any] = field(default_factory=dict)
    ts: dt.datetime | None = None
    id: int = 0  # 0 表示尚未落库，由存储层回填

    def __post_init__(self) -> None:
        if self.sql_text and len(self.sql_text) > MAX_SQL_TEXT:
            self.sql_text = self.sql_text[:MAX_SQL_TEXT]
            self.detail["sql_truncated"] = True

    @classmethod
    def guard_block(cls, **kwargs: Any) -> "AuditEvent":
        """构造一条守卫拦截事件，规则编号由守卫文案解析。"""
        message = kwargs.get("message") or ""
        kwargs.setdefault("rule_id", extract_rule_id(message))
        return cls(event_type=EVENT_GUARD_BLOCK, outcome=OUTCOME_DENIED, **kwargs)
