# FRD 第 7 批验收概览

对照文档：`docs/设计文档/FRD-功能需求完善文档.md` v1.2.0 第 9 节实施顺序第 7 步。
范围：**异常订阅（FR-O-1～O-5）、安全与合规（FR-I-2/I-3/I-4）、结论可信度
（FR-B-3）、时间一致性（FR-D-5）、中断三态（FR-E-3）、成本与健康观测（FR-J-3）**，
共 12 条需求。

本机实测时间：2026-09-17，桩实现模式（`.env` 的 `LLM_API_KEY` 为空）。

## 一、总体状态

第 7 批 12 条需求全部落地。后端新增异常订阅两表与 5 个接口、结论数字核对器、
时间区间核对器、敏感字段脱敏管道、进程内用量计数；评测集从六组扩到八组并输出
遥测；前端新增告警订阅中心页，对话卡片接入守卫双色、数字待查、脱敏与时间不一致
标注，中断 / 空响应 / 网络故障三态分流。

| 项 | 上一轮（第 6 批） | 本轮 |
|---|---|---|
| pytest | 342 passed | **401 passed**（+59：alert 14 + 一致性 19 + 脱敏 12 + 守卫 14） |
| 前端 tsc | 通过 | **通过（0 error）** |
| 评测组数 | 六组 | **八组**（新增 `robust` 2 条、`alert` 4 条） |
| 评测结果 | recall 37/37、pipeline 6/9、safety 8/8、datalineage 3/3、baseline 6/6、drilldown 7/7 | recall 37/37、pipeline **6/9**（既有预期失败，见第七节）、safety 8/8、datalineage **5/5**、baseline 6/6、drilldown 7/7、robust **2/2**、alert **4/4** |
| eval 遥测 | 无 | p50/p95/avg/max、token、估算成本、对比期查询数写入 `report.json.telemetry` |
| 浏览器冒烟 | — | 告警中心页加载 / 事件渲染 / 未读角标 / 无控制台错误，已实测 |

## 二、需求逐条核对

### FR-O 异常订阅（FR-O-1 ～ FR-O-5）

#### FR-O-1 规则两表与 CRUD（已交付）

- 数据模型：`alert_rules`（id / metric / dimensions_json / comparison / threshold /
  severity / enabled / 时间戳）+ `alerts`（id / rule_id FK ON DELETE CASCADE /
  evaluated_at / status / hit / metric / value / reason / slice_json(hits) / seen /
  created_at），`(rule_id, evaluated_at)` UNIQUE 保证同周期幂等。
- 接口（prefix `/api/alert`）：`GET /rules?enabled=`、`POST /rules`
  （无 id 新建 / 带 id 更新，更新不存在 404）、`DELETE /rules/{id}`。
- 写入前服务端校验：comparison ∈ {gt, lt, deviation}、severity ∈ {notice,
  attention}、dimensions 不重复，并过 `build_question` 做语义层校验（未知指标 /
  维度 400），订阅不存在的指标在配置阶段就挡住。
- 修复：`create_app_engine` 连接 PRAGMA 补 `foreign_keys = ON`——此前外键级联是
  摆设，删规则后事件成孤儿且自增 id 复用会串数据。

#### FR-O-2 立即求值复用问答链路（已交付）

- `POST /api/alert/check`（body：rule_ids? / evaluated_at?）：默认求值全部启用
  规则，缺省时刻按 `alert_eval_period_minutes` 量化到周期起点。
- 求值合成自然语言问句（物理名 / 显示名都认；整体口径问句不加「各」）后走
  `graph.invoke`，每次独立 session_id（`alert-{rule}-{周期}-{uuid}`），不与
  MemorySaver 历史 checkpoint 串状态。

#### FR-O-3 周期幂等与外部调度（已交付，部署前提见限制）

- 同 `(rule_id, evaluated_at)` 复检直接返回已存在记录，不重复跑图；异周期产生
  新行。alert 组 `alert-o004` 与 `test_alerts` 均固化此行为。
- 进程内**不设调度器**：按 FRD 由外部计划任务周期调用 `/check`，应用只保证幂等。

#### FR-O-4 事件列表、未读与一键下钻（已交付）

- `GET /api/alert/events?seen=&rule_id=&limit=` 返回事件 + `unread` 计数；
  `POST /api/alert/events/{id}/seen` 标记已读并回最新未读数。
- 命中切片 hits 携带 `slice / value / baseline / reason / question`，question 可
  直接回填首页提问框。前端 `AlertCenter.tsx` 事件行内有「去对话里下钻」，跳转
  `#/?q=...`，ChatPage 读 `q` 自动发送一次后清参。

#### FR-O-5 三态与失败计数（已交付）

- 事件三态：`hit=true` 命中；`status=ok,hit=false` 未命中；`status=error` 求值
  失败（图异常 / SQL 错 / intent 非 query / 未召回指标；deviation 分组缺逐切片
  基准也算口径不成立的 error）。
- 健康度：`/api/health` 输出 `alert_consecutive_failures / alert_failures_threshold
  /alert_unhealthy / alert_last_eval_at / alert_unread`；连续失败计数仅当成功记录
  的 evaluated_at 晚于失败记录才归零。

### FR-I 安全与合规

#### FR-I-2 拒绝原因编号 R1～R9 与双通道（已交付）

- 守卫全部拒绝文案带规则编号（R1～R9）；`guard.py` 提供 `notice_code /
  make_notice / rejected_notices_from_text`。
- 图终态新增 `guard_notices: [{kind, code, message}]` 双通道：`rejected`
  （give_up 的 sql_error 按「；」还原）与 `rewritten`（R6 等改写后放行的告警）；
  历史回放走同一 `derive_guard_notices`，本轮与历史渲染一致。
- 前端双色：rejected 黄色「语句被沙箱拒绝」，rewritten 蓝色「语句改写后放行」，
  编号加粗；老数据（纯字符串 guard_warnings）按黄色兜底。

#### FR-I-3 敏感字段脱敏（已交付）

- 语义层：`tables.yaml` 标 `sensitive: true` + `mask` 类型，loader 加载
  `mask_kind / sensitive_fields` 并做配置校验；`schema.sql` users 补 phone /
  id_card 列（phone/id_card 由 uid 与注册日确定性派生，不消耗造数 RNG）。
- 管道：`db/masking.py` 在落库与 SSE / sync 下发**之前**对结果集就地脱敏
  （物理列名、中文别名投影、list 行都覆盖，幂等），响应写 `masked /
  masked_fields`，随轮次落 metrics_json，历史回看与分享快照同契约。
- 前端：结论标题旁紫色「已脱敏」Tag，Tooltip 列出字段。

#### FR-I-4 回合追溯（已交付）

- 节点序列 `node_path` 沿 stages 去重保序（fix 回环取首次），此前批次已落库；
- 本批补齐：每轮守卫告警、数字核对、时间核对、脱敏标记随轮次持久化，历史详情
  接口逐字段回放，前端按缺失降级，老库记录不报错。

### FR-B-3 结论数字可回溯（已交付）

- `agent/consistency.py::check_conclusion_numbers`：从结论抽数字（万亿 / 亿 / 万、
  千分位、货币、百分比双量纲各按 1% 相对或 0.01 绝对容差），只在结果集 + 白名单
  字段（row_count / elapsed / findings / baselines 等）中回溯；99999 待查只报一次、
  日期序数排除、脱敏值不当数字。
- 不可回溯的数字以 `number_checks: [{text, value}]` 返回，前端在结论下方给中性
  提示「以下数字未能从结果集回溯，请人工核对」，**不改正文**。

### FR-D-5 SQL 时间区间一致性（已交付）

- `check_time_range_consistency(sql, retrieved_time_range)`：识别 gte/lt、
  BETWEEN 折叠、strftime 月折叠等形态；缺谓词 / 单边 / 区间超宽 → false，
  子区间 → true，无召回需求 → None（未核对）。
- enrich 节点在基准计算**之前**做纯函数核对，基准失败也不影响该结论；核对器自身
  异常按 None 处理并记 warning。
- 前端仅在 `=== false` 时显示橙色「时间区间不一致」Tag，None 不渲染。

### FR-E-3 中断 / 空响应 / 网络异常三态（已交付）

- 纯判定抽到 `lib/streamState.ts`（无 React 依赖）：
  - `aborted`：AbortError，中性 info 提示，**不给重试**（重发即可）；
  - `empty`：onDone 时既无 result 也无 error，黄色警告 + 重试；
  - `network`：fetch 异常 / 非 2xx / SSE error 帧，红色失败 + 底层文案 + 重试。

### FR-J-3 成本、对比期用量与健康扩展（已交付）

- 配置：`LLM_PRICE_JSON`（model → 每千 token 输入 / 输出单价，解析失败不臆造）；
  gateway `totals()` 增 `estimated_cost`（round 6）。
- `observability.py` 进程内计数：mom / yoy 基准 SQL 每次计数，`/api/stats`
  在 gateway totals 基础上输出 `comparison_queries`。
- `/api/health` 扩展：实际 checkpointer 模式（memory/sqlite）、`data_as_of`
  （orders/refunds cutoff 取早者）、`data_freshness`、告警规则数 / 末次求值 /
  连续失败 / 阈值 / unhealthy / unread。
- eval 报告顶层 `telemetry`：samples、query_latency_ms{p50,p95,avg,max}
  （最近秩法）、llm_calls、prompt/completion_tokens、estimated_cost、
  comparison_queries。本轮实测：26 样本 p50 9.5ms / p95 16.0ms。

## 三、新增文件

| 文件 | 内容 |
|---|---|
| `backend/app/agent/alert_eval.py` | 问句合成、求值（复用图）、三态判定、周期量化 |
| `backend/app/api/alerts.py` | 5 个接口：rules CRUD + check + events + seen |
| `backend/app/agent/consistency.py` | FR-B-3 数字回溯 + FR-D-5 时间区间一致性两个纯函数 |
| `backend/app/db/masking.py` | mask_scalar / mask_result 脱敏管道 |
| `backend/app/observability.py` | 进程内单例计数器（对比期查询） |
| `backend/tests/test_alerts.py` | 14 条：CRUD / 三态 / 幂等 / 未读 / 失败计数 / health |
| `backend/tests/test_consistency.py` | 19 条：数字回溯与时间五形态 |
| `backend/tests/test_masking.py` | 12 条：标量规则 / dict / list / 别名 / 幂等 |
| `frontend/src/api/alerts.ts` | 告警接口封装 |
| `frontend/src/pages/AlertCenter.tsx` | 规则管理 + 事件中心页（Tabs / Modal / 下钻） |
| `frontend/src/lib/streamState.ts` | 三态判定、终态文案、守卫告警归一化（纯函数） |

## 四、主要修改文件

| 文件 | 变更 |
|---|---|
| `backend/app/db/models.py` / `repository.py` / `sqlite_repo.py` | alert 两表 ORM、Record、10 个仓储方法与唯一约束 |
| `backend/app/db/schema.sql` / `data/seed.py` | users 加 phone/id_card；seed 增 `--end-date` 复现评测快照 |
| `backend/app/db/engine.py` | 连接开启 `foreign_keys=ON` |
| `backend/app/db/guard.py` | R1–R9 编号、notice 工具函数 |
| `backend/app/agent/graph.py` | `derive_guard_notices`；summarize_result 输出 guard_notices / number_checks / time_range_consistent |
| `backend/app/agent/state.py` / `nodes/enrich.py` / `nodes/summarize.py` | 核对器接入与新字段透传 |
| `backend/app/agent/baselines.py` | mom/yoy 执行处计数 |
| `backend/app/agent/checkpointer.py` / `api/deps.py` | with_mode 返回实际 checkpointer 模式并透传 |
| `backend/app/llm/gateway.py` / `config.py` | estimated_cost、LLM_PRICE_JSON、告警阈值配置 |
| `backend/app/main.py` | 挂载 alerts router；/api/health 与 /api/stats 扩展 |
| `backend/app/api/chat.py` / `session.py` / `share.py` | 脱敏点统一、新字段落库与回放 |
| `backend/eval/run_eval.py` / `cases.yaml` / `README.md` | 八组、robust/alert 组、遥测；README 同步 |
| `frontend/src/api/types.ts` | GuardNotice / NumberCheck / 全套 alert 类型；ChatResult / TurnItem / HealthInfo / ShareSnapshot 扩字段 |
| `frontend/src/hooks/useChatStream.ts` | ChatTurn 增 errorKind，三态打标；新字段历史回放 |
| `frontend/src/components/AnswerCard.tsx` | 三态终态、数字待查提示、已脱敏 Tag、新标注透传 |
| `frontend/src/components/TechDetails.tsx` / `ResultPane.tsx` | 守卫告警黄蓝双色 |
| `frontend/src/components/MetricBadge.tsx` | 时间区间不一致橙色 Tag |
| `frontend/src/pages/ChatPage.tsx` | 支持 `?q=` 自动发送（告警下钻），新建会话保留 q |
| `frontend/src/pages/SharePage.tsx` | 脱敏 / 数字待查 / 时间一致性标注 |
| `frontend/src/App.tsx` | 「告警订阅」导航 + 未读 Badge（30s 轮询 health）+ /alerts 路由 + 求值异常红 Tag |

## 五、接口契约（新增）

- `POST /api/alert/rules`

```json
{ "metric": "refund_rate", "dimensions": ["region"],
  "comparison": "gt", "threshold": 0, "severity": "notice", "enabled": true }
```

- `POST /api/alert/check`

```json
// 请求（均可空）：{ "rule_ids": [1], "evaluated_at": "2026-09-15T10:00:00" }
// 响应：
{ "evaluated_at": "2026-09-15T10:00:00", "checked": 1,
  "events": [{ "id": 11, "rule_id": 1, "status": "ok", "hit": true,
               "hits": [{ "slice": {"大区": "西北"}, "value": 0.0602,
                          "baseline": null, "reason": "…", "question": "西北退款率" }] }] }
```

- `GET /api/alert/events` → `{ "events": [AlertEvent], "unread": 1 }`
- `POST /api/alert/events/{id}/seen` → `{ "id": 11, "seen": true, "unread": 0 }`
- 错误：规则校验失败 400（detail 为中文原因）；更新 / 删除 / 指定 check 的未知
  规则 404；标记未知事件 404。

## 六、验证证据

- `cd backend; .venv\Scripts\python.exe -m pytest -q` → **401 passed**（基线 342，
  无回归）。
- `.venv\Scripts\python.exe eval\run_eval.py` → 八组，除 pipeline 三条第 0 步
  校准用例外全绿；alert 组 4/4 覆盖命中 / 未命中 / 失败 / 幂等。
- `cd frontend; npx tsc --noEmit` → 0 error。
- 浏览器冒烟（桩模式 + 真实业务库）：预置规则经 API 求值得到 7 切片命中事件，
  页面事件 Tab 正确渲染命中行与「去对话里下钻」按钮，顶栏未读角标为 1，控制台
  无红色错误、/api 无 2xx 以外请求；冒烟数据随测随清（级联删除），health
  回到 `unread=0 / rules=0 / failures=0`。
- 业务库快照：`data/seed.py --end-date 2026-09-15` 重建（与第 6 批 drilldown
  7/7 基线同一快照）。

## 七、未完成项与已知限制

- **pipeline 3 条失败是既有预期，不是本批回归**：flow-p006～p008 要求日粒度与
  users JOIN，桩模板不产出；第 0 步校准时刻意保留，基线即 6/9，配置真实模型后
  才有意义。
- **桩模式 telemetry 中 token / 成本为 0**：无 LLM 调用；p50/p95 反映的是桩链路
  延迟。`LLM_PRICE_JSON` 未配置时单价缺省即成本 0，绝不臆造价格。
- **脱敏端到端待真实模型**：桩模板不覆盖 users 表查询，链路级脱敏由
  `test_masking.py` 对 `mask_result` 直接断言；语义层标记与管道已就位。
- **FR-O-3 依赖外部计划任务**：应用内无调度器，部署方需用 cron / 定时器周期
  调用 `POST /api/alert/check`；手动入口在告警中心「立即求值」。
- **deviation 订阅只支持整体口径**：分组结果缺逐切片对比基准，配置阶段前端禁用
  维度、后端兜底返回 error 事件。
- **数据快照与日期耦合**：seed 默认截止昨天，评测断言锚定 2026-09-15 快照；
  重新造数须带 `--end-date 2026-09-15`（README 已写明），否则分布敏感的 rank
  断言可能翻转。
- **AlertCenter 维度下拉显示物理名**：指标可选维度来自 `/api/semantic/metrics`
  的 dimensions（物理名），后端物理名 / 显示名都接受；维度中文名映射未做。
