"""FR-L-5 一单多退的计数口径单测。

为什么放在单测而不是评测集：评测集跑的是业务库 data/ecommerce.db，构造数据
要把「一个订单两笔退款」写进库里才能复现，那会污染演示数据集。构造数据属于
「表设计允许但当前数据里没有」的情形，只有临时库能干净地验证，因此用最小
schema 建临时库。

验证的是 FR-L-5 关心的那件事：按退款原因分组时，「按笔数」与「按订单数」
不是同一个数。当前业务数据里 605 笔退款对应 605 个订单、无重复，两种口径
算出来一样，测不出差别。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.semantic.loader import Catalog, load_catalog

# 构造数据：订单 1 发生三笔退款（质量问题两笔 + 发错货一笔），订单 2 一笔
# （质量问题），订单 3 无退款。合计 4 笔退款、2 个退款订单。
# 同一个订单在同一种原因下退两次是最容易被忽略的情形——它让「按笔」与
# 「按单」在同一个原因分组里就分出差别，而不必靠跨原因求和才看得出来。
ORDERS = [(1, "paid"), (2, "paid"), (3, "paid")]
REFUNDS = [
    (1, 1, "质量问题"),
    (2, 1, "发错货"),
    (3, 1, "质量问题"),
    (4, 2, "质量问题"),
]


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog()


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "multi_refund.db")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
    con.execute(
        "CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, reason TEXT)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?)", ORDERS)
    con.executemany("INSERT INTO refunds VALUES (?, ?, ?)", REFUNDS)
    con.commit()
    yield con
    con.close()


def _by_reason(con: sqlite3.Connection, expression: str) -> dict[str, int]:
    rows = con.execute(
        "SELECT r.reason AS reason, "
        f"{expression} AS cnt "
        "FROM orders o JOIN refunds r ON r.order_id = o.id "
        "GROUP BY r.reason ORDER BY r.reason"
    ).fetchall()
    return {reason: cnt for reason, cnt in rows}


def test_dimension_declares_its_counting_grain(catalog: Catalog) -> None:
    """口径必须写在维度上，不能留给生成环节自行选择。"""
    dimension = catalog.dimension_by_name["refund_reason"]
    assert dimension["count_grain"] == "refund_row"
    assert set(dimension["count_expressions"]) == {"refund_row", "order"}


def test_by_refund_row_sums_to_total_refunds(db: sqlite3.Connection, catalog: Catalog) -> None:
    expression = catalog.dimension_by_name["refund_reason"]["count_expressions"]["refund_row"]
    result = _by_reason(db, expression)
    assert result == {"发错货": 1, "质量问题": 3}
    assert sum(result.values()) == len(REFUNDS)


def test_by_order_double_counts_multi_refund_orders(
    db: sqlite3.Connection, catalog: Catalog
) -> None:
    """一单多退时按订单数计会把同一订单计入多个原因，各原因之和大于退款订单数。"""
    expression = catalog.dimension_by_name["refund_reason"]["count_expressions"]["order"]
    result = _by_reason(db, expression)
    # 订单 1 在「质量问题」下退两次只算一个订单，在「发错货」下再算一次
    assert result == {"发错货": 1, "质量问题": 2}
    distinct_refunded_orders = db.execute(
        "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN refunds r ON r.order_id = o.id"
    ).fetchone()[0]
    assert distinct_refunded_orders == 2
    assert sum(result.values()) > distinct_refunded_orders


def test_two_grains_differ_within_the_same_reason(
    db: sqlite3.Connection, catalog: Catalog
) -> None:
    """同一个原因分组里两种口径就不同：3 笔 vs 2 个订单。

    这是「两种口径不可混用」最直接的证据——不需要跨原因求和，光看质量问题
    这一组就能看出 3 与 2 的差别。
    """
    expressions = catalog.dimension_by_name["refund_reason"]["count_expressions"]
    by_row = _by_reason(db, expressions["refund_row"])
    by_order = _by_reason(db, expressions["order"])
    assert by_row["质量问题"] == 3
    assert by_order["质量问题"] == 2


def test_grain_note_is_documented(catalog: Catalog) -> None:
    """口径说明要能被口径面板直接读到，写进注释等于没写。"""
    note = catalog.dimension_by_name["refund_reason"].get("grain_note") or ""
    assert "一单多退" in note or "多次退款" in note


def test_count_expressions_execute_against_business_db(
    business_repo: Any, catalog: Catalog
) -> None:
    """维度声明的两种计数表达式必须能在真实库上跑通。

    表达式里写了表别名（r.id / o.id），只有实际执行才能发现别名与 JOIN
    写法对不上。用真实库的另一层含义是核对默认口径：当前数据里一笔退款对应
    一个订单，按笔与按单应当算出同一个总数。
    """
    expressions = catalog.dimension_by_name["refund_reason"]["count_expressions"]
    by_row = business_repo.execute_readonly(
        "SELECT r.reason AS 原因, "
        f"{expressions['refund_row']} AS 笔数 "
        "FROM orders o JOIN refunds r ON r.order_id = o.id "
        "GROUP BY r.reason ORDER BY 笔数 DESC"
    ).rows
    by_order = business_repo.execute_readonly(
        "SELECT r.reason AS 原因, "
        f"{expressions['order']} AS 订单数 "
        "FROM orders o JOIN refunds r ON r.order_id = o.id "
        "GROUP BY r.reason ORDER BY 订单数 DESC"
    ).rows
    assert by_row and len(by_row) == len(by_order)
    total_row = business_repo.execute_readonly(
        "SELECT COUNT(*) AS n FROM refunds"
    ).rows[0]["n"]
    assert sum(r["笔数"] for r in by_row) == total_row
    assert {r["原因"] for r in by_row} == {r["原因"] for r in by_order}
