"""审计事件的落点。

sink 只暴露 write 一个方法，是为了让「审计写不进去」这件事有统一的兜底位置：
recorder 包住 write 的异常，调用方一概不需要写 try。审计是旁路，它失败时
正确的处理是记一条 warning 然后继续，而不是把主链路打断。

DatabaseSink 不直接依赖 db 层的具体类，只要求对象有 record 方法。这样
sinks.py 不 import 任何存储模块，依赖方向保持单向。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Protocol

from app.audit.events import AuditEvent

logger = logging.getLogger(__name__)


class RecordingRepository(Protocol):
    """审计仓储需要满足的最小契约。"""

    def record(self, event: AuditEvent) -> int: ...


class AuditSink(ABC):
    @abstractmethod
    def write(self, event: AuditEvent) -> None:
        """写入一条事件。实现必须保证异常由调用方（recorder）兜住。"""


class DatabaseSink(AuditSink):
    """写进应用库的 audit_log 表。生产环境的默认落点。"""

    def __init__(self, repo: RecordingRepository) -> None:
        self.repo = repo

    def write(self, event: AuditEvent) -> None:
        self.repo.record(event)


class LoggingSink(AuditSink):
    """写进标准库日志。

    用在两类场景：应用库不可写时的降级，以及不希望审计数据落盘的环境。
    日志会随进程重启丢失，因此它只作兜底，不作为默认配置。
    """

    def write(self, event: AuditEvent) -> None:
        logger.info(
            "审计 %s %s rule=%s session=%s rows=%s | %s",
            event.event_type,
            event.outcome,
            event.rule_id,
            event.session_id,
            event.row_count,
            event.message,
        )


class NullSink(AuditSink):
    """丢弃。审计关闭时使用，也是未配置时的默认状态。"""

    def write(self, event: AuditEvent) -> None:
        return None


class MemorySink(AuditSink):
    """留在本进程内存里，供单测断言。

    events 只读使用：测试断言落库的事件内容，不模拟回填 id。
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def filter(self, event_type: str | None = None, outcome: str | None = None) -> list[AuditEvent]:
        return [
            e
            for e in self.events
            if (event_type is None or e.event_type == event_type)
            and (outcome is None or e.outcome == outcome)
        ]

    def dump_json(self) -> str:
        return json.dumps([e.__dict__ for e in self.events], ensure_ascii=False, default=str)
