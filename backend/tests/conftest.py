"""pytest 共享夹具。

三条原则：

1. 语义层与守卫是会话级夹具。三个 YAML 只在首次用时加载一次，守卫的字段
   白名单直接取自语义层——这样用例断言的是真实配置，配置改了用例会跟着红，
   不会出现「测试用一套假表、线上跑另一套真表」的偏差。
2. 应用库一律用临时目录。会话元数据写入 backend/data/app.db 的真实库会让
   单元测试污染演示数据，因此用 APP_DB_PATH 环境变量把配置指向 tmp_path。
3. 依赖真实模型的用例主动跳过。本项目当前以桩模式开发，接入密钥后这些用例
   会跳过而不是失败——它们断言的是桩实现的分支，不是真实模型的能力。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.config import get_settings
from app.db.engine import create_app_engine
from app.db.guard import SqlGuard
from app.db.sqlite_repo import SqliteQueryRepository, SqliteSessionStore
from app.semantic.loader import Catalog, load_catalog

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return load_catalog()


@pytest.fixture(scope="session")
def guard(catalog: Catalog) -> SqlGuard:
    """用真实语义层构造的守卫。max_rows 与配置一致。"""
    return SqlGuard.from_catalog(catalog, max_rows=get_settings().sql_max_rows)


@pytest.fixture(scope="session")
def data_as_of(business_repo: SqliteQueryRepository) -> date | None:
    """时间词锚点：库内数据截止日，与生产同一来源。

    相对时间词用例按它换算，不按系统日期——按系统日期换算会在数据陈旧后
    落到没有数据的月份，这类失败与实现无关，换一天跑就换一个结果。
    """
    return business_repo.data_as_of()


@pytest.fixture()
def small_guard(catalog: Catalog) -> SqlGuard:
    """行数上限压到 10 的守卫，用于 LIMIT 边界值的正反例。"""
    return SqlGuard.from_catalog(catalog, max_rows=10)


@pytest.fixture()
def isolated_app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把应用库指向临时文件。配置带 lru_cache，进出夹具都要清缓存。"""
    path = tmp_path / "app_test.db"
    monkeypatch.setenv("APP_DB_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


@pytest.fixture()
def store(isolated_app_db: Path) -> SqliteSessionStore:
    return SqliteSessionStore(engine=create_app_engine(isolated_app_db))


@pytest.fixture(scope="session")
def business_repo() -> SqliteQueryRepository:
    """业务库只读连接。业务库缺失（未造数）时整组跳过而不是报错。"""
    settings = get_settings()
    if not settings.business_db_path.exists():
        pytest.skip(f"业务库不存在，请先运行 python data/seed.py：{settings.business_db_path}")
    return SqliteQueryRepository()


@pytest.fixture()
def client(isolated_app_db: Path, monkeypatch: pytest.MonkeyPatch):
    """装配一个指向临时应用库的 FastAPI 测试客户端。"""
    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.api.deps import Container, get_container, reset_container

    settings = get_settings()
    if not settings.business_db_path.exists():
        pytest.skip(f"业务库不存在，请先运行 python data/seed.py：{settings.business_db_path}")

    reset_container()
    container: Container = get_container()

    # health 与 stats 直接在函数体内取容器，不走依赖注入，需要一并替换
    monkeypatch.setattr(main_module, "get_container", lambda: container)
    app = main_module.app
    app.dependency_overrides[get_container] = lambda: container
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def require_mock() -> None:
    """编排链路相关的用例只在桩模式下有意义。"""
    settings = get_settings()
    if not settings.llm_mock:
        pytest.skip("已配置真实模型，桩实现相关用例跳过")
