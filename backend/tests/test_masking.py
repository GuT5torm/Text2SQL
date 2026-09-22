"""FR-I-3 脱敏管道用例：掩码规则、投影解析、幂等与标记字段。"""

from __future__ import annotations

import pytest

from app.db.masking import mask_result, mask_scalar


# ---------------------------------------------------------------------------
# mask_scalar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("phone", "13800001234", "138****1234"),
        ("idcard", "310100199001011234", "310100********1234"),
        ("other", "anything", "******"),
    ],
)
def test_mask_scalar_rules(kind: str, value: str, expected: str) -> None:
    assert mask_scalar(kind, value) == expected


def test_mask_scalar_non_string_passthrough() -> None:
    assert mask_scalar("phone", 13800001234) == 13800001234
    assert mask_scalar("phone", None) is None


def test_mask_scalar_short_values_use_placeholder() -> None:
    assert mask_scalar("phone", "12345") == "******"
    assert mask_scalar("idcard", "123456789") == "******"


def test_mask_scalar_is_idempotent() -> None:
    once = mask_scalar("phone", "13800001234")
    assert mask_scalar("phone", once) == once


# ---------------------------------------------------------------------------
# mask_result
# ---------------------------------------------------------------------------


def test_mask_result_dict_rows_with_physical_columns(catalog) -> None:
    result = {
        "sql": "SELECT u.phone AS phone, u.id_card AS id_card, u.name FROM users u LIMIT 5",
        "columns": ["phone", "id_card", "name"],
        "rows": [
            {"phone": "13900000001", "id_card": "31010020000101001X", "name": "张三"},
            {"phone": "13900000002", "id_card": "31010020000101002X", "name": "李四"},
        ],
    }
    mask_result(result, catalog)
    assert result["rows"][0]["phone"] == "139****0001"
    assert result["rows"][0]["id_card"] == "310100********001X"
    assert result["rows"][0]["name"] == "张三"
    assert result["masked"] is True
    assert "users.phone" in result["masked_fields"]
    assert "users.id_card" in result["masked_fields"]


def test_mask_result_chinese_alias_hits_projected_column(catalog) -> None:
    # 中文别名 AS 手机号：必须按投影表达式而不是结果列名匹配
    result = {
        "sql": "SELECT u.phone AS 手机号, u.id_card AS 证件号 FROM users u LIMIT 5",
        "columns": ["手机号", "证件号"],
        "rows": [{"手机号": "13900000003", "证件号": "31010020000101003X"}],
    }
    mask_result(result, catalog)
    assert result["rows"][0]["手机号"] == "139****0003"
    assert result["rows"][0]["证件号"] == "310100********003X"


def test_mask_result_list_rows(catalog) -> None:
    result = {
        "sql": "SELECT u.name, u.phone FROM users u LIMIT 5",
        "columns": ["name", "phone"],
        "rows": [["王五", "13900000004"]],
    }
    mask_result(result, catalog)
    assert result["rows"][0] == ["王五", "139****0004"]


def test_mask_result_no_sensitive_columns(catalog) -> None:
    result = {
        "sql": "SELECT region, COUNT(*) AS n FROM orders GROUP BY region LIMIT 100",
        "columns": ["region", "n"],
        "rows": [{"region": "华东", "n": 100}],
    }
    mask_result(result, catalog)
    assert result["masked"] is False
    assert result["masked_fields"] == []
    assert result["rows"][0]["n"] == 100


def test_mask_result_is_idempotent(catalog) -> None:
    result = {
        "sql": "SELECT u.phone AS phone FROM users u LIMIT 5",
        "columns": ["phone"],
        "rows": [{"phone": "13900000005"}],
    }
    mask_result(result, catalog)
    first = result["rows"][0]["phone"]
    mask_result(result, catalog)
    assert result["rows"][0]["phone"] == first
    assert result["masked_fields"] == ["users.phone"]


def test_mask_result_without_sql_falls_back_to_column_name(catalog) -> None:
    # 历史回放等场景可能没有 SQL 文本，按结果列物理名 / 显示名兜底
    result = {
        "sql": "",
        "columns": ["phone"],
        "rows": [{"phone": "13900000006"}],
    }
    mask_result(result, catalog)
    assert result["rows"][0]["phone"] == "139****0006"
