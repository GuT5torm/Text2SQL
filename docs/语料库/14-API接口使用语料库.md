# 14 — API 接口使用语料库

## 接口总表

后端单进程提供 15 个接口，全部以 `/api` 开头。

| 分组 | 方法 | 路径 | 用途 |
|---|---|---|---|
| 运维 | GET | `/api/health` | 存活探针，报告模型模式与资源数量 |
| 运维 | GET | `/api/stats` | 本进程累计的模型调用与 token 用量 |
| 会话 | POST | `/api/session` | 新建会话 |
| 会话 | GET | `/api/session` | 会话列表，支持关键词与归档过滤 |
| 会话 | GET | `/api/session/{session_id}` | 会话详情，含全部轮次 |
| 会话 | PATCH | `/api/session/{session_id}` | 重命名与归档 |
| 会话 | DELETE | `/api/session/{session_id}` | 删除会话及其轮次 |
| 语义层 | GET | `/api/semantic` | 指标、表、维度的概览与计数 |
| 语义层 | GET | `/api/semantic/metrics` | 指标口径全文 |
| 语义层 | GET | `/api/semantic/tables` | 表结构与维度全文 |
| 语义层 | GET | `/api/semantic/synonyms` | 同义词、枚举值口语、相对时间词 |
| 语义层 | GET | `/api/semantic/recall` | 调试接口，不调模型直接看召回结果 |
| 语义层 | GET | `/api/semantic/health` | 语义层登记与实际表的一致性 |
| 对话 | POST | `/api/chat` | 流式问答，SSE 推送 |
| 对话 | POST | `/api/chat/sync` | 非流式问答，一次性返回 |

交互式文档在 `/docs`，由 FastAPI 自动生成，可以直接在浏览器里试每个接口。

## 通用约定

请求体为 JSON，`Content-Type: application/json`。响应体同样为 JSON，中文不做 Unicode 转义。

错误响应分两类：

- HTTP 异常：`{"detail": "会话不存在：xxx"}`，业务上的资源不存在用 404，参数校验失败用 422。
- 语义层配置错误：`{"detail": "...", "type": "SemanticError"}`，状态码 500。语义层 YAML 写错时返回这个，detail 里给出完整的问题清单。

`/api/chat` 不走这套错误响应。它一旦建立 SSE 流，状态码已经是 200，后续的错误只能通过事件体传递。

## 运维接口

### GET /api/health

```json
{
  "status": "ok",
  "llm_mock": false,
  "model": "qwen-plus",
  "checkpointer": "sqlite",
  "metrics": 7,
  "tables": 6
}
```

`llm_mock` 为 `true` 表示当前是桩实现，没有配置 API Key。部署后第一件事就是看这个字段，它的值决定了所有问答结果是否可信。

`metrics` 与 `tables` 是语义层加载的数量，为 0 说明 YAML 没读到，检查工作目录与配置路径。

### GET /api/stats

返回本进程累计的模型调用次数、prompt token 与 completion token。演示时用这个数字说明一次问答的消耗。进程重启后清零。

## 会话接口

### POST /api/session

请求体可选：

```json
{ "title": "8月经营分析" }
```

响应：

```json
{
  "id": "3f2a1c9e-...",
  "title": "8月经营分析",
  "created_at": "2026-09-14T10:20:31+08:00",
  "updated_at": "2026-09-14T10:20:31+08:00",
  "archived": false,
  "turn_count": 0
}
```

`id` 是后续一切操作的入口。它同时作为 LangGraph 的 thread_id，会话一旦创建，多轮上下文就在图内部按这个键累积，接入层不维护对话历史。

### GET /api/session

查询参数：`keyword`（按标题或提问内容搜索）、`include_archived`（默认 false）。

```json
{ "total": 2, "items": [ /* 会话对象 */ ] }
```

### GET /api/session/{session_id}

返回会话对象加 `turns` 数组。每个轮次包含 `question`、`answer`、`sql`、`metrics`（口径、提示、时间区间）、`chart`、`result`（列与行）、`row_count`、`elapsed_ms`、`status`、`error`。

历史页与会话恢复都依赖这个接口。轮次落库时不额外做行数截断，落库的内容与当轮 SSE 流下发的一致，因此历史回看能看到与当时相同的完整结果集。

### PATCH /api/session/{session_id}

重命名与归档合并在一个接口，两者都是对元数据的局部修改：

```json
{ "rename": { "title": "8月复盘" }, "archive": { "archived": true } }
```

两个字段都可以单独给。两者都未提供时属于请求体参数不完整，按通用约定返回 422，与「会话不存在」的 404 区分开。

### DELETE /api/session/{session_id}

返回 `{"id": "...", "deleted": true}`。删除会连带删除该会话的全部轮次。LangGraph 的检查点数据不在删除范围内，属于已知行为，不影响功能。

## 语义层接口

### GET /api/semantic

概览接口，返回指标清单、表清单、维度清单与各自数量。前端侧边栏的口径说明与实训环节的整体认知都用它。

### GET /api/semantic/metrics

指标口径全文，每条含名称、显示名、同义词、口径表达式、依赖表、必带条件、可用维度、边界说明、单位、示例问法。

口径表达式是可直接嵌入 SQL 的片段，不是自然语言描述。前端把它原样展示在口径标签上，这是「数字可追溯」的基础。

### GET /api/semantic/tables

返回 `items`（表结构元数据）与 `dimensions`（九类可用维度）。字段元数据含字段名、类型、中文名、枚举值说明、备注。

### GET /api/semantic/synonyms

返回三组：`synonyms`（业务黑话到标准字段或指标的映射）、`value_synonyms`（枚举值的口语表达映射）、`time_terms`（相对时间词与换算规则）。

改了这三组之后不需要重启之外的任何操作，配置是启动时加载的。

### GET /api/semantic/recall

调试接口，不调用模型：

```
GET /api/semantic/recall?q=上个月各渠道的成交金额&with_context=true
```

返回命中的指标、表、维度、匹配词、值提示与时间区间。`with_context=true` 时额外返回 `context`，即渲染后的 Prompt 段落，等于看到模型实际会收到什么。

排查「召回错了」这一类问题时，这个接口比看日志快。

### GET /api/semantic/health

```json
{
  "consistent": true,
  "declared_count": 6,
  "actual_count": 6,
  "missing_in_semantic": [],
  "missing_in_db": [],
  "row_counts": { "users": 1200, "categories": 24, "products": 860, "orders": 45000, "order_items": 118000, "refunds": 2100 }
}
```

`missing_in_semantic` 非空表示业务库里有表没登记，生成的 SQL 会被守卫当成越权拦下。`missing_in_db` 非空表示语义层注册了不存在的表，召回会把模型引向不存在的对象。

`row_counts` 用来确认造数是否成功。SQLite 的内部表（`sqlite_stat1` 等）已被排除，不参与一致性核对。

## 对话接口

### 请求体

两个接口共用同一个请求模型：

```json
{ "question": "上个月各渠道的成交金额", "session_id": "3f2a1c9e-..." }
```

`question` 长度 1 到 500 字符。`session_id` 必须已存在，否则返回 404。这是最容易犯的调用错误：直接拿一个自己拼的 UUID 去调 `/api/chat`。

### POST /api/chat/sync

非流式版本，一次返回完整结果，供脚本、评测集与 curl 调试使用。响应体就是下面「result 事件」里的结构。

```bash
curl -s http://127.0.0.1:8000/api/session -X POST -H 'Content-Type: application/json' -d '{"title":"调试"}'
# 拿到 id 后
curl -s http://127.0.0.1:8000/api/chat/sync \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"<上一步的 id>","question":"上个月各渠道的成交金额"}'
```

### POST /api/chat

流式版本，返回 `text/event-stream`。响应头带 `X-Accel-Buffering: no`，用于关闭 Nginx 的响应缓冲。

## SSE 事件类型

共四类事件。

### stage

每经过一个节点推一条，前端据此更新进度条。

```json
{"node": "generate", "label": "正在生成查询", "status": "running", "detail": "第 1 次尝试"}
```

`status` 取 `running`、`done`、`failed`。`detail` 是可选的补充说明，重试时会带上第几次尝试。

### result

本轮最终结果，一次对话有且只有一条。

| 字段 | 类型 | 说明 |
|---|---|---|
| `answer` | string | 一句话结论 |
| `sql` | string \| null | 实际执行的 SQL，可能被 LIMIT 包裹；澄清或未生成时为 null |
| `original_sql` | string \| null | 模型生成的原始语句，用于回溯 |
| `columns` | string[] | 结果集列名 |
| `rows` | array | 结果集行，最多 1000 行 |
| `row_count` | int | 本轮下发的行数，非真实命中行数 |
| `truncated` | bool | 结果是否被行数上限截断 |
| `chart` | object \| null | 图表配置，含 chart_type、title、x_field、y_fields、series |
| `metric_used` | string[] | 本轮命中的指标名 |
| `caveats` | string[] | 口径提示，例如「不含已取消订单」 |
| `time_range` | object \| null | 换算后的时间区间，含 label、start、end |
| `guard` | object \| null | 被守卫拦截时的规则编号与原因，含 rule_id、reason |
| `status` | string | `ok` / `clarify` / `chitchat` / `error` |
| `error` | string \| null | 失败时的原因 |
| `stages` | array | 本轮经过的节点，每项含 node、label、detail、elapsed_ms（该节点耗时毫秒）。作用域为单轮问答，下一轮重新累积；协议里没有 status 字段，运行态由前端自行推导 |
| `query_elapsed_ms` | float | SQL 执行耗时 |
| `attempts` | int | 生成尝试次数 |

`status` 四个取值的含义：

- `ok`：正常回答，`sql`、`rows`、`chart` 均已填充；
- `clarify`：信息不足，`answer` 是回问话术，`sql` 与 `chart` 为 null；
- `chitchat`：非取数问题（问候、致谢），`answer` 给出可用问法示例，不产生 SQL；
- `error`：重试耗尽或链路异常，`error` 给出最后一次失败的原因。

前端按四个值分支。只按 `ok` / `clarify` / `give_up` 三值处理会漏掉 `chitchat` 与 `error`，前者会显示空白结果，后者会丢失失败原因。

### error

编排层的意外异常。节点内部的失败不会走这里，那是 `give_up` 节点的职责。

```json
{"message": "服务端处理失败：..."}
```

### done

终止帧，`data` 为空对象。收到后前端应主动关闭连接，否则部分浏览器会认为连接仍在等待并触发重连。

## 客户端调用示例

### Python

```python
import json
import requests

BASE = "http://127.0.0.1:8000"

sid = requests.post(f"{BASE}/api/session", json={"title": "调试"}).json()["id"]

with requests.post(
    f"{BASE}/api/chat",
    json={"session_id": sid, "question": "上个月各渠道的成交金额"},
    stream=True,
    headers={"Accept": "text/event-stream"},
) as resp:
    event = None
    for line in resp.iter_lines(decode_unicode=True):
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            payload = json.loads(line[6:])
            if event == "result":
                print(payload["answer"], payload["sql"])
            event = None
```

关键点：`stream=True` 与 `iter_lines`。少了 `stream=True`，requests 会等到响应结束才返回，进度反馈完全失效。

### JavaScript

浏览器原生 `EventSource` 只支持 GET，本接口是 POST，因此前端用 `fetch` 配合 `ReadableStream` 手工解析帧，或者用 `@microsoft/fetch-event-source` 这类库。

```ts
const resp = await fetch('/api/chat', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id: sid, question: q }),
});
const reader = resp.body!.getReader();
const decoder = new TextDecoder();
let buf = '';
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buf += decoder.decode(value, { stream: true });
  const frames = buf.split('\n\n');
  buf = frames.pop() ?? '';
  for (const frame of frames) {
    const ev = frame.match(/^event: (.+)$/m)?.[1];
    const data = frame.match(/^data: (.+)$/m)?.[1];
    if (ev && data) handle(ev, JSON.parse(data));
  }
}
```

按 `\n\n` 切帧而不是按行，是因为事件之间以空行分隔，逐行解析会在 data 含换行时出错。本实现已把 data 压成单行 JSON，但这个假设不应写死在客户端里。

## 常见调用问题

**404 会话不存在。** 未先调 `POST /api/session`，或用了前端本地生成的 ID。

**SSE 进度一次性吐出。** 反向代理开启了响应缓冲。Nginx 需要 `proxy_buffering off` 与 `proxy_read_timeout` 放大，应用已带 `X-Accel-Buffering: no`，两者都要配。

**`result` 里的 `rows` 为空但 `row_count` 大于 0。** 不可能，两者同源。出现 `row_count` 为 0 而预期有数据时，先看 `status` 与 `error`。

**`/api/stats` 长期为 0。** 当前是桩实现，桩实现不计数。看 `/api/health` 的 `llm_mock`。

**语义层接口返回 500 且 type 为 SemanticError。** YAML 配置有问题，detail 里列出具体是哪一项、哪个字段。
