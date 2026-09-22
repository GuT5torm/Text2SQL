"""进程内轻量计数器（FR-J-3）。

Gateway 负责 LLM 用量，但「对比期额外跑了几次 SQL」不经过 LLM 网关，
放在这里作为进程级单例：baselines 每执行一条环比 / 同比对比查询就加一，
/api/stats 与 eval 报告读同一个数。

计数器刻意只做内存态：它是可观测性指标而不是业务数据，进程重启归零可以
接受，落库反而引入与应用库的写竞争。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Counters:
    comparison_queries: int = 0


_global = Counters()


def counters() -> Counters:
    return _global


def record_comparison_query(n: int = 1) -> None:
    """记录一次对比期（环比 / 同比）额外 SQL 查询。"""
    _global.comparison_queries += n


def reset_counters() -> None:
    """评测集每组跑完时归零，避免组间串数。"""
    _global.comparison_queries = 0
