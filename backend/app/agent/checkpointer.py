"""会话记忆的 Checkpointer。

起步用 MemorySaver：零依赖、进程内、复杂度最低，符合 P0 演示阶段。
需要会话在重启后仍可恢复时，把 .env 里的 CHECKPOINTER 改为 sqlite，
即切到 SQLiteSaver，落到 data/app.db，无需改动节点代码。

thread_id 取 session_id，与接入层创建的会话一一对应。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_checkpointer(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    mode = (s.checkpointer or "memory").lower()

    if mode == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError:  # pragma: no cover - 未安装可选依赖时退回内存
            logger.warning(
                "未安装 langgraph-checkpoint-sqlite，会话将不会跨重启保留。"
                "安装命令：pip install langgraph-checkpoint-sqlite"
            )
            return MemorySaver()

        s.app_db_path.parent.mkdir(parents=True, exist_ok=True)
        # SqliteSaver 自带连接管理，from_conn_string 返回上下文管理器，
        # 这里用底层连接手工构造，便于在 FastAPI 生命周期内长期持有
        import sqlite3

        conn = sqlite3.connect(s.app_db_path.as_posix(), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        logger.info("会话记忆使用 SQLiteSaver：%s", s.app_db_path)
        return saver

    logger.info("会话记忆使用 MemorySaver（进程重启后丢失）")
    return MemorySaver()


def thread_config(session_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": session_id}}
