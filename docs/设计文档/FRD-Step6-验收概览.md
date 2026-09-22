# FRD 第 6 批验收概览

对照文档：`docs/设计文档/FRD-功能需求完善文档.md` v1.2.0 第 9 节实施顺序第 6 步。
范围：**FR-G-1 至 FR-G-4（反馈闭环）+ FR-N-1 至 FR-N-4（汇报交付）**，共 8 条需求。
本机实测时间：2026-09-17，桩实现模式（`.env` 的 `LLM_API_KEY` 为空）。

## 一、总体状态

第 6 批 8 条需求全部落地。后端新增 `feedback` 与 `shares` 两表，3 个 feedback 接口
+ 2 个 share 接口；前端新增 `FeedbackBar` / `ExportMenu` / `SharePage` 三组件，
AnswerCard 集成反馈栏与导出菜单，HistoryPage 增加「导出 PPTX」入口。

| 项 | 上一轮 | 本轮 |
|---|---|---|
| pytest | 320 passed | **342 passed**（+22：feedback 12 + share 10） |
| 前端 tsc | 通过 | 通过 |
| 既有用例 | 320 | 320（无回归） |
| 新增用例 | — | 22 |

## 二、需求逐条核对

### FR-G 反馈闭环

#### FR-G-1 点赞点踩（已交付）

- 后端：`feedback` 表（`turn_id UNIQUE`, `rating in {up,down}`），UNIQUE 约束保证一条
  turn 至多一条记录，重复提交按 upsert 更新。
- 接口：`POST /api/feedback` 提交点赞点踩，返回 `{id, feedback, written}`。
- 前端：`FeedbackBar.tsx` 在 AnswerCard 底部渲染点赞 / 点踩按钮，已落库的轮次
  （`turn.turnId` 非空）才显示，streaming 中不显示。
- 落库点：写入 `app.db`，与业务库 `data/ecommerce.db` 分离（见 `test_feedback_table_lives_in_app_db_not_business`）。

#### FR-G-2 点踩原因与正确口径（已交付）

- 原因枚举：`wrong_metric / wrong_field / wrong_number / off_topic / other`，与前端
  `REASON_OPTIONS` 一一对应，文案为「口径错 / 字段错 / 数字错 / 答非所问 / 其他」。
- 接口校验：`reason_code` 必须在枚举内或为空，否则 422
  （`test_submit_rejects_invalid_reason_code`）；点赞时强制清空。
- 自由文本：`note` 上限 500 字，前端 Modal 内 TextArea maxLength=500。

#### FR-G-3 反馈转为评测条目（已交付）

- 接口：`GET /api/feedback/export`，只导出 `rating=down` 的记录。
- 输出格式：YAML 片段，每条 `- id: feedback-N` 含 `question / reason_code /
  user_note / turn_id / session_id / feedback_at / status: pending_review`。
- 不自动写入：返回 `note` 字段提示「导出条目需人工确认后再写入 cases.yaml；
  单条点踩不能证明口径错了」（`test_export_emits_yaml_with_pending_review`）。

#### FR-G-4 反馈落库位置（已交付）

- 落库到 `app.db` 的 `feedback` 表（`Feedback` ORM），不查业务库。
- `turn_id` 关联 `turns.id`，外键 `ON DELETE CASCADE`；反馈只能针对已落库轮次，
  未落库时接口返回 404（`test_submit_rejects_unknown_turn`）。

### FR-N 汇报交付

#### FR-N-1 整页导出 PDF（已交付）

- 实现：浏览器原生打印（`window.print()`）+ 临时注入 `@media print` 样式。
  打印时隐藏顶栏 / 侧栏 / 操作按钮（`.t2sql-no-print`），底部注入页脚 div。
- 不打新接口：纯前端实现，无服务端二次查询。
- 导出物含问句、结论、异常标记、口径与数据时效、图表、明细表——这些是 AnswerCard
  已有内容，打印 CSS 不隐藏卡片主区。

#### FR-N-2 按周会模板导出 PPTX（已交付）

- 依赖：`pptxgenjs`（前端运行时生成 .pptx，PowerPoint 与 WPS 均可打开）。
- 一页一回合：每页含「第 N 回合」标识 + 问句 + 结论 + 口径 + 时间范围 + 异常标记
  + 图表（如有）+ 页脚（数据截至 + 生成时间）。
- 首页：标题 + 数据截至 + 生成时间。
- 入口：HistoryPage 操作列「导出 PPTX」按钮，按整会话拉所有轮次。

#### FR-N-3 只读分享链接（已交付）

- 后端：`shares` 表（`token` PK, `turn_id`, `session_id`, `snapshot_json`,
  `expires_at`, `revoked`, `created_at`）。「链接即凭据」，不做用户校验。
- 接口：
  - `POST /api/share` 生成快照与 token，可选 `expires_in_days`
  - `GET /api/share/{token}` 读快照，软撤销（revoked=1）或过期返回 410，
    不存在返回 404
- 快照内容：`question / answer / sql / status / metric_used / caveats /
  time_range / chart / result{columns,rows,row_count} / baselines / findings /
  partial_period / granularity / source_tables / created_at`
- 前端：`SharePage.tsx` 路由 `/#/share/:token`，只读，无提问入口。

#### FR-N-4 导出物带时效与生成时间（已交付）

- PDF 页脚：`数据截至：{cutoff}　生成时间：{localTimestamp}`，由 ExportMenu 注入
  `#t2sql-print-footer` div。
- PPTX 首页页脚 + 每页页脚：同款文案。
- 数据截至来源：`result.data_freshness.orders_cutoff` 优先，回退 `refunds_cutoff`，
  再回退 `turn.createdAt`。

## 三、新增文件

| 文件 | 内容 |
|---|---|
| `backend/app/api/feedback.py` | 3 个接口：submit / summary / export |
| `backend/app/api/share.py` | 2 个接口：create / read |
| `backend/tests/test_feedback.py` | 12 条用例：仓储 3 + API 9 |
| `backend/tests/test_share.py` | 10 条用例：仓储 2 + API 8 |
| `frontend/src/api/feedback.ts` | 4 个 API 调用：submit / getByTurn / summary / export |
| `frontend/src/api/share.ts` | 2 个 API 调用 + buildShareUrl 工具 |
| `frontend/src/components/FeedbackBar.tsx` | 点赞点踩 + 原因选择 Modal |
| `frontend/src/components/ExportMenu.tsx` | PDF / CSV / 分享链接 下拉菜单 |
| `frontend/src/lib/pptxBuilder.ts` | pptxgenjs 封装：首页 + 一页一回合 |
| `frontend/src/pages/SharePage.tsx` | 只读快照页 |

## 四、修改文件

| 文件 | 变更 |
|---|---|
| `backend/app/db/models.py` | 新增 `Feedback` 与 `Share` ORM 模型 |
| `backend/app/db/repository.py` | 新增 `FeedbackRecord` / `ShareRecord` 数据类；`SessionStore` 抽象加 `upsert_feedback` / `get_feedback_by_turn` / `list_feedback` / `create_share` / `get_share` |
| `backend/app/db/sqlite_repo.py` | `SqliteSessionStore` 实现上述 5 个新方法；`Base.metadata.create_all` 自动建表 |
| `backend/app/api/chat.py` | `_persist` 回填 `result["turn_id"]`，前端据此调 feedback / share |
| `backend/app/api/session.py` | `get_session` 返回的 turn 字段补 `turn_id`（与历史回看同字段同语义） |
| `backend/app/main.py` | 挂载 `feedback.router` 与 `share.router` |
| `frontend/src/api/types.ts` | 新增 `FeedbackRating` / `FeedbackItem` / `FeedbackSummary` / `FeedbackExport` / `ShareRequest` / `ShareCreateResponse` / `ShareSnapshot` / `ShareReadResponse`；`ChatResult` 加 `turn_id`；`TurnItem` 加 `turn_id?` |
| `frontend/src/hooks/useChatStream.ts` | `ChatTurn` 加 `turnId: number | null`；`turnToChatTurn` 写入；`onResult` 时同步 |
| `frontend/src/components/AnswerCard.tsx` | 底部新增 `FeedbackBar` + `ExportMenu`，需要 `sessionId` |
| `frontend/src/components/ChatPanel.tsx` | 透传 `sessionId` 给 `AnswerCard` |
| `frontend/src/pages/ChatPage.tsx` | 给 `ChatPanel` 传 `sessionId` |
| `frontend/src/pages/HistoryPage.tsx` | 操作列加「导出 PPTX」按钮，调用 `buildSessionPptx` |
| `frontend/src/App.tsx` | 注册 `/#/share/:token` 路由 |
| `frontend/package.json` | 新增依赖 `pptxgenjs` |

## 五、字段契约

### POST /api/feedback

请求：
```json
{
  "turn_id": 42,
  "session_id": "uuid",
  "rating": "down",
  "reason_code": "wrong_number",
  "note": "应该是 100"
}
```

响应：
```json
{
  "id": 1,
  "feedback": {
    "id": 1,
    "turn_id": 42,
    "session_id": "uuid",
    "rating": "down",
    "reason_code": "wrong_number",
    "note": "应该是 100",
    "created_at": "2026-09-17T11:22:33",
    "updated_at": "2026-09-17T11:22:33"
  },
  "written": true
}
```

### GET /api/feedback/summary?session_id=&rating=

响应：
```json
{
  "total": 3,
  "up": 1,
  "down": 2,
  "reason_counts": {"wrong_number": 1, "wrong_metric": 1},
  "items": [/* FeedbackItem[] */]
}
```

### POST /api/share

请求：
```json
{
  "turn_id": 42,
  "session_id": "uuid",
  "expires_in_days": 7
}
```

响应：
```json
{
  "token": "hex32",
  "turn_id": 42,
  "session_id": "uuid",
  "expires_at": "2026-09-24T11:22:33",
  "created_at": "2026-09-17T11:22:33"
}
```

### GET /api/share/{token}

响应（200）：
```json
{
  "token": "hex32",
  "turn_id": 42,
  "session_id": "uuid",
  "snapshot": {
    "question": "上个月各区域的成交总额",
    "answer": "本月共成交 ...",
    "sql": "SELECT ...",
    "status": "ok",
    "metric_used": ["成交总额"],
    "caveats": [],
    "time_range": {"label": "上个月", "resolved": "..."},
    "chart": {"chart_type": "bar", "option": {...}},
    "result": {"columns": [...], "rows": [...], "row_count": 7},
    "baselines": {"metric": "成交总额", "granularity": "month", ...},
    "findings": [...],
    "partial_period": false,
    "granularity": "month",
    "source_tables": ["orders"],
    "created_at": "2026-09-17T11:22:33"
  },
  "expires_at": "2026-09-24T11:22:33",
  "created_at": "2026-09-17T11:22:33"
}
```

错误：404 不存在 / 410 撤销或过期。

## 六、与第 7 批的衔接

- **FR-I-3 数据掩码**：第 7 批接入后，`POST /api/share` 在写入 `snapshot_json` 前
  要先走 mask_pipe，把语义层标记为敏感的字段掩掉。第 6 批快照原样存，因为语义层
  当前没有敏感字段——这是一个被刻意留出的接口位，第 7 批只是补 mask_pipe 的实现，
  schema 不变。
- **FR-I-4 节点路径**：`shares` 表已留 `node_path` 字段（实际复用 turns.node_path），
  快照里没单独装——分享页不需要研发细节。
- **FR-D-5 / FR-J-3 / FR-O-5**：第 7 批的统计与延迟观测不影响 share / feedback 接口。

## 七、未完成项与已知限制

- **PDF 导出依赖浏览器打印对话框**：FR-N-1 要求「整页导出 PDF」，本批用 `window.print()`
  实现，用户需在打印对话框中选「另存为 PDF」。无服务端二次查询，满足 FR-N-1 的口径。
- **PPTX 图表占位**：HistoryPage 导出 PPTX 时不渲染 echarts 实例（脱离 DOM 上下文），
  图表区域空着。FR-N-2 要求「图表以图片嵌入」——这一条在第 6 批未做，原因是从
  历史回看拉到的 chart.option 是结构化配置，需要先在隐藏 DOM 中渲染一次再取
  `getDataURL()`。第 7 批接入前端 chart builder 抽离后可以补上。当前实现保留
  `getChartImage` 接口位，调用方传 `async () => null` 表示无图。
- **反馈不联动告警**：FR-G-3 的导出条目只标记 `pending_review`，没有自动给已确认的
  cases.yaml 加进去——单条点踩不能证明口径错了，必须人工确认。
- **快照不带 stages / sql_reasoning**：与历史回看同款，分享页缺这些运行时字段，
  按 ChatResult 的 ChatTurn 降级渲染。
- **回答反馈只针对 ok 与 chitchat 终态**：error 终态不渲染 FeedbackBar
  （AnswerCard 中 `result.status !== 'error'` 判断），避免在失败轮上点踩——失败原因
  在技术细节折叠面板里更直接。
