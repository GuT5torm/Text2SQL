"""应用配置。全部配置项从环境变量或 backend/.env 读取，代码中不出现任何密钥字面量。"""

from __future__ import annotations

import json
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
    # FR-F-1：空结果回灌的独立配额。与 sql_max_retry 分开计数——空结果与报错是
    # 两类失败，共享配额时一次空结果回灌会吃掉一次本该给报错修正的机会。
    # 默认 1：只允许放宽一次，再宽下去得到的就不是用户问的那件事了。
    sql_max_empty_retry: int = 1
    # 单次查询允许扫描的最大行数，超过由 SQLite 的 progress handler 中断
    sql_scan_limit: int = 20_000_000
    # 单个结果集的体积上限（字节）。行数上限挡不住「一行一个超大值」的写法，
    # 这一条是第二道兜底，超过即拒绝返回，避免序列化时把进程内存打满
    sql_max_result_bytes: int = 32 * 1024 * 1024
    # FR-J-2：SQL 方言可配置。守卫与 prompt 都从这里取方言，不再四处硬编码。
    # 默认 sqlite；切换到 PostgreSQL / MySQL 时改这一项即可。
    sql_dialect: str = "sqlite"

    # —— 会话 ——
    # 历史会话回灌给模型的最近轮数
    history_window: int = 6
    checkpointer: str = "memory"  # memory | sqlite

    # —— 数据时效（FR-D-2）——
    # 超过该天数未更新视为异常。订单库每天都该有变化，按 7 天取保守值；
    # 嫌声大可以调小到 3，无须后台改动即可生效。退款表允许比订单表更旧。
    data_staleness_days: int = 7
    # 可选配置项覆盖「库内 MAX 取法」。运维在数据快照日期与库内实际取法不同
    # 时（如迁移、压测）置该项强制覆盖；留空则按库内 MAX 计算。
    data_cutoff_override: str = ""

    # —— FR-J-3 可观测 ——
    # 模型价格表，JSON 字符串：模型名 → {"input": 每千 token 价格,
    # "output": 每千 token 价格}。价格随厂商调整，不写进代码，统一走配置；
    # 未配置的模型成本按 0 计，token 数照常统计。
    llm_price_json: str = ""
    # 异常订阅连续失败达到此次数时在 /api/health 暴露（FR-O-5）
    alert_consecutive_failures_threshold: int = 3
    # 求值时刻量化周期（分钟）。外部计划任务在同一周期内重试不会产生重复
    # 求值行：默认 60，即每个规则每小时最多一条记录（FR-O-3）。
    alert_eval_period_minutes: int = 60

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_prices(self) -> dict[str, dict[str, float]]:
        """解析 LLM_PRICE_JSON；配置缺失或非法时返回空字典（成本按 0 计）。"""
        if not self.llm_price_json.strip():
            return {}
        try:
            data = json.loads(self.llm_price_json)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        prices: dict[str, dict[str, float]] = {}
        for model, item in data.items():
            if not isinstance(item, dict):
                continue
            prices[str(model)] = {
                "input": float(item.get("input", 0) or 0),
                "output": float(item.get("output", 0) or 0),
            }
        return prices


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
