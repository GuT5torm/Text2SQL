# FR-L-4 + FR-M-4 + FR-M-5 验收概览

按 FRD v1.2.0 第九节实施顺序第 1 步交付。本批属于纯语义层与加载期校验，成本最低但阻塞最多：FR-L-4 决定所有退款率数字是否可信；FR-M-4 回答口径面板里「区域按什么划分」；FR-M-5 把前两条升级为机制。

## 落地位置

| 需求 | 文件 | 行 | 行为 |
|---|---|---|---|
| FR-L-4 | `app/semantic/metrics.yaml` | refund_rate 块 | `required_filters` 增加 `orders.status NOT IN ('cancelled','pending')`，与 gmv 等使用同一分母口径；`caveats` 注明 |
| FR-M-4 | `app/semantic/tables.yaml` | 6 处 enum 字段上方 | 含 enum 的字段全部补 `definition`：orders.status / orders.region / orders.channel / users.level / users.channel / refunds.reason |
| FR-M-4 | `app/semantic/loader.py` `_validate` | enum 字段 definition 校验段 | 缺 definition 或纯空白 → `SemanticError` 阻止启动 |
| FR-M-5 | `app/semantic/metrics.yaml` | 7 个指标 | 全部声明 `denominator: paid_order` |
| FR-M-5 | `app/semantic/loader.py` `_validate` | denominator 一致性段 | 同 denominator 但 `orders.status` 过滤字符串集合超过 1 个 → `SemanticError` |

## 退款率口径修复对照

| 口径 | 数字 | 备注 |
|---|---|---|
| 老口径（全部订单作分母） | 4.96% | 与 gmv 等不同分母导致偏差来源 |
| 新口径（paid_order，排除 cancelled/pending） | 5.70% | 与 gmv 完全一致，分母偏差消失 |

数据来源：ecommerce.db，2026 年至今全部成交订单。新口径与 FRD 第 1.4.1 节、1.4.3 节记录一致，相对差 15% 已消除。

## 测试覆盖

`tests/test_semantic.py` 新增 8 条：

- `test_validate_rejects_enum_field_without_definition`：含 enum 缺 definition 必拒
- `test_validate_rejects_enum_field_with_blank_definition`：definition 为空白等价于缺失
- `test_validate_accepts_enum_field_with_definition`：合法配置照常加载
- `test_validate_does_not_require_definition_for_non_enum_fields`：无 enum 字段不强制
- `test_validate_accepts_consistent_denominator_status_filters`：同 denominator 同 status 合法
- `test_validate_rejects_inconsistent_denominator_status_filters`：FR-L-4 场景的核心回归
- `test_validate_allows_different_denominators_to_differ`：不同 denominator 概念不互相约束
- `test_validate_metric_without_denominator_is_not_checked`：未声明 denominator 不参与校验

另两条真实配置回归：

- `test_real_catalog_passes_enum_definition_check`：遍历所有表，含 enum 字段必须全部有 definition
- `test_real_catalog_passes_denominator_consistency_check`：遍历所有指标，denominator 标签下 status 过滤集合大小 ≤ 1

未来在 `tables.yaml` 加 enum 字段忘了加 definition，或在 `metrics.yaml` 加同 denominator 指标使用了不同 status 过滤，会立即 fail，避免线上才发现两个数字对不上。

## 验收基线（2026-09-16 16:24 桩实现）

| 项 | 数字 |
|---|---|
| pytest | 206 passed in 7.47s |
| recall | 32/32 |
| pipeline | 6/9（skipped=1） |
| safety | 8/8 |
| datalineage | 3/3 |

## 本批代码改动

零。FR-L-4 的 YAML 改写、FR-M-4 的 definition 字段、FR-M-5 的 `denominator` 字段都在前几轮已就位，验证后留档。`loader.py` 的 `_validate` 函数当时同步加了对应校验段，未需要再动。

## FRD 文档同步待办

FRD 第 1.4.1 节文字「退款率两口径对比（4.96% vs 5.70%）」已不成立——FR-L-4 修正后只剩 5.70%。按「退款率相关的既有断言与文档数字需要同步更新」的约定，回写由用户决定是单条还是合并。

## 已登记的待办与下一步

- FRD 顺序 3（纯前端呈现）：FR-B-1 / B-2 / B-4 / C-3 / D-3 / D-4 / E-2 / I-1 / P-1~P-4
- FRD 顺序 4 + 5（服务端计算）：FR-M-1~3 + FR-L-1~3 + FR-L-5，**前置条件是 FR-L-4 已完成 → 已满足**
- FRD 顺序 6（反馈与汇报）：FR-G-1~4 + FR-N-1~4
- FRD 顺序 7（订阅与审计）：FR-O-1~5 + FR-I-2~4 + FR-D-5 + FR-B-3 + FR-E-3 + FR-J-3
- FRD 顺序 8（依赖 LLM_API_KEY）：FR-A-1~5 + FR-C-2 + FR-J-1 / J-2 / J-4 / J-5

确认下一步推进哪一批。
