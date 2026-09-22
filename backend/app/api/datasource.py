"""数据源连接接口：让普通用户通过界面连接一个新数据库，无需手改 .env 或 YAML。

- POST /api/datasource/test    只读内省目标库，回传表清单与字段数（不改动任何配置）
- POST /api/datasource/connect 生成语义层草稿 → 备份现有 YAML → 写盘 → 热重载

安全：
- 仅本机可达（与聊天同权，架构既定无鉴权）；
- 写盘前把现有 4 份语义层 YAML 备份为 .bak-<时间戳>，可随时回退；
- 连接串（含密码）只写入 .env（已 gitignore），GET /api/health 等接口不回传明文。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import reset_container
from app.config import BACKEND_DIR, apply_runtime_settings, get_settings
from app.db.introspect import (
    DatasourceSpec,
    build_semantic_draft,
    introspect,
    normalize_dialect,
)

router = APIRouter(prefix="/api/datasource", tags=["datasource"])

SEMANTIC_DIR = BACKEND_DIR / "app" / "semantic"
# 这 5 份是 load_catalog 涉及的，连接新库时整体替换并各留一份备份
_SEMANTIC_FILES = (
    "tables.yaml",
    "metrics.yaml",
    "synonyms.yaml",
    "baseline_rules.yaml",
    "finding_rules.yaml",
)


class DatasourceRequest(BaseModel):
    dialect: str = "sqlite"
    path: Optional[str] = None  # sqlite 库文件绝对路径
    dsn: Optional[str] = None  # postgres / mysql 连接串


def _to_spec(req: DatasourceRequest) -> DatasourceSpec:
    dialect = normalize_dialect(req.dialect)
    if dialect == "sqlite":
        if not req.path or not req.path.strip():
            raise HTTPException(status_code=400, detail="请填写 SQLite 库文件绝对路径")
        return DatasourceSpec(dialect="sqlite", path=req.path.strip())
    if not req.dsn or not req.dsn.strip():
        raise HTTPException(status_code=400, detail="该方言需要填写连接串（DSN）")
    return DatasourceSpec(dialect=dialect, dsn=req.dsn.strip())


@router.post("/test")
def test_datasource(req: DatasourceRequest) -> dict[str, Any]:
    """只读内省目标库，验证可达并回传结构概览。不改动任何配置。"""
    spec = _to_spec(req)
    try:
        intro = introspect(spec)
    except Exception as exc:  # 文件缺失 / 驱动缺失 / 鉴权失败等，原样告知用户
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "dialect": spec.dialect,
        "table_count": len(intro.tables),
        "tables": [
            {"name": t["name"], "fields": len(t["fields"]), "pk": t["pk"]}
            for t in intro.tables
        ],
    }


@router.post("/connect")
def connect_datasource(req: DatasourceRequest) -> dict[str, Any]:
    """连接新库：内省 → 生成语义层草稿 → 备份并覆盖现有 YAML → 写 .env → 热重载。"""
    spec = _to_spec(req)
    try:
        intro = introspect(spec)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not intro.tables:
        raise HTTPException(status_code=400, detail="该库没有任何表，无法生成语义层")

    draft = build_semantic_draft(spec, intro)
    _backup_semantic()
    _write_semantic(draft)

    # 写 .env：方言必写；sqlite 写路径，其余写连接串
    patch: dict[str, Any] = {"sql_dialect": spec.dialect}
    if spec.dialect == "sqlite":
        patch["business_db_path"] = str(Path(spec.path).as_posix())
    else:
        patch["business_db_dsn"] = spec.dsn
    apply_runtime_settings(patch)

    # 清容器缓存并触发下一次请求重建：新引擎 + 新语义层立即生效，无需重启
    reset_container()

    return {
        "ok": True,
        "dialect": spec.dialect,
        "table_count": len(intro.tables),
        "metric_count": len(draft["metrics.yaml"]["metrics"]),
        "dimension_count": len(draft["tables.yaml"]["dimensions"]),
    }


def _backup_semantic() -> None:
    """连接新库前备份现有 4 份语义层 YAML，便于回退。已存在的同名备份会被覆盖。"""
    stamp = dt_stamp()
    for name in _SEMANTIC_FILES:
        src = SEMANTIC_DIR / name
        if src.exists():
            shutil.copyfile(src, SEMANTIC_DIR / f"{name}.bak-{stamp}")


def _write_semantic(draft: dict[str, dict[str, Any]]) -> None:
    SEMANTIC_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        "# 自动生成的语义层草稿（基于库内省）。表结构为事实；指标与维度为启发式推测，\n"
        "# 口径需人工确认后再用于生产。切换数据源前已自动备份原文件为 *.bak-<时间戳>。\n"
    )
    for name, doc in draft.items():
        path = SEMANTIC_DIR / name
        body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
        path.write_text(header + body, encoding="utf-8")


def dt_stamp() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y%m%d%H%M%S")
