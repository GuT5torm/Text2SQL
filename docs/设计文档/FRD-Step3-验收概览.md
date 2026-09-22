# FRD 第 3 步（前端呈现）验收概览

按 FRD v1.2.0 第九节实施顺序第 3 步执行。本批只动前端，后端零改动。9 条全在 React 工程内：`ResultTable` / `ChartPanel` / `SqlPanel` / `StageIndicator` / `AnswerCard` / `MetricBadge` / `App.tsx` 7 个现有组件 + `DefinitionPanel` / `TechDetails` 2 个新组件 + `api/semantic.ts` / `lib/exporters.ts` 2 个底层工具。

## 文件与改动一览

| 文件 | 性质 | 关联 FR |
|---|---|---|
| `frontend/src/lib/exporters.ts`（新） | CSV / PNG / .sql 下载工具；slug 与本地时间戳 | FR-B-1 / B-2 / C-3 |
| `frontend/src/api/semantic.ts`（新） | `/api/semantic/metrics` 与 `/api/semantic/tables` 封装，模块级 Promise 缓存 | FR-D-3 |
| `frontend/src/components/DefinitionPanel.tsx`（新） | 六项口径面板：表达式 / 单位 / 分母 / 必备维度 / caveats / 枚举依据 | FR-D-3 |
| `frontend/src/components/TechDetails.tsx`（新） | 统一技术细节入口：阶段、SQL、Guard 告警，折叠态 | FR-P-3 |
| `frontend/src/components/ResultTable.tsx` | 加「导出 CSV」按钮，UTF-8 BOM + 转义引号，1000 行兜底 | FR-B-1 |
| `frontend/src/components/ChartPanel.tsx` | 标题加「导出 PNG」按钮，echarts.getDataURL 2x DPR | FR-B-2 |
| `frontend/src/components/SqlPanel.tsx` | 折叠项旁加「下载 .sql」按钮，命名 `T2S_{slug}_{stamp}.sql` | FR-C-3 |
| `frontend/src/components/StageIndicator.tsx` | 节点 ID → 动词语映射（兜底层），收起行摘掉耗时文本 | FR-E-2 / P-1 |
| `frontend/src/components/AnswerCard.tsx` | 接 5 项：复制结论 / 未命中提示 / 口径面板 / 移除耗时 / 技术细节 | FR-B-4 / D-3 / D-4 / P-1 / P-3 |
| `frontend/src/App.tsx` | 顶栏加「只读」常驻徽标 + Popover 四项说明 | FR-I-1 |
| `frontend/src/api/types.ts` | `ChatResult` 补 `sql_reasoning` / `fix_reasoning` / `source_tables` / `data_freshness` / `persist_row_count` / `node_path`；新增 `DataFreshness` 类型 | 链路对齐 |
| `frontend/src/hooks/useChatStream.ts` | `turnToChatTurn` 同步填新字段（旧记录读为空，FR-H-2 不报错） | 链路对齐 |

## 9 条需求落地

### FR-B-1 结果表导出 CSV

`ResultTable.tsx` 顶部加「导出 CSV」按钮。`buildCsv` 列名 + 行集转 CSV：含 `,` `"` `\r\n` 的字段双引号包裹、内部 `"` 转义为 `""`；行间用 `\r\n` 保证 Excel 跨地域不乱；文件头加 UTF-8 BOM 解决中文 Windows Excel 乱码。

文件名：`{question_slug}_{YYYYMMDD_HHMMSS}.csv`。超过 1000 行的历史回看兜底截断，避免浏览器下载卡死。

### FR-B-2 图表导出 PNG

`ChartPanel.tsx` 的 Card `extra` 加「导出 PNG」。`echarts.getDataURL({ type: 'png', pixelRatio: 2 })` 出高分辨率，`dataUrlToBlob` 转 Blob 再 `downloadBlob`，白底 `backgroundColor: '#ffffff'`。

文件名：`{question_slug}_{YYYYMMDD_HHMMSS}.png`。图表 unload 时 `instance.dispose()`，在 dispose 之前取 dataURL 即取即用。

### FR-B-4 结论与要点复制

`AnswerCard.tsx` 结论上方加「复制结论」按钮。复制文本格式：

```
（结论文案）

时间范围：（resolved）
口径：（metric_used）
边界：
- caveats[0]
- caveats[1]
...
来源：Text2SQL 生成于 20260915_153020
```

脱离上下文后仍能读出来源。

### FR-C-3 SQL 下载

`SqlPanel.tsx` 折叠项内 SQL 旁加「下载 .sql」。文件名：`{sql_slug}_{stamp}.sql`。下载时附 `application/sql;charset=utf-8` 的 MIME，「复制」按钮仍保留。

### FR-D-3 口径定义入口

新建 `DefinitionPanel.tsx`。内容走 `/api/semantic/metrics` 与 `/api/semantic/tables`（`api/semantic.ts` 封装 + 模块级 Promise 缓存）。六项：

1. **口径表达式**：`expression` 字段
2. **单位**：`unit`
3. **分母与状态过滤**：`denominator` Tag + 每条 `required_filters` 等宽字体
4. **必备维度**：`dimensions` Tag 列表
5. **caveats（口径边界）**：长文本 4 行后可展开
6. **枚举字段的划分依据**：收集所有用到的 `tables[*].fields[].definition`，按 enum 非空才入列

`AnswerCard` 默认折叠在指标徽标下方。任一指标缺失则面板改 `<Empty>`，不报错。

### FR-D-4 未命中口径的显式提示

`AnswerCard.tsx` 在 `metric_used` 为空 且 `intent === 'query'`（区别于 chitchat/clarify 的「该为空就为空」）时，顶部加 `Alert type="warning"`：

> 本次未命中已登记指标 — 系统未在语义层找到与本问题匹配的指标定义，结论与数字仅来自生成式总结，请谨慎采纳。

### FR-E-2 动作态阶段文案

`StageIndicator.tsx` 加 `VERB_LABELS` 映射表，节点 ID → 动词语：

```
route     → 路由
retrieve  → 检索
generate  → 生成 SQL
validate  → 守卫
execute   → 执行
fix       → 修正
visualize → 出图
summarize → 总结
respond   → 回复
clarify   → 反问
give_up   → 放弃
```

后端目前 `new_stage` 已经传入语义化 label，前端优先用 stage.label，本映射作为兜底。改动时两边同步，注释里写明。

### FR-I-1 只读说明常驻

`App.tsx` 顶栏项目名右侧加 `<ReadOnlyBadge />`（Popover + click trigger）。点击展开四个能力维度的说明：连接只读 / 语句白名单 / 字段白名单 / 结果只含聚合。徽标紧邻项目名不抢戏，被拦截黄条出现也不与之冲突。

### FR-P-1~4 信息分层

- **FR-P-1**：阶段收起行从 `共 N 个阶段 · X ms · 点击展开` 改为 `共 N 个阶段　点击展开`，耗时不再外漏到摘要层。
- **FR-P-2**：`AnswerCard` 摘要行（原 `查询耗时 {ms} ms` 文案）整体移除，耗时仅出现在阶段展开后的每一行。
- **FR-P-3**：新建 `TechDetails.tsx`。三个折叠项（阶段时序 / SQL 与安全沙箱 / Guard 告警），任何一项都不会默认展开。`AnswerCard` 业务信息主区只剩 阶段指示 + 澄清 / 失败 / 空结果提示 + 结论 + 要点 + 口径徽标 + 口径定义面板 + 图表 + 摘要表格。
- **FR-P-4**：复核 `SqlPanel.tsx` 与 `StageIndicator.tsx` 默认 `Collapse` 收起，符合实现态。

## 验证

| 项 | 结果 |
|---|---|
| `tsc --noEmit` | 通过 |
| Vite 转译模块数 | 3610 modules transformed（无报错） |
| 后端 `pytest` 回归 | 206 passed（与上一批一致，前端改动不影响） |

前端没有单测框架（`package.json` 未配 vitest / jest），验证以编译期类型检查为主。

## 已知限制

1. **口径面板的网络延迟**：第一次展开时调 `/api/semantic/metrics` 与 `/api/semantic/tables`，两个请求并发。模块级 Promise 缓存保证同一进程内只取一次。如果后端冷启动，第一次展开的等待时长就接近网络往返。生产可考虑改为查询结果里加一个 `_definitions` 内联摘要字段，但本批保持职责分离原则。
2. **PNG 导出非矢量**：echarts 没有 SVG 导出在 antd / echarts 5.x 生态里有可靠封装，PNG 2x DPR 已经够用；矢量导出涉及 canvas → svg → canvas 链路，引入两套画布依赖，否决。
3. **CSV 与 BOM**：Excel for Mac 2019 之前版本对 UTF-8 BOM 处理不一致，超出本系统支持范围。

## 已登记的待办与下一步

- 第 3 批（前端 9 条）完成
- 实际下一步可推进顺序 4（FR-M-1~3 对比基准计算）或顺序 6（FR-G-1~4 + FR-N-1~4 反馈与汇报）。

确认后开下一批。
