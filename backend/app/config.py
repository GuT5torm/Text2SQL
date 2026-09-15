"""应用配置。全部配置项从环境变量或 backend/.env 读取，代码中不出现任何密钥字面量。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # —— 应用 ——
    app_name: str = "Text2SQL 电商数据分析 Agent"
    debug: bool = False
    log_level: str = "INFO"
    # 逗号分隔。本地开发时前端跑在 5173，由 vite 代理转发，实际很少需要放开 CORS
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # —— LLM 网关 ——
    # 服务商与模型名以各家最新文档为准，此处给出的是阿里云百炼的 OpenAI 兼容入口。
    # 换服务商只需改这三项，不需要动代码。
    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"
    # 主模型连续失败时降级使用；留空表示不启用降级
    llm_fallback_model: str = ""
    llm_timeout: float = 30.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2048
    # 无密钥环境（离线演示、单元测试）下用桩实现替代真实调用，
    # 便于在拿到密钥之前先把整条链路跑通。生产使用必须置为 false。
    llm_mock: bool = False

    # —— 存储 ——
    business_db_path: Path = BACKEND_DIR / "data" / "ecommerce.db"
    app_db_path: Path = BACKEND_DIR / "data" / "app.db"

    # —— SQL 安全沙箱 ——
    sql_timeout_seconds: float = 10.0
    sql_max_rows: int = 1000
    # 校验失败与执行失败共享的重试配额，超过即返回可读的失败说明
    sql_max_retry: int = 3
    # 单次查询允许扫描的最大行数，超过由 SQLite 的 progress handler 中断
    sql_scan_limit: int = 20_000_000

    # —— 会话 ——
    # 历史会话回灌给模型的最近轮数
    history_window: int = 6
    checkpointer: str = "memory"  # memory | sqlite

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
