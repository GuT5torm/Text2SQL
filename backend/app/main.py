"""FastAPI 入口：路由挂载、CORS、静态资源、异常处理。

启动时会做一次预热：加载语义层、装配容器、编译 LangGraph 图。这些工作放在
启动阶段而不是首个请求里，是为了让「配置有问题」在进程启动时就暴露 —— 语义层
的校验错误会直接打印出来，而不是等用户提问才报一个看不懂的错。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import chat, semantic, session
from app.api.deps import get_container
from app.config import get_settings
from app.semantic.loader import SemanticError

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
# 前端构建产物。存在就挂载，不存在就只提供 API —— 开发时前端由 vite 单独跑，
# 交付时用 npm run build 产出 dist 后由本服务一并托管。
FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _setup_logging(settings.log_level)

    started = time.monotonic()
    try:
        container = get_container()
    except FileNotFoundError as exc:
        # 数据没造出来是最常见的首次启动问题，这里把修复命令直接打出来
        logger.error("%s", exc)
        raise
    except SemanticError as exc:
        logger.error("语义层配置校验未通过，进程不会启动：\n%s", exc)
        raise

    app.state.container = container
    logger.info(
        "服务已就绪（%.0f ms）：http://%s:%s/docs",
        (time.monotonic() - started) * 1000,
        "127.0.0.1",
        8000,
    )
    if container.gateway.mock:
        logger.warning(
            "当前为桩实现模式：未配置 LLM_API_KEY，生成的 SQL 来自固定模板。"
            "配置密钥后重启即可切换到真实模型。"
        )
    yield


app = FastAPI(
    title="Text2SQL 电商数据分析 Agent",
    description=(
        "用自然语言查询电商经营数据：语义层约束口径，SQL 安全沙箱约束执行，"
        "全流程可通过 SSE 观察。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(session.router)
app.include_router(semantic.router)


@app.get("/api/health", tags=["ops"])
def health() -> dict[str, Any]:
    """存活探针。同时报告当前是真实模型还是桩实现，便于部署后自查。"""
    container = get_container()
    return {
        "status": "ok",
        "llm_mock": container.gateway.mock,
        "model": container.settings.llm_model,
        "checkpointer": container.settings.checkpointer,
        "metrics": len(container.catalog.metrics),
        "tables": len(container.catalog.tables),
    }


@app.get("/api/stats", tags=["ops"])
def stats() -> dict[str, Any]:
    """本进程内的模型用量统计。用于演示时说明一次问答消耗了多少 token。"""
    return get_container().gateway.totals()


@app.exception_handler(SemanticError)
async def _semantic_error_handler(_request: Request, exc: SemanticError) -> JSONResponse:
    # 语义层配置错误属于开发期问题，返回 500 并带上完整的问题清单
    return JSONResponse(status_code=500, content={"detail": str(exc), "type": "SemanticError"})


if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    logger.info("未发现前端构建产物 %s，仅提供 API。开发时请单独启动 vite。", FRONTEND_DIST)
