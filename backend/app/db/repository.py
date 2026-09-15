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
    id: int
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
