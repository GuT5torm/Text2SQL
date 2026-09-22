"""FR-D-2 数据时效计算。

按设计说明，日期来源「库内 MAX + 配置项兜底」：默认查 orders 与 refunds 的
MAX(created_at)，运维在压测或迁移场景里可用 DATA_CUTOFF_OVERRIDE 强制覆盖。

超期判定以 settings.data_staleness_days 为阈值（默认 7），任一表 age_days 超过
阈值则 is_stale=True。文档要求「订单表与退款表相差 1 天」的情况如实呈现，
不掩盖。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.config import get_settings
from app.db.repository import QueryError, QueryRepository

logger = logging.getLogger(__name__)


def compute_data_freshness(repo: QueryRepository) -> dict[str, Any]:
    """计算 orders / refunds 两表的截止日期与超期标记。

    返回字段：
      orders_cutoff     订单表 MAX(created_at)，ISO 字符串
      refunds_cutoff    退款表 MAX(created_at)，ISO 字符串
      orders_age_days   与今天相差天数
      refunds_age_days  与今天相差天数
      is_stale          任一表超期（age_days > 阈值）
      threshold_days    配置阈值
      source            "library_max" 或 "config_override"
      note              文案：例如「订单表与退款表相差 1 天」
    """
    settings = get_settings()
    today = dt.date.today()
    threshold = settings.data_staleness_days

    # 配置项兜底：运维在数据快照日期与库内实际取法不同时强制覆盖。
    if settings.data_cutoff_override:
        cutoff = settings.data_cutoff_override.strip()[:10]
        orders_cutoff = cutoff
        refunds_cutoff = cutoff
        source = "config_override"
    else:
        orders_cutoff = _max_date(repo, "orders")
        refunds_cutoff = _max_date(repo, "refunds")
        source = "library_max"

    orders_age = _age_days(orders_cutoff, today)
    refunds_age = _age_days(refunds_cutoff, today)

    is_stale = (
        (orders_age is not None and orders_age > threshold)
        or (refunds_age is not None and refunds_age > threshold)
    )

    note_parts: list[str] = []
    if orders_age is None and refunds_age is None:
        note_parts.append("两表均无数据。")
    if (
        orders_age is not None
        and refunds_age is not None
        and abs(orders_age - refunds_age) >= 1
    ):
        # 文档要求如实呈现，不掩盖。差 1 天是数据更新节奏差异，不算异常。
        # 注意 age_days 是「距今天数」，数值越大代表截止日期越早，比较方向与
        # 日期大小相反——按日期直觉写会得出相反的结论。
        diff = abs(orders_age - refunds_age)
        older, newer = (
            ("订单表", "退款表") if orders_age > refunds_age else ("退款表", "订单表")
        )
        note_parts.append(f"{older} 比 {newer} 截止早 {diff} 天。")

    return {
        "orders_cutoff": orders_cutoff,
        "refunds_cutoff": refunds_cutoff,
        "orders_age_days": orders_age,
        "refunds_age_days": refunds_age,
        "is_stale": is_stale,
        "threshold_days": threshold,
        "source": source,
        "note": " ".join(note_parts) or "数据时效正常。",
    }


def _max_date(repo: QueryRepository, table: str) -> str | None:
    """取某表的 MAX(created_at)，最多 8 字符日期部分。失败时返回 None 而不是抛错。"""
    try:
        result = repo.execute_readonly(f"SELECT MAX(created_at) AS m FROM {table}")
    except QueryError as exc:
        logger.warning("查 %s 最大日期失败：%s", table, exc)
        return None
    if not result.rows:
        return None
    value = result.rows[0].get("m") or result.rows[0].get("MAX(created_at)")
    if not value:
        return None
    # SQLite 返回的可能是 'YYYY-MM-DD HH:MM:SS'；截前 10 位
    return str(value)[:10]


def _age_days(date_str: str | None, today: dt.date) -> int | None:
    if not date_str:
        return None
    try:
        return (today - dt.date.fromisoformat(date_str)).days
    except ValueError:
        return None
