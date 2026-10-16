"""审计记录器：编排节点与接入层唯一的写入入口。

做成进程内单例而不是每次调用时构造，是因为审计的调用点分散在节点内部，
把仓储一路传进去会让节点签名多出一个与业务无关的参数（节点已经带了
guard、repo、gateway）。单例的代价是全局状态，因此装配与复位都通过本模块
的显式函数进行，不在 import 时产生任何副作用。

默认落点是 NullSink：未装配容器时（单元测试、脚本调用）审计保持静默，
不会因为模块被 import 就去连数据库。
"""

from __future__ import annotations

import logging

from app.audit.events import AuditEvent
from app.audit.sinks import AuditSink, NullSink

logger = logging.getLogger(__name__)


class AuditRecorder:
    """把事件交给 sink，并兜住 sink 的失败。"""

    def __init__(self, sink: AuditSink | None = None) -> None:
        self.sink: AuditSink = sink or NullSink()

    @property
    def enabled(self) -> bool:
        """是否真的在记。关闭时调用方可以跳过构造事件的开销。"""
        return not isinstance(self.sink, NullSink)

    def record(self, event: AuditEvent) -> AuditEvent:
        """记录一条事件，返回原对象便于调用方继续用（例如取回填的 id）。

        这里刻意不抛出：审计写不进去不该让正在执行的查询失败。异常只落到
        日志，且带 exc_info，排查时看得到根因。
        """
        try:
            self.sink.write(event)
        except Exception:  # noqa: BLE001 - 审计是旁路，失败不能影响主链路
            logger.warning(
                "审计事件写入失败（不影响主链路）：%s/%s",
                event.event_type,
                event.outcome,
                exc_info=True,
            )
        return event


_recorder = AuditRecorder(NullSink())


def get_recorder() -> AuditRecorder:
    return _recorder


def configure_recorder(sink: AuditSink | None) -> None:
    """装配落点。传 None 等于关闭审计。"""
    global _recorder
    _recorder = AuditRecorder(sink)


def reset_recorder() -> None:
    """复位为静默状态。测试与容器重建时调用，避免用例之间互相串数据。"""
    configure_recorder(None)
