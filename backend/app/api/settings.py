"""运行时设置接口：让普通用户通过界面配置 LLM 密钥等，无需手改 .env 或重启。

- GET  /api/settings  返回安全子集（密钥绝不回传明文，只给 has_api_key 标记）
- POST /api/settings  接收白名单字段 → 写盘 → reset_container() 热重载 → 返回安全子集
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import reset_container
from app.config import apply_runtime_settings, get_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPatch(BaseModel):
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_mock: Optional[bool] = None
    llm_fallback_model: Optional[str] = None


def _safe_view() -> dict[str, Any]:
    """只回传非敏感字段，密钥以布尔占位。"""
    s = get_settings()
    return {
        "has_api_key": bool(s.llm_api_key),
        "llm_base_url": s.llm_base_url,
        "llm_model": s.llm_model,
        "llm_mock": s.llm_mock,
        "llm_fallback_model": s.llm_fallback_model,
    }


@router.get("")
def get_settings_view() -> dict[str, Any]:
    return _safe_view()


@router.post("")
def update_settings_view(patch: SettingsPatch) -> dict[str, Any]:
    data = patch.model_dump(exclude_none=True)
    apply_runtime_settings(data)
    # 清容器缓存并触发下一次请求重建：新网关会用新密钥，保存即生效
    reset_container()
    return _safe_view()
