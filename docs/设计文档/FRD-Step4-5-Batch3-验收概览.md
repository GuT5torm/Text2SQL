# FRD 第 4+5 步第三批（前端接入与接口回归）验收概览

按 FRD v1.2.0 第九节实施顺序第 4+5 步第三批执行。本批把第二批的 `baselines` / `findings` / 下钻接口接入前端，并补齐接口层回归用例。代码落地时间：2026-09-17 10:27 至 10:36。

## 文件与改动一览

### 新增文件

| 文件 | 内容 | 关联 FR |
|---|---|---|
| `frontend/src/components/FindingList.tsx` | 异常标记列表。严重度与规则名各一枚标签，正文给出值、基准与偏离；下钻入口在 `onDrilldown` 未传时不渲染 | FR-L-1 / L-2 |
| `frontend/src/api/drilldown.ts` | `requestDrilldown`，`POST /api/drilldown`，只取问句不当作结果渲染 | FR-L-2 |

### 修改文件

| 文件 | 变更 | 关联 FR |
|---|---|---|
| `frontend/src/components/MetricBadge.tsx` | 接入 `baselines`：环比与同比各一枚标签（涨红跌绿），不可比时显示「不可比」并给对比期窗口；`partial_period` 为真时显示「本期未完整」；无基准时呈现 `note` 与 `missing_reasons` | FR-M-1 / M-2 |
| `frontend/src/components/ResultTable.tsx` | 非数值列可点击，回调上抛 `{dimension, value}`；数值列不做下钻入口 | FR-L-2 |
| `frontend/src/components/AnswerCard.tsx` | 装配 `MetricBadge` / `FindingList` / `DefinitionPanel` / `ChartPanel` / `TechDetails` / `ResultTable`；`handleFindingDrill` 与 `handleCellDrill` 两个下钻触发点 | FR-L-1 / L-2 / M-1 |
| `frontend/src/components/ResultPane.tsx` | 右侧结果面板同样接入两个下钻触发点 | FR-L-2 |
| `frontend/src/components/ChatPanel.tsx` | 透传 `onDrilldown` | FR-L-2 |
| `frontend/src/pages/ChatPage.tsx` | `handleDrilldown`：先换问句再走正常提问流程；接口返回 `available=false` 时弹出原因 | FR-L-2 |
| `frontend/src/api/types.ts` | 补 `Baselines` / `BaselineWindow` / `Finding` / `DrilldownRequest` / `DrilldownResponse`；`ChatResult` 补 `baselines` / `partial_period` / `granularity` / `findings`；`TurnItem` 三项设为可选 | 链路对齐 |
| `frontend/src/hooks/useChatStream.ts` | 历史回看填 `baselines` 与 `findings`，`partial_period` 缺省为假 | FR-H-2 |
| `backend/tests/test_api.py` | 新增 3 条：`/chat/sync` 返回体带基准与标记、历史回看保留两者、下钻问句可执行 | 验收 |

## 需求落地

### FR-M-1 / M-2 前端呈现

`MetricBadge` 接入 `baselines` 字段：
- 环比（mom）与同比（yoy）各一枚标签，涨红跌绿只表方向不表好坏
- `comparable=false` 时显示「不可比」并在 Tooltip 给出对比期窗口与本期值
- `partial_period=true` 时显示「本期未完整」
- 无基准时把 `note` 与 `missing_reasons` 一并呈现，不留白

### FR-L-1 异常标记的结构化

`FindingList.tsx` 渲染 `findings[]`：
- 严重度（提示/关注）与规则名（deviation/rank/absolute）各一枚标签
- 正文给出值、基准与偏离度
- 界面顺序与判定顺序一致，前端不重新排序、不做阈值判断
- 下钻入口在 `onDrilldown` 未传时不渲染（历史回看没有可提交的会话）

### FR-L-2 从标记与单元格发起下钻

两个触发点：
- **finding 触发**：点击 `FindingList` 的异常标记，拼装 `{source: 'finding', metric, dimension, slice, time_label}` 提交下钻接口，返回问句后走正常提问流程
- **cell 触发**：点击 `ResultTable` 的非数值列单元格，拼装 `{source: 'cell', metric, dimension, slice, time_label}` 提交

`ChatPage.handleDrilldown` 先换问句再走 `/api/chat` 链路，接口返回 `available=false` 时弹出原因而非静默失败。

## 验证

| 项 | 结果 |
|---|---|
| `tsc --noEmit` | 通过 |
| 后端 `pytest` | 320 passed |
| 端到端（TestClient + 真实 ecommerce.db） | 「上个月各区域的成交总额」：`baselines.mom` = 窗口 2026-07-01 至 2026-08-01、值 2763231.72、偏离 +5.58%、`comparable=true`；`findings` 3 条，首条为「大区=华东，值 15789290.26，基准 7116766.21，偏离 +121.9%，关注 / rank」 |
| 历史回看 | 同一轮落库后经 `GET /api/session/{id}` 回看，`findings`、`baselines`、`source_tables`、`persist_row_count` 均在 |

## 回归基线

| 项 | 第二批结束时 | 第三批结束时 |
|---|---|---|
| pytest | 317 passed | 320 passed |
| recall | 37/37 | 37/37 |
| pipeline | 6/9（skipped=1） | 6/9（skipped=1） |
| safety | 8/8 | 8/8 |
| datalineage | 3/3 | 3/3 |
| baseline | 6/6 | 6/6 |
| drilldown | 7/7 | 7/7 |

新增 3 条用例落在 `tests/test_api.py`。既有用例全部通过，未引入回归。

## 收尾核查后的补丁（2026-09-17 11:00）

逐项核查（进度日志第十七节）发现四处缺口，已于同日修复：

| 缺口 | 修复 | 文件 |
|---|---|---|
| FR-M-2 图表侧未标注「本期未完整」 | `ChartPanel` 加 `partialPeriod` prop，为真时在图表上方渲染 Alert；`AnswerCard` 与 `ResultPane` 传 `result.partial_period` | `ChartPanel.tsx` / `AnswerCard.tsx` / `ResultPane.tsx` |
| 历史回看不返回 `partial_period` 与 `granularity` | `session.py` 的轮次映射从 `baselines_json` 提取这两个字段到顶层；`_baselines_field` 辅助函数 | `backend/app/api/session.py` |
| 前端 `Baselines` 类型未声明 `rule` | `Baselines` 接口补 `rule?: string` | `frontend/src/api/types.ts` |
| 接口测试未验证回看字段 | `test_history_turn_keeps_baselines_and_findings` 补 `partial_period` 与 `granularity` 断言 | `backend/tests/test_api.py` |

修复后 `tsc --noEmit` 通过、pytest 320 passed。
