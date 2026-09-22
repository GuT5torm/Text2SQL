"""数据访问层抽象接口。

上层（Agent 节点、接入层）只依赖这里的接口，不关心底层是 SQLite 还是
PostgreSQL。换库时新增一个实现类即可，这正是 ADR-002 留出的切换口子。
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    """一次只读查询的结果。rows 用字典列表，便于直接序列化给前端。"""

    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    # 结果集体积估算值（字节）。行数上限管不住「一行一个超大值」，
    # 记录实际体积便于在日志与用例里核对兜底是否触发
    bytes_estimate: int = 0

    def preview(self, n: int = 20) -> list[dict[str, Any]]:
        return self.rows[:n]


class QueryError(RuntimeError):
    """执行失败。message 会原样交给 fix 节点，因此要保持数据库的原始措辞。"""


class QueryTimeout(QueryError):
    """执行超时。与语法错误区分开，重试策略不同。"""


class QueryRepository(ABC):
    """业务库查询接口。只读，不提供任何写方法。"""

    @abstractmethod
    def execute_readonly(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """执行一条只读查询。sql 应当已通过 SqlGuard 校验。"""

    @abstractmethod
    def list_tables(self) -> list[str]:
        """列出业务库中的全部表名，用于与语义层做一致性核对。"""

    @abstractmethod
    def count_rows(self, table: str) -> int:
        """统计表行数，供演示前的数据量确认。"""


# --------------------------------------------------------------------------
# 应用库（会话元数据）
# --------------------------------------------------------------------------

@dataclass
class SessionRecord:
    id: str
    title: str
    created_at: dt.datetime
    updated_at: dt.datetime
    archived: int = 0
    turn_count: int = 0


@dataclass
class TurnRecord:
    """一轮问答记录。

    id 排在最后且带默认值，是因为它有两个方向：写入时还没有主键，由数据库自增
    生成后由 add_turn 回填；读取时由 list_turns 填好。放在前面会让所有字段
    被迫给出默认值，反而掩盖了 session_id 与 question 是必填这一事实。
    """

    session_id: str
    question: str
    answer: str = ""
    sql: str | None = None
    metrics_json: str | None = None
    chart_json: str | None = None
    rows_json: str | None = None
    row_count: int | None = None
    elapsed_ms: float | None = None
    status: str = "ok"
    error: str | None = None
    created_at: dt.datetime | None = None
    id: int = 0  # 0 表示尚未落库，add_turn 提交后回填自增主键

    # —— FR-H-2 历史回看字段 ——
    # 阶段时间线 JSON，stages 列表原样落库
    stages_json: str | None = None
    # 守卫改写前的原始 SQL
    original_sql: str | None = None
    # 本轮守卫告警 JSON 字符串
    guard_warnings_json: str | None = None
    # fix 节点递增次数；0 表示未修正
    retry_count: int = 0
    # 0/1：结果是否被行数上限截断
    truncated: int = 0
    # 实际落库的行数
    persist_row_count: int = 0
    # 实际执行 SQL 中出现的表名（去重后），JSON 字符串数组
    source_tables_json: str | None = None
    # 异常标记列表 JSON（FR-L-1，第 6 批事，本批允许 None）
    findings_json: str | None = None
    # 对比基准值 JSON（FR-M-1，第 4 批事，本批允许 None）
    baselines_json: str | None = None
    # 本轮走过的节点序列去重（FR-I-4 后续会用到）
    node_path: str | None = None
    # FR-A-5：结构化槽位 JSON（metrics/dimensions/filters/time_range/order_by/top_n）
    slots_json: str | None = None
    # FR-H-3：用户原话与改写后问句。二者不同时说明这一轮发生了指代消解
    original_question: str | None = None
    rewritten_question: str | None = None


@dataclass
class FeedbackRecord:
    """一条点赞点踩（FR-G-1）。

    rating 取 up/down；down 时 reason_code 与 note 才有意义。
    turn_id 关联 turns 表，反馈只能针对已落库的轮次。
    """

    turn_id: int
    session_id: str
    rating: str  # up / down
    reason_code: str = ""
    note: str = ""
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    id: int = 0


@dataclass
class ShareRecord:
    """一条只读分享链接（FR-N-3）。

    token 是访问凭据；snapshot_json 存完整结果快照，访问时直接读出。
    expires_at 为 None 表示永久有效；revoked=1 后前端不再可读。
    """

    token: str
    snapshot_json: str = "{}"
    turn_id: int | None = None
    session_id: str | None = None
    expires_at: dt.datetime | None = None
    revoked: int = 0
    created_at: dt.datetime | None = None


@dataclass
class AlertRuleRecord:
    """一条异常订阅规则（FR-O-1）。

    dimensions 为维度名列表，空列表表示整体口径；comparison 取
    gt / lt / deviation；severity 取 notice / attention。
    id=0 表示尚未落库。
    """

    metric: str
    comparison: str
    threshold: float
    dimensions: list[str] = field(default_factory=list)
    severity: str = "notice"
    enabled: bool = True
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    id: int = 0


@dataclass
class AlertRecord:
    """一次规则求值事件（FR-O-2 / FR-O-5）。

    一个 (rule_id, evaluated_at) 唯一对应一行：命中多个切片时，hits 里
    每个元素是 {"slice": {...}|None, "value": float, "baseline": {...}|None,
    "reason": str}；未命中 hits 为空；失败时 status=error 且 reason 必填。
    """

    rule_id: int
    evaluated_at: dt.datetime
    status: str = "ok"  # ok / error
    hit: bool = False
    metric: str = ""
    hits: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    seen: bool = False
    created_at: dt.datetime | None = None
    id: int = 0

    @property
    def value(self) -> float | None:
        """首个命中值，供列表排序与角标展示。"""
        return self.hits[0].get("value") if self.hits else None


class SessionStore(ABC):
    """会话元数据接口。历史会话的增删查都在这里。"""

    @abstractmethod
    def create_session(self, title: str = "新会话") -> SessionRecord: ...

    @abstractmethod
    def get_session(self, session_id: str) -> SessionRecord | None: ...

    @abstractmethod
    def list_sessions(self, include_archived: bool = False, keyword: str | None = None) -> list[SessionRecord]: ...

    @abstractmethod
    def rename_session(self, session_id: str, title: str) -> bool: ...

    @abstractmethod
    def archive_session(self, session_id: str, archived: bool = True) -> bool: ...

    @abstractmethod
    def delete_session(self, session_id: str) -> bool: ...

    @abstractmethod
    def add_turn(self, turn: TurnRecord) -> int: ...

    @abstractmethod
    def list_turns(self, session_id: str) -> list[TurnRecord]: ...

    # —— FR-G 反馈闭环（第 6 批） ——
    @abstractmethod
    def upsert_feedback(self, feedback: FeedbackRecord) -> int: ...
    """提交点赞点踩。同一 turn_id 重复提交按更新处理（UNIQUE 约束保证）。"""

    @abstractmethod
    def get_feedback_by_turn(self, turn_id: int) -> FeedbackRecord | None: ...

    @abstractmethod
    def list_feedback(
        self,
        session_id: str | None = None,
        rating: str | None = None,
    ) -> list[FeedbackRecord]: ...
    """按会话或评分筛选反馈列表，用于聚合统计与导出。"""

    # —— FR-N-3 只读分享（第 6 批） ——
    @abstractmethod
    def create_share(self, share: ShareRecord) -> str: ...
    """插入一条分享链接记录，返回 token（既是 PK 也是访问凭据）。"""

    @abstractmethod
    def get_share(self, token: str) -> ShareRecord | None: ...

    # —— FR-O 异常订阅（第 7 批） ——
    @abstractmethod
    def list_alert_rules(self, enabled_only: bool = False) -> list[AlertRuleRecord]: ...

    @abstractmethod
    def get_alert_rule(self, rule_id: int) -> AlertRuleRecord | None: ...

    @abstractmethod
    def upsert_alert_rule(self, rule: AlertRuleRecord) -> AlertRuleRecord:
        """新建（id=0）或按 id 更新规则。"""

    @abstractmethod
    def delete_alert_rule(self, rule_id: int) -> bool: ...

    @abstractmethod
    def insert_alert_evaluation(self, alert: AlertRecord) -> AlertRecord:
        """幂等写入一次求值：(rule_id, evaluated_at) 已存在则原样返回旧记录。"""

    @abstractmethod
    def list_alerts(
        self,
        seen: bool | None = None,
        rule_id: int | None = None,
        limit: int = 100,
    ) -> list[AlertRecord]: ...

    @abstractmethod
    def count_unseen_alerts(self) -> int: ...

    @abstractmethod
    def mark_alert_seen(self, alert_id: int) -> bool: ...

    @abstractmethod
    def latest_alert_eval(self) -> AlertRecord | None:
        """最近一次求值（任意规则任意状态），从未求值返回 None。"""

    @abstractmethod
    def consecutive_alert_failures(self) -> int:
        """从最近一次求值向前数连续 error 的次数（按求值时刻倒序）。"""
