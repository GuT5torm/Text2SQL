"""分层约束自查。

设计文档里的三条硬约束靠人工评审容易漏，改成可执行检查后每次跑测试都会核对：

1. agent/nodes 下不出现数据库连接，只能调 repository 暴露的方法；
2. semantic 下不出现 LLM 调用与 SQL 执行，保持纯函数；
3. 密钥只出现在 .env，代码与配置中不出现字面量。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"

# 出现即视为越界：engine 建连、sqlite_repo 是实现类、models 是 ORM 表
FORBIDDEN_IN_NODES = {
    "sqlalchemy",
    "sqlite3",
    "create_engine",
    "app.db.engine",
    "app.db.sqlite_repo",
    "app.db.models",
}
# 语义层是纯函数：不调模型、不碰库、不参与编排
FORBIDDEN_IN_SEMANTIC = {"sqlalchemy", "sqlite3", "openai", "httpx", "app.llm", "app.db", "app.agent"}

_SECRET_NAME = re.compile(r"(key|token|secret|password|passwd)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"^sk-[A-Za-z0-9]{16,}$")


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module)
                roots.add(node.module.split(".")[0])
            for alias in node.names:
                roots.add(alias.name)
    return roots


def _py_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if p.name != "__init__.py")


def test_nodes_do_not_touch_database_connection() -> None:
    offenders = []
    for path in _py_files(APP_DIR / "agent" / "nodes"):
        hits = _imported_roots(path) & FORBIDDEN_IN_NODES
        if hits:
            offenders.append(f"{path.relative_to(BACKEND_DIR)}: {sorted(hits)}")
    assert not offenders, "编排节点不得直接建连，只能依赖 repository：\n" + "\n".join(offenders)


def test_semantic_layer_is_pure() -> None:
    offenders = []
    for path in _py_files(APP_DIR / "semantic"):
        hits = _imported_roots(path) & FORBIDDEN_IN_SEMANTIC
        if hits:
            offenders.append(f"{path.relative_to(BACKEND_DIR)}: {sorted(hits)}")
    assert not offenders, "语义层不得调用模型或执行 SQL：\n" + "\n".join(offenders)


def test_no_secret_literals_in_source() -> None:
    offenders: list[str] = []
    for path in _py_files(APP_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            target = None
            value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
            if not target or value is None:
                continue
            if not _SECRET_NAME.search(target):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
                offenders.append(f"{path.relative_to(BACKEND_DIR)}: {target} 被赋了字符串字面量")
    assert not offenders, "密钥只允许出现在 .env：\n" + "\n".join(offenders)


def test_no_secret_like_strings_anywhere_in_app() -> None:
    offenders = []
    for path in [*APP_DIR.rglob("*.py"), *APP_DIR.rglob("*.yaml")]:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in re.findall(r"sk-[A-Za-z0-9]{16,}", line):
                if _SECRET_VALUE.match(match):
                    offenders.append(f"{path.relative_to(BACKEND_DIR)}:{lineno}")
    assert not offenders, "疑似密钥字面量：\n" + "\n".join(offenders)


def test_env_example_contains_no_real_secret() -> None:
    """样例文件是会被提交进仓库的，任何非空密钥都算事故。"""
    example = BACKEND_DIR / ".env.example"
    if not example.exists():
        pytest.skip(".env.example 不存在")
    for lineno, line in enumerate(example.read_text(encoding="utf-8").splitlines(), 1):
        if "=" not in line or line.strip().startswith("#"):
            continue
        _key, _, value = line.partition("=")
        value = value.strip()
        assert not _SECRET_VALUE.match(value), f".env.example:{lineno} 含疑似密钥"
