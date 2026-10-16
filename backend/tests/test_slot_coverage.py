"""槽位覆盖核对的单测。

锁住三件事：

1. 五类槽位各自的缺失都能被认出来。尤其是过滤条件——问华东却少写一个
   `region = '华东'`，语句照样合法、口径签名照样匹配、结果照样有数，这是
   答非所问里唯一全程无报错的形态。
2. 正确的语句不被误判。两类写法最容易造成误报：按输出别名分组
   （`GROUP BY 品类`，桩实现与模型都这么写）、以及被过滤到单一取值的字段
   又出现在维度槽位里（「华东区各品类」里的区域）。误报比漏报更伤——
   它会让一句完全正确的问句背上一条假告警。
3. 语义层没有的维度不猜。「支付方式」不该被配到 channel 上还判它已覆盖，
   那等于替用户做了一个他不认可的字段替换。

集成用例断言 validate 节点把结果写进 state：不上这条，前两点测过了也没人用。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.agent.nodes.validate import validate_node
from app.agent.slot_coverage import SlotGap, check_slot_coverage
from app.semantic.loader import Catalog


def _full_sql(as_of: dt.date | None = None) -> str:
    """一条「上个月华东区各品类成交总额 Top5」的完整语句，五类槽位全部落在语句里。

    日期字面量按数据截止日生成，不写死：写死的话每重播一次种子数据，依赖
    「上个月」换算的两条用例就红一次，而红的原因与实现无关。算法与语义层
    resolve_time_term 的 last_month 一致——上月起始到本月初一，左闭右开。
    """
    ref = as_of or dt.date.today()
    end = ref.replace(day=1)
    start = (end - dt.timedelta(days=1)).replace(day=1)
    return (
        "SELECT c.name AS 品类, SUM(oi.quantity * oi.unit_price) AS 成交总额 "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id JOIN categories c ON c.id = p.category_id "
        f"WHERE o.created_at >= '{start.isoformat()}' AND o.created_at < '{end.isoformat()}' "
        "AND o.region = '华东' "
        "GROUP BY 品类 ORDER BY 成交总额 DESC LIMIT 5"
    )


def _kinds(gaps: list[SlotGap]) -> set[str]:
    return {g.kind for g in gaps}


# ----------------------------------------------------------------------
# 五类槽位
# ----------------------------------------------------------------------


def test_all_slots_covered(catalog: Catalog, data_as_of) -> None:
    slots = {
        "dimensions": ["品类"],
        "filters": [{"field": "区域", "value": "华东"}],
        "time_range": "上个月",
        "top_n": 5,
        "order_by": {"field": "成交总额", "direction": "desc"},
    }
    assert (
        check_slot_coverage(
            _full_sql(data_as_of), slots, catalog=catalog, today=data_as_of
        )
        == []
    )


def test_missing_filter_is_detected(catalog: Catalog) -> None:
    """少写一个过滤条件：语句合法、有数据、无报错，只有这里能发现。"""
    sql = _full_sql().replace("AND o.region = '华东' ", "")
    gaps = check_slot_coverage(
        sql, {"filters": [{"field": "区域", "value": "华东"}]}, catalog=catalog
    )
    assert _kinds(gaps) == {"filter"}
    assert "华东" in gaps[0].expected


def test_missing_dimension_is_detected(catalog: Catalog) -> None:
    sql = "SELECT SUM(oi.quantity) AS 销量 FROM orders o JOIN order_items oi ON oi.order_id = o.id"
    gaps = check_slot_coverage(sql, {"dimensions": ["区域"]}, catalog=catalog)
    assert _kinds(gaps) == {"dimension"}


def test_missing_time_filter_is_detected(catalog: Catalog) -> None:
    sql = "SELECT o.region, COUNT(*) FROM orders o GROUP BY o.region"
    gaps = check_slot_coverage(sql, {"time_range": "上个月"}, catalog=catalog)
    assert _kinds(gaps) == {"time_range"}


def test_disjoint_time_range_is_detected(catalog: Catalog) -> None:
    sql = (
        "SELECT COUNT(*) FROM orders o "
        "WHERE o.created_at >= '2025-01-01' AND o.created_at < '2025-02-01'"
    )
    gaps = check_slot_coverage(sql, {"time_range": "上个月"}, catalog=catalog)
    assert _kinds(gaps) == {"time_range"}


def test_wrong_top_n_is_detected(catalog: Catalog) -> None:
    sql = "SELECT o.region, COUNT(*) AS c FROM orders o GROUP BY o.region ORDER BY c DESC LIMIT 3"
    assert _kinds(check_slot_coverage(sql, {"top_n": 10}, catalog=catalog)) == {"top_n"}
    # 条数对得上时不报
    assert check_slot_coverage(sql, {"top_n": 3}, catalog=catalog) == []


def test_order_direction_is_checked(catalog: Catalog) -> None:
    sql = "SELECT o.region, COUNT(*) AS c FROM orders o GROUP BY o.region ORDER BY c ASC"
    assert _kinds(
        check_slot_coverage(sql, {"order_by": {"field": "订单量", "direction": "desc"}}, catalog=catalog)
    ) == {"order_by"}
    assert (
        check_slot_coverage(sql, {"order_by": {"field": "订单量", "direction": "asc"}}, catalog=catalog)
        == []
    )


def test_missing_order_is_checked(catalog: Catalog) -> None:
    sql = "SELECT o.region, COUNT(*) AS c FROM orders o GROUP BY o.region"
    assert _kinds(
        check_slot_coverage(sql, {"order_by": {"field": "订单量", "direction": "desc"}}, catalog=catalog)
    ) == {"order_by"}


# ----------------------------------------------------------------------
# 不误判
# ----------------------------------------------------------------------


def test_group_by_output_alias_is_covered(catalog: Catalog) -> None:
    """按输出别名分组是 SQLite 的常规写法，不能判成没分组。"""
    sql = (
        "SELECT o.region AS 大区, SUM(oi.quantity * oi.unit_price) AS 成交总额 "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id GROUP BY 大区"
    )
    assert check_slot_coverage(sql, {"dimensions": ["区域"]}, catalog=catalog) == []


def test_filtered_field_needs_no_grouping(catalog: Catalog) -> None:
    """「华东区各品类」：区域被过滤到单一取值，再按它分组只会得到一组，不算缺失。"""
    sql = (
        "SELECT c.name AS 品类, SUM(oi.quantity * oi.unit_price) AS 成交总额 "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id JOIN categories c ON c.id = p.category_id "
        "WHERE o.region = '华东' GROUP BY 品类"
    )
    slots = {"dimensions": ["品类", "区域"], "filters": [{"field": "区域", "value": "华东"}]}
    assert check_slot_coverage(sql, slots, catalog=catalog) == []


def test_unknown_dimension_is_not_guessed(catalog: Catalog) -> None:
    sql = "SELECT o.channel, COUNT(*) FROM orders o GROUP BY o.channel"
    assert check_slot_coverage(sql, {"dimensions": ["支付方式"]}, catalog=catalog) == []


def test_empty_slots_yield_no_gaps(catalog: Catalog) -> None:
    assert check_slot_coverage("SELECT COUNT(*) FROM orders", {}, catalog=catalog) == []


def test_unparsable_sql_yields_no_gaps(catalog: Catalog) -> None:
    """解析不了的语句交给守卫，本模块不给结论——否则会与守卫的报错重复且更含糊。"""
    assert check_slot_coverage("SELECT FROM WHERE (((", {"top_n": 3}, catalog=catalog) == []


def test_guard_wrapped_query_keeps_order_and_limit(catalog: Catalog) -> None:
    """守卫套一层 LIMIT 后，分组与排序都在内层，不能因此判缺失。"""
    sql = (
        "SELECT * FROM ("
        "SELECT o.region, COUNT(*) AS c FROM orders o GROUP BY o.region ORDER BY c DESC LIMIT 5"
        ") AS _guard_limited LIMIT 1000"
    )
    slots = {"top_n": 5, "order_by": {"field": "订单量", "direction": "desc"}}
    assert check_slot_coverage(sql, slots, catalog=catalog) == []


# ----------------------------------------------------------------------
# 接入 validate 节点
# ----------------------------------------------------------------------


@pytest.mark.usefixtures("require_mock")
def test_validate_writes_slot_gaps_into_state(catalog: Catalog, guard: object) -> None:
    """核对结果必须进 state，否则前端与评测都拿不到，等于没做。"""
    state = {
        "sql": "SELECT o.region, SUM(oi.quantity) AS 销量 FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY o.region",
        "slots": {"time_range": "上个月"},
        "retry_count": 0,
        "fix_history": [],
    }
    update = validate_node(state, guard=guard, catalog=catalog)  # type: ignore[arg-type]
    gaps = update.get("slot_gaps") or []
    assert [g["kind"] for g in gaps] == ["time_range"]
    assert update.get("sql_error") is None, "槽位缺口不阻断执行"


@pytest.mark.usefixtures("require_mock")
def test_validate_reports_no_gaps_when_covered(
    catalog: Catalog, guard: object, data_as_of
) -> None:
    state = {
        "sql": _full_sql(data_as_of),
        "slots": {
            "dimensions": ["品类"],
            "filters": [{"field": "区域", "value": "华东"}],
            "time_range": "上个月",
            "top_n": 5,
            "order_by": {"field": "成交总额", "direction": "desc"},
        },
        "retry_count": 0,
        "fix_history": [],
    }
    update = validate_node(state, guard=guard, catalog=catalog, today=data_as_of)  # type: ignore[arg-type]
    assert update.get("slot_gaps") == []
