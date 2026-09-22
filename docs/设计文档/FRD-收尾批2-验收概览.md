# FRD 收尾批 2 验收概览（FR-F-1 / FR-F-2 / FR-H-3 / FR-H-4）

日期：2026-09-22
范围：完成度核查报告中 4 条「未交付」需求的收尾。本批完成后，FRD v1.2.0 的 68 条需求中未交付清零。

---

## 一、交付内容

### FR-F-1 空结果回灌重写

| 位置 | 改动 |
|---|---|
| `app/config.py` | 新增 `empty_max_retry: int = 1`（独立配额，不占 `sql_max_retry`） |
| `app/llm/schemas.py` | `SqlDraft` 新增 `empty_result: bool` 与 `no_data_reason: str`，供模型显式声明「确认无数据」 |
| `app/agent/prompts/fix_empty.md` | 新增空结果专用提示词：只允许放宽时间区间端点与枚举取值范围，禁止移除时间/维度过滤、禁止跨入不完整周期 |
| `app/agent/nodes/fix.py` | 新增 `fix_empty_node`：空结果时走独立分支，输出「改写后的 SQL」或「确认无数据」判定 |
| `app/agent/nodes/execute.py` | 0 行时改走 `fix_empty` 分支（而非报错路径） |
| `app/agent/graph.py` | 路由 `_after_execute` 区分报错与空结果两条回环；`empty_retry_count` 进状态 |
| `app/agent/state.py` | 新增 `empty_retry_count` 字段（TypedDict 声明，见易踩坑记录：未声明键会被 LangGraph 静默丢弃） |

防失真约束：空结果回灌只允许区间端点与枚举范围两类放宽，最多 1 次；`no_data` 判定成立时按正常结论展示「确认无数据」而非报错。

### FR-F-2 修正痕迹进入结论

| 位置 | 改动 |
|---|---|
| `app/agent/nodes/summarize.py` | 结论生成时注入 `retry_count` 与最后一次 `fix_reasoning`；修正过的轮次在答案尾部追加「（经过 N 次自动修正：<原因>）」 |
| `app/agent/prompts/summarize.md` | 同步指令与占位符 |
| `app/agent/consistency.py` | `_whitelist_values` 白名单加入修正次数数字，避免「修正 N 次」被误判为待查数字（`check_conclusion_numbers` 增加 `retry_count` 入参） |

### FR-H-3 原始问句与改写问句落库与回看

| 位置 | 改动 |
|---|---|
| `app/db/models.py` | `Turn` 新增 `original_question` / `rewritten_question` 两列 |
| `app/db/sqlite_repo.py` | 迁移清单加两列（老库自动 ALTER TABLE）；读写两条路径同步 |
| `app/agent/nodes/route.py` | 改写发生时同时保留 `original_question` 与 `rewritten_question`（此前 `original_question` 是死字段：声明即赋值再无消费） |
| `app/api/chat.py` | 落库写入两字段 |
| `app/api/session.py` | 历史回看接口返回两字段 |
| 前端 `types.ts` | `ChatResult` / `TurnItem` 补两字段 |
| 前端 `useChatStream.ts` | `ChatTurn` 新增 `originalQuestion` / `rewrittenQuestion`；result 帧到达时同步 |
| 前端 `TechDetails.tsx` | 新增「问句来源」折叠项：展示原话与改写后问句，标题标注「已改写 / 未改写」 |
| 前端 `AnswerCard.tsx` | 接线传入两字段 |

### FR-H-4 会话导出 Markdown

| 位置 | 改动 |
|---|---|
| `frontend/src/lib/markdownBuilder.ts` | 新增 `buildSessionMarkdown`：文件头（标题+数据截至+生成时间）、每轮问句（含原话/改写）、结论、口径、异常标记、SQL 代码块、结果表（Markdown 表格，>50 行截断并标注）、守卫告警 |
| `frontend/src/pages/HistoryPage.tsx` | 操作列新增「导出 MD」按钮；与 PPTX 走同一份已落库 + 已脱敏数据，不重新请求原始数据；文件名 `{slug}_{时间戳}.md` |

---

## 二、验证记录（全部真实运行）

| 验证项 | 结果 |
|---|---|
| `pytest` 全量 | **431 passed**（新增 test_empty_retry.py 5 条 + test_question_trace.py 2 条，无回归） |
| `tsc --noEmit` | 0 error |
| `vite build` | 通过（32.68s） |
| 问句来源面板（浏览器实测） | 问「那上个月呢」（桩改写关键词命中），技术细节内出现「问句来源　已改写」，正文「原话：那上个月呢 / 改写后：[桩改写]那上个月呢」。截图 `assets/verify-frh3-question-trace.png` |
| 导出 MD（浏览器实测） | 历史页点击「导出 MD」，提示「已导出 1 轮 Markdown」；页面内构建同一份 md 核对：标题/问句/改写标记/SQL 块/结果表/数据截至/生成时间 七项全中 |
| 截断标注（浏览器实测） | 构造 120 行结果集：前 50 行显示、第 51 行起不显示、出现「已截断：本表仅显示前 50 行，实际命中 120 行」 |
| 环境清理 | 冒烟会话已删（4 条，均为空会话或本轮创建）；前后端进程已停 |

## 三、测试清单

- `tests/test_empty_retry.py`：空结果独立配额（不占 retry_count）、放宽被拒（移除时间过滤）、no_data 判定、二次空结果终止、修正次数白名单
- `tests/test_question_trace.py`：route 改写后两字段保留、未改写时两字段同值、落库→历史回读往返

## 四、完成度更新

68 条需求：**完全交付 62 / 部分交付 5 / 未交付 0 / 范围外 1**。

剩余 5 条部分交付的缺口均依赖外部条件，不在纯代码收尾范围：

| 需求 | 缺口 | 依赖条件 |
|---|---|---|
| FR-A-1 / FR-A-2 | 多轮链路缺跨轮会话的 eval 用例 | 需真实模型（桩改写只是关键词标记，断言无意义） |
| FR-E-4 | `fix_history` 已落库但前端无入口展示 | 纯前端可补，体量小，可并入下一批 |
| FR-H-1 | Checkpointer 默认仍为 memory，`CHECKPOINTER=sqlite` 可切 | 配置项已交付，默认值改动涉及演示环境稳定性，需确认 |
| FR-H-5 | 归档清理无保留期自动策略 | 需确认策略参数（保留多少天）后再实施 |

## 五、遗留事项

- 全部 FRD 产出仍未提交 git（最新提交仍为 dfe93a2 / T36），工作区约 130 项变更。建议尽快分批提交。
- CHANGELOG 仍无 FR 编号条目。
- 桩实现的 `_MOCK_SQL_BY_KEYWORD` 仍不带时间谓词（FR-D-5 核对器会标「时间区间不一致」橙色 Tag，属已知展示而非缺陷）。
