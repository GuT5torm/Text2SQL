# FRD 第 2 步（turns 补列与返回体扩展）验收概览

按 FRD v1.2.0 第九节实施顺序第 2 步交付。本批共用一次 `turns` 表补列与 `summarize_result` 扩展，把 5 条需求落在数据持久化与接口返回体上。

## 文件与改动一览

| 文件 | 变更 | 关联 FR |
|---|---|---|
| `backend/app/db/sqlite_repo.py` | `_TURN_MIGRATIONS` 定义 10 列迁移清单；`_ensure_turn_columns` 启动时幂等补列；`Turn` ORM 模型声明新字段；`add_turn` / `list_turns` 读写新列 | FR-H-2 |
| `backend/app/api/chat.py` | `_persist` 写入 `stages_json` / `original_sql` / `guard_warnings_json` / `retry_count` / `truncated` / `persist_row_count` / `source_tables_json` / `findings_json` / `baselines_json` / `node_path`；`_derive_node_path` 从 stages 去重提取节点序列 | FR-H-2 / E-1 / D-1 |
| `backend/app/agent/state.py` | `AgentState` 声明 `sql_reasoning` / `fix_reasoning` / `source_tables` / `data_freshness` / `persist_row_count` 等字段 | FR-C-1 / D-1 / D-2 |
| `backend/app/agent/graph.py` | `summarize_result` 返回体扩展 22→30+ 字段：补 `sql_reasoning` / `fix_reasoning` / `source_tables` / `data_freshness` / `persist_row_count` / `node_path` / `stages` / `original_sql` / `guard_warnings` / `retry_count` / `truncated` / `findings` / `baselines` / `partial_period` / `granularity` | FR-C-1 / D-1 / D-2 / H-2 / E-1 |
| `backend/app/api/session.py` | 轮次映射返回新字段（stages / original_sql / guard_warnings / retry_count / truncated / persist_row_count / source_tables / findings / baselines / node_path） | FR-H-2 |
| `frontend/src/api/types.ts` | `ChatResult` 补 `sql_reasoning` / `fix_reasoning` / `source_tables` / `data_freshness` / `persist_row_count` / `node_path` | 链路对齐 |
| `frontend/src/hooks/useChatStream.ts` | `turnToChatTurn` 同步填新字段（旧记录读为空，不报错） | FR-H-2 |

## 5 条需求落地

### FR-H-2 历史回看字段补全

`turns` 表补 10 列：

| 列 | 类型 | 用途 |
|---|---|---|
| `stages_json` | TEXT | 阶段时间线与每节点耗时 |
| `original_sql` | TEXT | 守卫改写前的语句 |
| `guard_warnings_json` | TEXT | 本轮告警 |
| `retry_count` | INTEGER | 修正次数 |
| `truncated` | INTEGER | 结果是否被行数上限截断 |
| `persist_row_count` | INTEGER | 实际落库的行数 |
| `source_tables_json` | TEXT | 来源表 |
| `findings_json` | TEXT | 异常标记（第 4+5 批填充） |
| `baselines_json` | TEXT | 对比基准值（第 4+5 批填充） |
| `node_path` | TEXT | 本轮走过的节点序列 |

迁移方式：`_ensure_turn_columns` 在每次启动时执行 `PRAGMA table_info` 查已有列，缺失的逐列 `ALTER TABLE ADD COLUMN`，已有列跳过，重复执行不报错。`insight` / `highlights` / `intent` 不补列（理由见 FRD 4.8 逐字段判断）。

`truncated` 语义重定义：true 表示执行时被 `SQL_MAX_ROWS` 截断；落库上限 `PERSIST_ROWS` 截断不算 truncated，改由 `persist_row_count` 与 `row_count` 的差值反映。

### FR-E-1 阶段时间线随轮次落库

`stages` 在 `summarize_result` 返回体中已有，本批将其落库到 `stages_json`。`_persist` 调用 `dump_json(result.get("stages") or [])` 写入。历史回看时 `session.py` 的轮次映射返回 `"stages": _loads(t.stages_json) or []`，刷新页面后打开旧轮次仍能看到完整阶段、每节点耗时与修正次数。

`node_path` 由 `_derive_node_path` 从 stages 提取节点名去重 + 保序得到，同一节点多次出现（fix 回环）只取一次。

### FR-D-1 来源表展示

`source_tables` 取自实际执行语句的解析结果（守卫 `guard.py::_check_tables` 已有表名提取逻辑），在 `summarize_result` 返回体中作为独立字段返回。`_persist` 落库到 `source_tables_json`。前端 `AnswerCard` 的口径定义面板标题处显示「N 项指标 · M 张来源表」。

### FR-D-2 数据时效的常驻与超期提示

`data_freshness` 在 `summarize_result` 返回体中返回，包含数据截止日期与距今天数。前端 `MetricBadge` 常驻显示「数据截至 YYYY-MM-DD（N 天前）」，超过配置的时效阈值时高亮。

### FR-C-1 写法说明落位到 SQL 面板

`sql_reasoning` 来自 generate 节点的 `SqlDraft.reasoning`，`fix_reasoning` 在经 fix 修正时给出最后一次的修正理由。两者在 `summarize_result` 返回体中作为独立字段返回，同时 `sql_reasoning` 也写入 `metrics_json` 供旧版接口读取。前端 `SqlPanel` 展示「写法说明」折叠项。

## 验收基线

| 项 | 结果 |
|---|---|
| pytest | 206 passed（当时基线） |
| recall | 32/32 |
| pipeline | 6/9（skipped=1） |
| safety | 8/8 |
| datalineage | 3/3 |

迁移用例覆盖「旧库 + 新代码」场景：空列读为 None / 空列表 / 0 / false，前端按字段缺失降级不报错。

## 已登记的待办与下一步

- 第 3 步（纯前端呈现）：FR-B-1 / B-2 / B-4 / C-3 / D-3 / D-4 / E-2 / I-1 / P-1~P-4
- 第 4+5 步（服务端计算）：FR-M-1~3 + FR-L-1~3 + FR-L-5
- 第 6 步（反馈与汇报）：FR-G-1~4 + FR-N-1~4
- 第 7 步（订阅与审计）：FR-O-1~5 + FR-I-2~4 + FR-D-5 + FR-B-3 + FR-E-3 + FR-J-3
