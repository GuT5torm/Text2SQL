# 实训 5 — AI 共创：后端开发

## 一、实训目标

1. 实现 LLM 网关，集中处理超时重试、模型降级、结构化输出解析与用量统计
2. 用 LangGraph 编排八个业务节点，实现意图识别到结论生成的完整链路
3. 实现失败修正回环与重试配额，理解为什么修正后要回到校验而不是直接执行
4. 通过 SSE 推送节点进度，并理解 Nginx 缓冲对流式的影响
5. 在未配置密钥的情况下用桩实现跑通整条链路

## 二、前置条件

| 项 | 要求 |
|---|---|
| 前置产出 | 实训 4 的样例库与三份语义层配置 |
| 环境 | Python 3.12+，已安装 `fastapi`、`uvicorn`、`langgraph`、`sqlglot`、`openai` |
| API 密钥 | 可选。未配置时网关自动启用桩实现，链路仍可跑通 |

## 三、知识点

| 知识点 | 说明 |
|---|---|
| 网关集中化 | 节点不持有客户端，超时重试降级解析统计全部收敛到一处 |
| 结构化输出 | JSON mode 优先，解析失败去掉 JSON mode 重试，再失败强化格式提示 |
| 状态归约 | `messages` 用追加归约实现多轮，`stages` 用追加归约支持流式推送 |
| 条件路由 | 校验失败与执行失败都进入 `fix`，共享重试配额 |
| 重试回环 | 修正后回到 `validate` 重新过守卫，不允许绕过校验直接执行 |
| SSE | 事件类型 `stage` / `result` / `error` / `done`，响应头关闭代理缓冲 |

## 四、操作步骤

### 步骤 1：配置与依赖注入

`config.py` 用 `pydantic-settings` 读取 `backend/.env`，密钥只出现在这里。`api/deps.py` 里用一个容器持有仓储、网关、守卫与图实例，启动时预热一次。

**预期输出**：`GET /api/health` 返回 `status: ok`，并带出模型名与是否处于桩实现模式。

### 步骤 2：实现 LLM 网关

`llm/gateway.py` 提供 `complete` 与 `complete_json` 两个方法。重试只对超时、限流、连接类错误生效，参数错误重试多少次都一样。

`complete_json` 的三次尝试顺序为：JSON mode、去掉 JSON mode、强化格式提示。国内模型对结构化输出的支持程度不一，这个顺序比直接用 function calling 稳。

**预期输出**：未配置密钥时 `mock` 为 `true`，调用返回桩内容。

### 步骤 3：实现数据访问层

Repository 抽象两个接口：只读业务库的 `QueryRepository` 与会话元数据的 `SessionStore`。SQLite 实现中用进度回调实现超时中断，用 `fetchmany(max_rows + 1)` 判断结果是否被截断。

只读连接用 `file:...?mode=ro` 打开，并执行 `PRAGMA query_only = ON`。

两个接口必须落到不同的库文件上。`SessionStore` 负责会话与轮次读写，需要写权限；业务库连接是只读的，若把会话写进业务库，`query_only` 与 `mode=ro` 会直接让写入失败。会话库单独建一个文件，用普通读写连接打开。

**预期输出**：业务库连接上的写操作被拒绝；会话能正常读写；超大结果集被截断并标记。

### 步骤 4：实现 SQL 安全守卫

`db/guard.py` 实现守卫的七条规则，编号 R1 到 R7，执行顺序固定：先做 R1 到 R5 与 R7 的检查，通过后再做 R6 的 LIMIT 包裹改写。详见《附录B-SQL安全沙箱规则与校验实现》。

语句超时中断（R8）不在守卫内实现，它由仓储层的 progress handler 完成，属于步骤 3 的产出。两者分开的原因是：守卫只做「能不能执行」的判断，超时是执行过程中的资源控制，放在仓储层才能覆盖到语句已经开始执行之后的情况。

**预期输出**：构造违规语句，逐条收到对应的规则编号与原因；构造慢查询，在配置的超时时间内被中断。

### 步骤 5：实现八个节点

按目录一文件一职责：`route`、`retrieve`、`generate`、`validate`、`execute`、`fix`、`visualize`、`summarize`。

三条硬约束在此落实：节点中不出现数据库连接，只调用仓储暴露的方法；语义层不调用模型也不执行 SQL；密钥只出现在 `.env`。

**预期输出**：八个节点各自可单独测试。

### 步骤 6：Prompt 外置

`agent/prompts/` 下五个 Markdown 文件。占位符用 `{name}` 形式，替换时用正则而非 `str.format`，因为 Prompt 正文里有 JSON 示例，花括号会被误处理。

**预期输出**：改 Prompt 不需要改代码。

### 步骤 7：装配图与条件路由

`agent/graph.py` 注册八个业务节点与三个终态节点。终态节点不调用模型也不访问数据库，直接写在图定义里。

条件路由四处：`_after_route` 按意图分流，`_after_generate` 判断是否有语句，`_after_validate` 与 `_after_execute` 判断是否需要修正并共享重试配额。

**预期输出**：构造失败场景可触发修正，`retry_count` 达上限后进入 `give_up`。

### 步骤 8：接入 Checkpointer

`agent/checkpointer.py` 按配置切换 `MemorySaver` 与 `SqliteSaver`，以 `session_id` 作为线程标识。

**预期输出**：多轮追问能继承上文的主体与指标。

### 步骤 9：实现接入层

`api/chat.py` 提供流式与同步两个接口。流式接口逐节点推送进度，响应头设置 `X-Accel-Buffering: no`。

**预期输出**：浏览器或 curl 能收到逐条 `stage` 事件。

### 步骤 10：无密钥冒烟

对话接口要求会话已存在，先创建会话拿到 `session_id`：

```bash
curl -X POST http://127.0.0.1:8000/api/session \
  -H "Content-Type: application/json" \
  -d '{"title":"冒烟测试"}'
```

返回中的 `id` 即会话 ID。直接调用对话接口而会话不存在时，接口返回 404，不是自动新建。

用返回值替换下面的占位，未配置密钥时用同步接口跑一遍：

```bash
curl -X POST http://127.0.0.1:8000/api/chat/sync \
  -H "Content-Type: application/json" \
  -d '{"session_id":"<上一步返回的 id>","question":"上个月各区域的成交总额"}'
```

**预期输出**：返回 `status: ok`，含语句、列、行数与图表配置。

## 五、AI 共创要点

**怎么问**：让 AI 写节点时，把状态字段与返回契约一起给出。只说「写一个生成 SQL 的节点」，AI 会自行设计输入输出，接进图以后才发现对不上。

一个可用的提问结构：

```
状态字段：<列出 AgentState 中与本节点相关的字段>
任务：实现 <节点名> 节点
约束：
1. 不出现数据库连接，只调用仓储暴露的方法
2. 失败时返回可读的错误说明，不抛异常中断图
3. 返回字典中必须包含 stages 键，追加一条进度事件
```

**常见偏差**：

| 偏差 | 表现 | 处理 |
|---|---|---|
| 节点直接连库 | 在节点里 `import sqlite3` | 改为调用仓储方法 |
| 修正后直接执行 | `fix` 边连到 `execute` | 改回 `validate`，不允许绕过校验 |
| 重试无上限 | 循环直到成功 | 加 `retry_count` 配额，超限进 `give_up` |
| 密钥写进代码 | 网关里出现 API Key 字面量 | 移到 `.env`，通过配置读取 |
| 异常打断整张图 | 节点抛异常导致流中断 | 节点内兜住异常，返回结构化错误 |
| 阶段事件跨轮累积 | 第二轮返回了第一轮的进度 | 按逐节点更新采集本轮事件，不从终态整体取 |

**审查动作**：遍历 `agent/nodes/` 目录，搜索是否出现数据库连接相关的导入。出现即违反分层约束，必须改。

## 六、产出要求

| 文件 | 位置 |
|---|---|
| `config.py`、`main.py` | `backend/app/` |
| `gateway.py`、`schemas.py` | `backend/app/llm/` |
| `engine.py`、`models.py`、`repository.py`、`sqlite_repo.py`、`guard.py` | `backend/app/db/` |
| `state.py`、`graph.py`、`checkpointer.py`、`nodes/*.py`、`prompts/*.md` | `backend/app/agent/` |
| `deps.py`、`chat.py`、`session.py`、`semantic.py` | `backend/app/api/` |

## 七、检查点

- [ ] `GET /api/health` 返回 `status: ok`
- [ ] 未配置密钥时链路仍可跑通
- [ ] 重试只对超时、限流、连接类错误生效
- [ ] 八个节点各自可单独测试
- [ ] 修正后回到校验而非直接执行
- [ ] 重试达上限进入 `give_up` 并给出可读说明
- [ ] SSE 逐节点推送，不一次性吐出
- [ ] `agent/nodes/` 下无数据库连接相关导入
- [ ] 密钥只出现在 `.env`

## 八、常见问题

**问题：SSE 进度在浏览器里一次性出现。**
原因：经过 Nginx 时响应被缓冲。解决：响应头设置 `X-Accel-Buffering: no`，并在 Nginx 的 `location` 中关闭缓冲。详见附录 C。

**问题：第二轮回答里带上了第一轮的阶段事件。**
原因：进度事件用追加归约，Checkpointer 保留了上一轮的内容。解决：接口按逐节点更新采集本轮事件，不从终态整体取。

**问题：修正节点把字段越改越错。**
原因：报错信息没有原样传给模型，模型只能重新猜。解决：把数据库或守卫的原始报错完整放进 Prompt，并在 Prompt 中要求「只改报错指出的部分」。

**问题：桩实现下所有问题返回同一条 SQL。**
原因：桩的关键词匹配对整段 Prompt 生效，而语义层上下文里列出了全部指标名。解决：只取 Prompt 中问题小节之后的内容作为匹配对象。
