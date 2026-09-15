"""Prompt 装载与上下文格式化。

Prompt 正文放在同目录的 .md 文件里，不写进 Python 字符串。原因是 Prompt 的
迭代频率远高于代码：外置之后改 Prompt 不需要动代码，也便于在实训环节直观看
「同一段 Prompt 改动前后模型输出的差别」，还能直接 diff。

占位符用 {name} 形式，但**不用 str.format**。Prompt 正文里有 JSON 示例，
花括号会被 format 当成占位符处理。这里用正则只替换形如 {word} 的片段，
JSON 里的 {"key": ...} 不受影响。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

PROMPT_DIR = Path(__file__).resolve().parent

_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def render_prompt(name: str, **kwargs: Any) -> str:
    """读取 prompts/<name>.md 并替换 {占位符}。未提供的占位符原样保留，
    便于从渲染结果直接看出漏传了哪个字段。"""
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    return _PLACEHOLDER_RE.sub(
        lambda m: str(kwargs[m.group(1)]) if m.group(1) in kwargs else m.group(0), text
    )


def format_history(messages: Iterable[Any], window: int = 6) -> str:
    """把最近若干轮对话压成文本，供 Prompt 使用。

    只取角色与内容，不带工具调用等内部结构，避免把编排细节泄漏进 Prompt。
    """
    items = list(messages or [])
    if not items:
        return ""
    lines: list[str] = []
    for msg in items[-window:]:
        role = getattr(msg, "type", None) or getattr(msg, "role", "unknown")
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            role = msg.get("role", role)
            content = msg.get("content", "")
        if not content:
            continue
        label = {"human": "用户", "ai": "助手", "system": "系统"}.get(str(role), str(role))
        lines.append(f"{label}：{str(content).strip()}")
    return "\n".join(lines)


def format_rows(
    rows: list[dict[str, Any]], columns: list[str], limit: int = 20
) -> str:
    """把结果集预览渲染成紧凑表格文本。

    这里刻意不用 JSON：结果集行数多时 JSON 会迅速吃掉上下文预算，
    而表格形态对模型判断「哪个是维度、哪个是度量」也更直观。
    """
    if not rows:
        return "（无数据）"
    head = " | ".join(columns)
    sep = " | ".join("---" for _ in columns)
    body = "\n".join(
        " | ".join(_cell(r.get(c)) for c in columns) for r in rows[:limit]
    )
    more = f"\n… 另有 {len(rows) - limit} 行未展示" if len(rows) > limit else ""
    return f"{head}\n{sep}\n{body}{more}"


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)
