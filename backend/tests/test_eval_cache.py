"""评测复用缓存单测（FR-J-5）。

验证 _INVOKE_CACHE 在相同问题时命中缓存、不再重复调用图。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from eval.run_eval import _INVOKE_CACHE, _invoke, reset_telemetry


def test_invoke_caches_repeated_question() -> None:
    """相同 (session_id, question) 的第二次调用命中缓存，不重跑图。"""
    reset_telemetry()
    assert _INVOKE_CACHE == {}

    container = MagicMock()
    container.gateway.mock = True
    # 模拟 graph.invoke 返回一个简单 dict
    mock_final = {"status": "ok", "answer": "test", "sql": "SELECT 1"}
    container.graph.invoke = MagicMock(return_value=mock_final)
    container.graph.get_state = MagicMock(return_value=MagicMock(values=mock_final))

    # 第一次调用：走真实路径
    result1 = _invoke(container, "sess-1", "各区域成交总额", None)
    assert container.graph.invoke.call_count == 1

    # 第二次相同问题：应命中缓存，不再调用 graph.invoke
    result2 = _invoke(container, "sess-1", "各区域成交总额", None)
    assert container.graph.invoke.call_count == 1  # 仍是 1，没有增加

    # 两次返回同一个对象（缓存的是同一份 result）
    assert result1 is result2

    reset_telemetry()


def test_invoke_does_not_cache_injected_sql() -> None:
    """inject_sql 非空时不走缓存（safety 组需要每次独立执行）。"""
    reset_telemetry()
    assert _INVOKE_CACHE == {}

    container = MagicMock()
    container.gateway.mock = True
    container.gateway.complete_json = MagicMock()
    mock_final = {"status": "error", "answer": "blocked", "sql": "DROP TABLE orders"}
    container.graph.invoke = MagicMock(return_value=mock_final)
    container.graph.get_state = MagicMock(return_value=MagicMock(values=mock_final))

    _invoke(container, "sess-1", "dangerous", "DROP TABLE orders")
    _invoke(container, "sess-1", "dangerous", "DROP TABLE orders")

    # inject_sql 非空时每次都走真实路径，不缓存
    assert container.graph.invoke.call_count == 2

    reset_telemetry()


def test_invoke_caches_different_sessions_separately() -> None:
    """不同 session_id 的相同问题各自独立缓存。"""
    reset_telemetry()

    container = MagicMock()
    container.gateway.mock = True
    container.graph.invoke = MagicMock(return_value={"status": "ok", "answer": "test"})
    container.graph.get_state = MagicMock(return_value=MagicMock(values={"status": "ok"}))

    _invoke(container, "sess-A", "问题", None)
    _invoke(container, "sess-B", "问题", None)

    # 两个不同 session 各调一次
    assert container.graph.invoke.call_count == 2

    # 再次调用 sess-A 应命中缓存
    _invoke(container, "sess-A", "问题", None)
    assert container.graph.invoke.call_count == 2

    reset_telemetry()
