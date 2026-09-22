# FRD 第 8 批验收概览

对照文档：`docs/设计文档/FRD-功能需求完善文档.md` v1.2.0 第 9 节实施顺序第 8 步。
范围：**意图路由结构化（FR-A-1～A-5）、SQL 解释（FR-C-2）、高级 SQL 与配置
（FR-J-1/J-2/J-4/J-5）**，共 10 条需求。

本机实测时间：2026-09-17，桩实现模式（`.env` 的 `LLM_API_KEY` 为空）。

## 一、总体状态

第 8 批 10 条需求全部落地。后端新增结构化槽位贯穿路由→状态→落库→接口→
前端，SQL 纯结构化解释接口，SQL 方言配置项，多候选择优函数，eval 结果缓存
与多轮 / 高级评测组；前端在技术细节面板新增槽位展示与「解释」按钮入口。

| 项 | 上一轮（第 7 批） | 本轮 |
|---|---|---|
| pytest | 401 passed | **410 passed**（+9：rerank 6 + eval_cache 3） |
| 前端 tsc | 通过 | **通过（0 error）** |
| 评测组数 | 八组 | **十组**（新增 `multiturn` 5+1 条、`advanced` 3 条） |
| 评测结果 | recall 37/37、pipeline 6/9、safety 8/8、datalineage 5/5、baseline 6/6、drilldown 7/7、robust 2/2、alert 4/4 | 前八组持平，multiturn **5/5**、advanced **—（3 条待真实模型）** |
| eval 遥测 | p50/p95、token、成本 | 同上，新增多轮改写与高级用例缓存复用 |

## 二、需求逐条核对

### FR-A 意图路由与结构化槽位（FR-A-1 ～ FR-A-5）

#### FR-A-1 多轮改写机制（已交付）

- `RouteDecision` schema 新增 `rewritten_question: str | None`，路由节点命中
  追问时写回 `state["question"]`，下游 generate / retrieve / summarize 全用
  改写后的问句。
- 桩模式 `_mock_route_decision` 按关键词（「那上个月」「换成」「改成」「按」
  「只看」等）触发改写，标注 `[桩改写]` 前缀让前端可观测。切到真实模型后由
  模型自行补全，桩逻辑不再生效。
- eval `multiturn` 组 `mt-a001-rewrite` 标 `requires_llm=true`：桩模式下单轮
  无上文可参照，跳过不计分。

#### FR-A-2 阶段时间线显示改写结果（已交付）

- `route_node` 的 `_describe(decision)` 在改写发生时输出
  `判定为取数问题（改写后：…）`，截断到 60 字防撑开。
- 无改写时维持原文案 `判定为取数问题`。

#### FR-A-3 排序与 TopN 槽位（已交付）

- 桩模式关键词：`前5 / 前五 / top5 / 前10 / top3` 等命中时填 `slots.top_n`
  与 `slots.order_by`。
- SQL 模板：TopN 产 `ORDER BY … DESC LIMIT 5`；排名产 `RANK() OVER (…)`。
- eval `mt-a003-topn` / `mt-a003-rank` PASS。

#### FR-A-4 数值过滤槽位（已交付）

- 桩模式关键词：`大于 / 超过 / 高于 / >` → `slots.filters` 追加
  `{field, op:">", value}`；`小于 / 低于 / <` 同理。
- 阈值不被当作维度取值，与显式维度值过滤分流。
- SQL 模板产 `HAVING … > 1000000` / `HAVING … < 100000`。
- eval `mt-a004-gt` / `mt-a004-lt` PASS。

#### FR-A-5 结构化槽位六元组（已交付）

- `RouteDecision` schema 新增 `slots: dict[str, Any]`，含
  `metrics / dimensions / filters / time_range / order_by / top_n` 六类。
- `AgentState` TypedDict 注册 `slots` 键（LangGraph 未注册键静默丢弃）。
- 落库：`Turn.slots_json` TEXT 列，`_TURN_MIGRATIONS` 增量迁移。
- 接口：`/api/sessions/{id}` 的 turn 序列含 `slots` 字段；SSE result 帧带回。
- 桩模式：`_mock_route_decision` 按关键词填充六类槽位；命中大区名时隐含
  `区域` 维度（「华东区的成交总额」虽不含「区域」二字，但既然按大区切片，
  维度列表就要有区域）。
- 前端：`TechDetails` 新增「问题槽位」折叠项，用 `Descriptions + Tag` 展示
  六类槽位。
- eval `mt-a005-slots` PASS（dimensions / filters / time_range 全部命中）。

### FR-C SQL 解释与写法说明

#### FR-C-2 POST /api/sql/explain（已交付）

- 新增 `app/api/sql_explain.py`：`POST /api/sql/explain`，body
  `{sql, question, session_id}`。
- `_explain_select`：用 sqlglot 解析 SELECT 语句，按子句（SELECT / FROM /
  WHERE / GROUP BY / ORDER BY / LIMIT）分段，每段标注序号与文本摘要。
- dialect 从 `settings.sql_dialect` 取，不硬编码 `sqlite`。
- 返回 `{segments: [{index, text}], tables: [...], error?}`。
- 前端 `SqlPanel` 新增「解释」按钮，调 `apiSend('POST', '/api/sql/explain')`，
  结果在折叠面板内用 `List` 渲染。`sessionId` 与 `question` 从 `AnswerCard`
  透传。

### FR-J 高级 SQL、方言与择优

#### FR-J-1 窗口函数与 CTE 提示（已交付）

- `generate.md` 新增「高级写法（FR-J-1）」段：RANK / ROW_NUMBER、LAG、
  WITH CTE 三类场景的写法指引与示例。
- dialect 占位符 `{dialect}` 取代硬编码 `SQLite`。
- eval `advanced` 组 3 条（rownumber / lag / cte）标 `requires_llm=true`，
  桩模板不覆盖窗口函数，跳过不计分。

#### FR-J-2 SQL_DIALECT 配置（已交付）

- `Settings.sql_dialect: str = "sqlite"`，`.env` 可配。
- `SqlGuard.from_catalog(dialect=...)` 接受 dialect 参数。
- `deps.py` 传 `dialect=s.sql_dialect` 给守卫。
- `generate.md` 用 `{dialect}` 占位，`render_prompt` 自动替换。

#### FR-J-4 候选重排择优（已交付）

- 新增 `app/agent/candidate_rerank.py`：
  - `CandidateScore` dataclass：guard_ok / field_hit_rate / cost / total。
  - 权重：W_GUARD=100、W_FIELD=10、W_COST=1。总分 = 守卫分 + 字段命中分 -
    代价分。
  - `_estimate_cost`：JOIN 数 + 子查询数（文本级计数）。
  - `_field_hit_rate`：SQL 中命中的语义层字段比例。
  - `score_candidate` / `rerank_candidates`：打分并按总分降序取首条。
- `generate_node` 新增 `ENABLE_RERANK=True` 开关，guard 与 catalog 可用时
  生成第二条候选并择优；桩模式下两候选相同，择优函数本身可单测。
- 单测 `test_rerank.py` 6 条：守卫通过优先、低代价优先、相同候选、单候选、
  空列表抛错、打分明细。

#### FR-J-5 eval 结果缓存（已交付）

- `run_eval.py` 新增 `_INVOKE_CACHE`（进程级，不跨运行）：
  - key = `(session_id, question)`，命中时直接返回上次结果。
  - `inject_sql` 的用例不缓存（安全组需独立执行）。
  - `reset_telemetry` 清空缓存。
- 单测 `test_eval_cache.py` 3 条：重复问题复用、注入 SQL 不缓存、不同会话
  独立缓存。

## 三、桩模式 bug 修复

本批实施中发现并修复了一个桩模式 bug：

- **根因**：`_mock_subject` 用正则 `##\s*[^\n#]*问题` 从 prompt 中提取问题
  段落。`generate.md` 有 `## 问题` 小节，但 `route.md` 用 `本轮问题：` 标记，
  正则不匹配 → 返回整段 prompt 作为匹配对象。
- **影响**：route prompt 的示例文本含「按」字（`按品类拆一下`），命中
  `_MOCK_REWRITE_KEYWORDS` → 整段 prompt 被当作改写结果写回 `state.question`
  → 下游 generate 节点在含「退款率」的 route prompt 文本上匹配到退款 SQL
  模板 → 告警求值 `evaluate_rule` 拿到错误 SQL 与不存在的 time_range →
  baselines 误算 → `test_alerts.py` 2 条失败（status=ok 期望 error）。
- **修复**：`_mock_subject` 增加对 `本轮问题：` 标记的识别，取最后一个匹配
  之后的文本。修复后 pytest 410 全绿，eval 各组恢复正常。

## 四、验证证据

- `cd backend; .venv\Scripts\python.exe -m pytest -q` → **410 passed**
  （基线 401，+9：rerank 6 + eval_cache 3，无回归）。
- `.venv\Scripts\python.exe eval\run_eval.py` → 十组：
  - recall 37/37、pipeline 6/9（既有预期失败，见第六节）、safety 8/8、
    datalineage 5/5、baseline 6/6、drilldown 7/7、robust 2/2、alert 4/4、
    multiturn 5/5、advanced —（3 条待真实模型）。
- `cd frontend; npx tsc --noEmit` → 0 error。
- 业务库快照：`data/seed.py --end-date 2026-09-15`（与第 7 批同一快照）。

## 五、新增 / 修改文件清单

后端：
- 新增 `app/api/sql_explain.py`（FR-C-2）
- 新增 `app/agent/candidate_rerank.py`（FR-J-4）
- 新增 `tests/test_rerank.py`（FR-J-4 单测 6 条）
- 新增 `tests/test_eval_cache.py`（FR-J-5 单测 3 条）
- 修改 `app/llm/gateway.py`（`_mock_route_decision`、`_mock_subject` 修复、
  SQL 模板扩展 TopN / 排名 / 数值过滤）
- 修改 `app/llm/schemas.py`（RouteDecision.slots / rewritten_question）
- 修改 `app/agent/state.py`（AgentState.slots）
- 修改 `app/agent/graph.py`（summarize_result 回传 slots，generate 传 guard / catalog）
- 修改 `app/agent/nodes/route.py`（slots 写回、_describe 改写结果）
- 修改 `app/agent/nodes/generate.py`（dialect / guard / catalog / ENABLE_RERANK）
- 修改 `app/agent/prompts/route.md`（slots 输出格式）
- 修改 `app/agent/prompts/generate.md`（dialect 占位、高级写法段）
- 修改 `app/db/models.py` / `repository.py` / `sqlite_repo.py`（slots_json 列）
- 修改 `app/api/session.py` / `chat.py`（slots 序列化与落库）
- 修改 `app/config.py`（sql_dialect）
- 修改 `app/db/guard.py` / `app/api/deps.py`（dialect 参数）
- 修改 `app/main.py`（sql_explain 路由注册）
- 修改 `eval/run_eval.py`（缓存、multiturn、advanced）
- 修改 `eval/cases.yaml`（multiturn 6 条、advanced 3 条）

前端：
- 修改 `src/api/types.ts`（Slots / SqlExplainSegment / SqlExplainResponse）
- 修改 `src/hooks/useChatStream.ts`（slots 透传）
- 修改 `src/components/TechDetails.tsx`（槽位展示、SQL 解释入口）
- 修改 `src/components/SqlPanel.tsx`（「解释」按钮）
- 修改 `src/components/AnswerCard.tsx`（sessionId / question / slots 透传）

## 六、未完成项与已知限制

- **pipeline 3 条失败是既有预期，不是本批回归**：flow-p006～p008 要求日粒度
  与 users JOIN，桩模板不产出；第 7 批即 6/9，配置真实模型后才有意义。
- **advanced 3 条待真实模型**：rownumber / lag / cte 需窗口函数与 CTE，桩模板
  不覆盖，标 `requires_llm=true` 跳过不计分。
- **mt-a001-rewrite 待真实模型**：单轮桩模式无上文可参照，改写需多轮上下文，
  标 `requires_llm=true` 跳过。
- **桩模式 slots 为关键词填充**：切到真实模型后由 RouteDecision.schema 约束
  模型输出，桩的关键词逻辑不再生效。
- **候选重排桩模式两候选相同**：`ENABLE_RERANK=True` 但桩模式下
  `complete_json` 对同一 prompt 返回同一结果，择优函数本身可单测但实际效果
  需真实模型生成不同候选。
