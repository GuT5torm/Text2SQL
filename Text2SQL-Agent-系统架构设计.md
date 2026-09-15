# Text2SQL 电商数据分析 Agent — 系统架构设计

> 技术栈：Python 3.12+ / LangGraph / SQLite + SQLAlchemy / ECharts / 国内直连 OpenAI 兼容 LLM
> 领域：电商零售　｜　定位：课程作业 · 毕设演示　｜　运行形态：本地单机

---

## 0. 需求定位与架构约束

架构设计的起点是把约束写清楚。以下四个维度来自需求澄清，直接决定后面每一个选型。

| 维度 | 现状 | 对架构的影响 |
|---|---|---|
| 规模 | 演示级，单用户，数据量不确定（先小后大） | 无需并发设计；SQLite 可用；必须留存储切换口子 |
| 团队 | 小规模（个人 / 2-3 人） | 排除一切需要专职运维的组件 |
| 阶段 | MVP，目标是链路跑通并演示 | 复杂度集中在 Agent 层，基础设施保持最小 |
| 约束 | 本地运行、无预算、有答辩截止时间 | 单进程启动；不引入云服务依赖；不容器化 |

**核心设计判断**：这个项目的技术价值在于 Agent 编排与语义层，不在于基础设施。因此基础设施刻意保持极简（单进程、单文件数据库、无中间件），把全部工程复杂度投入到「自然语言到可信 SQL」这一件事上。凡是不能直接提升 SQL 准确率或结果可信度的组件，一律不加。

---

## 1. 架构形态选型

### 候选对比

| 形态 | 是否采用 | 理由 |
|---|---|---|
| 模块化单体 Modular Monolith | **采用** | 单机演示、无并发压力、调试链路短；模块间用函数调用而非网络 |
| 微服务 | 不采用 | 采用条件（团队≥10 人 + 边界稳定 + 有运维能力）三条全部不满足，引入注册中心与链路追踪的成本远超收益 |
| 事件驱动 | 不采用 | 无异步削峰需求；问答链路本身是同步的 |
| CQRS | 不采用 | 唯一的写入是会话记录，读写比不悬殊 |
| Serverless | 不采用 | 长连接 SSE + 本地演示，冷启动与厂商绑定都是负担 |

### 结论

**模块化单体 + 单进程 FastAPI**

```
[浏览器] → [FastAPI 单进程] → [LangGraph Agent 模块] → [SQLite]
```

模块划分（agent / semantic / db / llm / chart）在代码层面强制隔离，模块之间只通过明确的函数签名交互，为将来可能的拆分留出接缝。这不是为了「将来一定拆」，而是让模块边界在开发期就保持清晰，避免所有逻辑堆在一个文件里。

---

## 2. 分层架构

### 架构图

```
┌──────────────────────────────────────────────────────────────────┐
│  浏览器（单页应用）                                                │
│  对话区  │  ECharts 图表区  │  生成的 SQL（可折叠）  │  结果表格    │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTP + SSE（流式推送节点进度）
┌───────────────────────────▼──────────────────────────────────────┐
│  接入层  FastAPI                                                   │
│  /api/chat(SSE)  │  /api/session  │  /api/semantic  │  静态资源     │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│  编排层  LangGraph 状态机                                          │
│                                                                    │
│  route ──┬── 闲聊/非取数 ─────────────────────────► respond        │
│  意图识别 ├── 信息不足 ──────► clarify（反问用户）┄┄► END           │
│          └── 取数 ──► retrieve ──► generate ──► validate           │
│                      语义召回        SQL 生成    安全校验           │
│                                                    │ 不通过         │
│                                                    ▼（携带错误）    │
│                                                   fix ◄───┐        │
│                                                    │      │        │
│                                               通过 ▼      │        │
│                                                   execute          │
│                                                    │ 失败          │
│                                                    ▼（携带错误）    │
│                                      visualize ──► summarize       │
│                                    ECharts 配置    经营结论         │
│                                                                    │
│  Checkpointer：多轮会话记忆（MemorySaver → SQLiteSaver）             │
└───────┬───────────────────────┬───────────────────┬──────────────┘
        │                       │                   │
┌───────▼────────┐  ┌───────────▼─────────┐  ┌──────▼──────────────┐
│ 语义层          │  │ LLM 网关            │  │ 数据访问层           │
│ metrics.yaml   │  │ OpenAI 兼容客户端    │  │ Repository 抽象      │
│ tables.yaml    │  │ 超时/重试/降级       │  │ 只读引擎 + SQL 守卫   │
│ 同义词 / 口径   │  │ 结构化输出解析        │  │ 超时 + 行数上限       │
└───────┬────────┘  └───────────┬─────────┘  └──────┬──────────────┘
        │                       │                   │
┌───────▼───────────────────────▼───────────────────▼──────────────┐
│  数据层  SQLite（只读连接 + WAL）  │  配置层 .env / pydantic-settings │
└──────────────────────────────────────────────────────────────────┘
```

### 各层职责与边界

| 层 | 职责 | 明确不做什么 |
|---|---|---|
| 接入层 | HTTP 路由、SSE 推送、请求校验、静态资源 | 不写业务逻辑，不直接访问数据库 |
| 编排层 | 状态机流转、节点调度、重试控制、会话记忆 | 不拼接 SQL 字符串，不解析图表 |
| 语义层 | 指标口径、表结构元数据、同义词、召回 | 不执行 SQL，不调用 LLM |
| LLM 网关 | 统一模型调用、超时重试、结构化输出解析、用量统计 | 不含任何业务 Prompt 内容 |
| 数据访问层 | 连接管理、SQL 校验、执行、结果规整 | 不生成 SQL，不做业务判断 |
| 前端 | 渲染对话、图表、SQL、表格 | 不做任何计算与判断 |

### 无状态原则

编排层之上不保存请求级状态，会话状态统一交给 LangGraph 的 Checkpointer（以 session_id 为键）。这使服务重启后会话可恢复（切到 SQLiteSaver 后），也让后续横向扩展成为可能。

---

## 3. 代码目录结构

```
text2sql-agent/
├── app/
│   ├── main.py                    # FastAPI 入口、路由挂载、静态资源
│   ├── config.py                  # pydantic-settings 读取 .env
│   ├── api/
│   │   ├── chat.py                # POST /api/chat（SSE 流式返回）
│   │   ├── session.py             # 会话创建 / 历史查询
│   │   └── semantic.py            # GET /api/semantic（暴露口径与表结构）
│   ├── agent/
│   │   ├── graph.py               # LangGraph 图定义：节点注册 + 边 + 条件路由
│   │   ├── state.py               # AgentState TypedDict
│   │   ├── nodes/
│   │   │   ├── route.py           # 意图识别与澄清判断
│   │   │   ├── retrieve.py        # 语义层召回
│   │   │   ├── generate.py        # SQL 生成
│   │   │   ├── validate.py        # SQL 安全校验
│   │   │   ├── execute.py         # 执行查询
│   │   │   ├── fix.py             # 依据错误信息修正 SQL
│   │   │   ├── visualize.py       # 结果 → ECharts 配置
│   │   │   └── summarize.py       # 结果 → 经营结论
│   │   └── prompts/
│   │       ├── route.md
│   │       ├── generate.md        # 含口径定义 + 表结构 + few-shot
│   │       ├── fix.md
│   │       └── summarize.md
│   ├── semantic/
│   │   ├── metrics.yaml           # 指标口径库
│   │   ├── tables.yaml            # 表结构与字段元数据
│   │   ├── synonyms.yaml          # 业务黑话 → 标准字段/指标
│   │   └── loader.py              # 加载、校验、召回
│   ├── llm/
│   │   ├── gateway.py             # 客户端封装：超时/重试/降级/用量
│   │   └── schemas.py             # 结构化输出的 Pydantic 模型
│   ├── db/
│   │   ├── engine.py              # SQLAlchemy engine（只读）
│   │   ├── repository.py          # Repository 抽象接口
│   │   ├── sqlite_repo.py         # SQLite 实现
│   │   └── guard.py               # SQL 安全校验（AST 白名单）
│   └── chart/
│       └── builder.py             # 结果集 → ECharts option 的兜底构建
├── data/
│   ├── ecommerce.db               # 样例电商库
│   └── seed.py                    # 造数脚本（含可复现随机种子）
├── web/
│   ├── index.html                 # 单页界面
│   ├── app.js                     # 对话 + SSE 接收 + ECharts 渲染
│   └── style.css
├── eval/
│   ├── cases.yaml                 # 评测集：问题 → 期望结果
│   └── run_eval.py                # 准确率回归脚本
├── tests/
├── .env.example
├── requirements.txt
└── README.md
```

---

## 4. Agent 编排设计（LangGraph）

### 状态定义

状态是整个编排层的数据契约，字段设计决定节点之间能否松耦合。

```python
from typing import Annotated, TypedDict, Literal
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    # 会话
    messages: Annotated[list, add_messages]   # 多轮历史，由 Checkpointer 持久化
    question: str                             # 当前轮问题（已做指代消解）

    # 语义召回
    retrieved: dict                           # 命中的指标口径 + 表结构 + few-shot 示例

    # SQL 生命周期
    sql: str | None
    sql_error: str | None                     # 校验或执行失败的原因
    retry_count: int                          # 修正重试计数
    rows: list[dict] | None
    columns: list[str] | None
    row_count: int | None

    # 输出
    chart_spec: dict | None                   # ECharts option
    insight: str | None                       # 经营结论
    metric_used: list[str]                    # 本轮用了哪些指标口径（用于前端标注）

    # 控制
    intent: Literal["query", "chitchat", "clarify"] | None
    clarify_question: str | None
```

### 图结构

| 节点 | 输入 | 输出 | 职责 |
|---|---|---|---|
| `route` | question + messages | intent | 判断是取数、闲聊还是信息不足。信息不足时产出反问话术 |
| `retrieve` | question | retrieved | 从语义层召回相关指标口径、表结构、few-shot 示例 |
| `generate` | question + retrieved | sql | 生成 SQL，只输出 SQL 文本 |
| `validate` | sql | sql_error | AST 级安全校验，拦截非 SELECT、越权表、无 LIMIT 的查询 |
| `execute` | sql | rows / sql_error | 只读执行，带超时与行数上限 |
| `fix` | sql + sql_error | sql | 携带数据库原始报错重新生成，最多 N 次 |
| `visualize` | rows + question | chart_spec | 选择图表类型并生成 ECharts 配置 |
| `summarize` | rows + metric_used | insight | 生成经营结论，并标注所用口径与数据范围 |

### 条件路由

```
route
  ├─ intent == "chitchat"  → respond → END
  ├─ intent == "clarify"   → clarify → END          # 反问用户，等下一轮
  └─ intent == "query"     → retrieve → generate → validate

validate
  ├─ 校验通过 → execute
  └─ 校验失败 → fix（retry_count += 1）
                  └─ retry_count >= 3 → 失败兜底 → END（返回可读的错误说明）

execute
  ├─ 成功 → visualize → summarize → END
  └─ 失败 → fix（同上，共享重试配额）
```

`fix` 节点是整条链路可靠性的关键。数据库返回的报错（字段不存在、语法错误、聚合不合法）信息量远大于让模型自查，把它原样回传给模型做定向修正，比重新生成一次的成功率高得多。

### 多轮与指代消解

多轮场景的典型问题是「那上个月呢」这类省略主语的追问。处理方式：

1. 把最近的 N 轮对话（问题与结论摘要）注入 `route` 与 `generate` 节点的 prompt；
2. 在 `generate` 之前不单独设指代消解节点，而是让模型在生成 SQL 时直接参考历史 SQL 与历史问题，避免多一次 LLM 调用；
3. 前端在会话中保留上一轮的 `metric_used`，便于模型对齐上下文。

### 重试配额

`retry_count` 在 validate 与 execute 之间共享，上限 3 次。达到上限后不再重试，返回结构化的失败说明（包含最后一次的 SQL 与报错），避免无限循环消耗 token。

---

## 5. 语义层设计

语义层是这套架构与「直接把 schema 丢给 LLM」的方案的实质区别，也是准确率与可信度的主要来源。

### 要解决的问题

同一个业务词在不同人嘴里指向不同的 SQL。以「销量」为例：可能指订单数（`COUNT(*)`），也可能指商品件数（`SUM(quantity)`）；「销售额」是否扣除退款、是否含取消订单，都是口径问题。如果这些问题交给模型猜测，结果就不可复现，也无法向答辩老师解释数字是怎么来的。

### metrics.yaml：指标口径库

```yaml
metrics:
  - name: gmv
    display: 成交总额
    synonyms: [销售额, 营业额, GMV, 交易额, 卖了多少钱]
    expression: "SUM(oi.quantity * oi.unit_price)"
    tables: [orders, order_items]
    required_filters:
      - "orders.status != 'cancelled'"
    dimensions: [order_date, category_name, region, channel]
    caveats: "不含已取消订单；不扣除退款，退款率需另看 refund_rate"

  - name: sales_volume
    display: 销量
    synonyms: [卖了多少, 销售量, 卖出的件数]
    expression: "SUM(oi.quantity)"
    tables: [order_items]
    default_note: "默认口径为商品件数；如需订单数口径，请明确说明"

  - name: aov
    display: 客单价
    synonyms: [平均订单金额, 每单金额, 单均]
    expression: "SUM(oi.quantity * oi.unit_price) / COUNT(DISTINCT orders.id)"
    tables: [orders, order_items]
    required_filters:
      - "orders.status != 'cancelled'"
    caveats: "按订单数口径；若需按付费用户数口径，分母改为 COUNT(DISTINCT orders.user_id)"

  - name: refund_rate
    display: 退款率
    synonyms: [退货率, 退款比例]
    expression: "COUNT(DISTINCT r.order_id) * 1.0 / COUNT(DISTINCT o.id)"
    tables: [refunds, orders]
    caveats: "以订单数为分母，非金额口径"
```

### tables.yaml：表结构元数据

```yaml
tables:
  - name: orders
    display: 订单表
    synonyms: [订单, 单子, 交易]
    grain: 一行一个订单
    fields:
      - name: id
        type: INTEGER
        display: 订单ID
      - name: user_id
        type: INTEGER
        display: 用户ID
      - name: status
        type: TEXT
        display: 订单状态
        enum:
          pending: 待支付
          paid: 已支付
          shipped: 已发货
          completed: 已完成
          cancelled: 已取消
          refunded: 已退款
      - name: created_at
        type: TEXT
        display: 下单时间
        note: "存储为 ISO8601 字符串，日期函数使用 SQLite 的 strftime"

  - name: order_items
    display: 订单明细表
    grain: 一行一个订单中的一个商品
    join: "order_items.order_id = orders.id"
    fields:
      - name: product_id
        type: INTEGER
        display: 商品ID
      - name: quantity
        type: INTEGER
        display: 购买数量
      - name: unit_price
        type: REAL
        display: 成交单价
```

### 召回策略

起步用**规则匹配，不引入向量数据库**。

1. 对用户问题做分词与同义词归一，命中 `synonyms.yaml` 中的业务黑话；
2. 按命中的指标与字段，反查依赖的表；
3. 组装 prompt 时只注入相关的 2-4 张表与 2-3 个指标口径，而不是全库 schema。

这样做有三个好处：prompt 长度可控、无关字段不会干扰模型、演示时无需额外依赖。当表数量增长到数十张、规则匹配召回率下降时，再引入 embedding 检索——这是明确的切换触发条件，届时只需替换 `retrieve` 节点的实现。

### 口径透传

每轮回答在前端标注「本轮使用的口径」，例如「成交总额 = 订单明细金额合计，不含已取消订单」。这一步把不可见的假设变成可见的说明，既是可信度的体现，也是答辩时的加分项。

---

## 6. SQL 安全沙箱

即使演示场景只有自己使用，也必须做校验。原因有两个：LLM 输出存在不确定性，一次 `DROP TABLE` 就意味着重建数据；同时这套机制本身是项目的技术亮点。

### 校验规则（在 `db/guard.py` 中实现）

| 规则 | 实现要点 |
|---|---|
| 只允许单条 SELECT | 用 SQL 解析库（如 sqlglot，版本需核实）解析为 AST，检查语句类型与语句数量 |
| 禁止 DDL / DML | 拒绝 CREATE / DROP / ALTER / INSERT / UPDATE / DELETE / ATTACH / PRAGMA 写操作 |
| 表名白名单 | AST 中出现的所有表必须在 `tables.yaml` 登记 |
| 字段白名单 | 字段必须在对应表的元数据中存在，拦截模型编造的字段 |
| 强制 LIMIT | 无 LIMIT 或 LIMIT 超过阈值时，自动包裹为 `SELECT * FROM (原SQL) LIMIT 1000` |
| 拦截笛卡尔积 | 检测多表查询中缺失 JOIN 条件的情况，提示重新生成 |
| 执行超时 | 用 SQLite 的中断机制或独立线程设超时，默认 10 秒 |
| 行数上限 | 结果集超过阈值时截断并提示 |

### 连接层加固

数据库连接使用只读模式打开；对 SQLite 启用 WAL 模式以便读并发；连接串中不暴露写权限。前端展示 SQL 是刻意保留的——让使用者看到实际执行的语句，比藏起来更可信。

---

## 7. LLM 网关

国内可直接调用、且提供 OpenAI 兼容接口的服务商包括阿里云百炼（通义千问）、DeepSeek、智谱 GLM、月之暗面 Kimi、火山方舟（豆包）、硅基流动等。**具体模型名称、接口地址与计费方式需以各家最新文档为准，此处不做假定。**

### 网关职责

```
[Agent 节点] → [LLM 网关] → [服务商 API]
                  │
                  ├─ 统一超时（默认 30s，流式单独配置）
                  ├─ 重试（指数退避，仅对超时与限流重试）
                  ├─ 降级（主模型失败切备用模型，配置化）
                  ├─ 结构化输出（优先 JSON mode，不支持则退回文本解析 + 重试）
                  └─ 用量统计（token 数、耗时，写入本地日志）
```

### 结构化输出的处理

节点需要模型返回结构化数据（如 `visualize` 返回图表配置）时，定义对应的 Pydantic 模型，网关负责解析与校验。需要留意的是，并非所有国内模型都完整支持 function calling，实现时以 JSON mode 为主、文本解析兜底，解析失败时重试一次并在 prompt 中强化格式要求。

### 配置项

全部通过 `.env` 注入，代码中不出现密钥：模型名称、接口地址、API Key、超时、重试次数、备用模型。

---

## 8. 前端与图表

### 页面结构

单页三区布局：左侧对话流，右侧上方图表区、下方结果表格，生成的 SQL 以可折叠面板附在每轮回答下方。

### 流式反馈

后端通过 SSE 推送节点进度，前端在等待期间显示当前阶段，例如「正在理解问题」「正在生成查询」「正在执行」「正在分析结果」。查询类请求通常要数秒到十几秒，没有进度反馈的界面会让人以为卡死。

### 图表生成

两条路径并存，以模型输出为主、规则兜底：

1. `visualize` 节点让模型输出 ECharts option 的 JSON，包含图表类型（折线 / 柱状 / 饼图 / 表格）、维度映射、系列配置；
2. `chart/builder.py` 依据结果集的结构（维度数、度量数、行数）生成兜底配置。时间维度走折线、单维度对比走柱状、占比走饼图、多列宽表退回表格。

图表配置在前端渲染前做一次字段校验，防止模型输出的 option 引用不存在的字段导致空白图表。

---

## 9. 关键架构决策（ADR）

### ADR-001：采用模块化单体而非微服务

- **场景**：课程作业演示，单机运行，小团队，无并发压力
- **决策**：单进程 FastAPI，按 agent / semantic / db / llm / chart 划分模块，模块间函数调用
- **理由**：对比微服务方案——微服务需要注册中心、链路追踪、容器编排，这些都需要专职运维，而本项目没有。单体调试链路短，一条命令启动，符合演示需求
- **代价**：模块边界依赖代码规范自律，不能独立扩缩容。若后续产品化，`agent` 模块是最先可以独立拆出的
- **状态**：已接受

### ADR-002：保留 SQLite，用 Repository 抽象隔离

- **场景**：数据规模不确定，需求方选择「先小后大」
- **决策**：SQLite 起步，所有数据访问经 Repository 接口，不在 Agent 节点中直接拼接 SQL
- **理由**：SQLite 零运维、单文件、便于随项目一起提交。Repository 隔离使存储切换只影响一个实现类
- **代价**：增加少量样板代码。SQLite 的方言差异（日期函数、窗口函数、并发写锁）需在 Repository 内部消化，不能泄漏到上层
- **切换触发条件**：单表超过约 500 万行、出现真实并发写入、或需要行级权限体系时，换 PostgreSQL，仅新增 `postgres_repo.py`
- **状态**：已接受

### ADR-003：在 LLM 与数据库之间引入语义层

- **场景**：直接投喂 schema 的方案存在口径歧义（「销量」「销售额」的多种解释）与幻觉（编造字段）
- **决策**：建立指标口径库与表结构元数据，召回后再注入 prompt
- **理由**：对比「只给 schema」的方案——语义层把业务口径从模型的猜测变为显式配置，结果可复现、可审计；召回子集也降低了 prompt 噪声
- **代价**：需要人工维护 YAML，口径变更要同步更新。这是必要的维护成本，换来的是结果可信
- **状态**：已接受

### ADR-004：SQL 安全沙箱前置校验

- **场景**：模型生成的 SQL 直接执行存在破坏性风险
- **决策**：执行前做 AST 级白名单校验，只读连接，强制 LIMIT，超时与行数上限
- **理由**：模型输出存在不确定性，防护成本远低于数据重建成本；同时构成项目技术亮点
- **代价**：少数合法复杂 SQL 可能被误拦，需要维护白名单与规则。可通过 `fix` 节点把拦截原因回传给模型重新生成来缓解
- **状态**：已接受

### ADR-005：会话记忆使用 LangGraph Checkpointer

- **场景**：多轮对话需要上下文
- **决策**：使用 LangGraph 原生 Checkpointer，起步 `MemorySaver`，需要跨进程持久化时切 `SQLiteSaver`
- **理由**：对比引入 Redis——本场景无需跨机共享与会话过期策略，LangGraph 原生支持即可满足，少一个依赖
- **代价**：`MemorySaver` 在进程重启后丢失会话，切换到 `SQLiteSaver` 可解决，切换成本仅为一行配置
- **状态**：已接受

### ADR-006：语义层召回先用规则匹配

- **场景**：需要从指标与表结构中召回相关子集
- **决策**：先用同义词表 + 关键词匹配，不引入向量数据库
- **理由**：演示级数据只有数张表、数十个字段，规则匹配召回率足够，且无额外依赖与索引维护成本
- **代价**：表数量增长后召回率下降
- **切换触发条件**：表数量超过约 30 张，或规则匹配的漏召回在评测集上可观测地拉低准确率时，替换 `retrieve` 节点为 embedding 检索
- **状态**：已接受

---

## 10. 分阶段演进路线

| 阶段 | 形态 | 触发信号 | 现在不做 |
|---|---|---|---|
| P0 演示版（当前） | 单进程 FastAPI + SQLite + 规则召回语义层 + MemorySaver | 答辩演示，单用户，本地运行 | 不上向量库、不上 Redis、不上 Docker、不做用户体系与鉴权 |
| P1 加固 | SQLiteSaver 会话持久化 + 查询结果缓存 + 评测集回归 | 演示反馈要求提升准确率，或需要多轮会话跨重启保留 | 不换数据库、不上多租户 |
| P2 产品化 | PostgreSQL + embedding 召回 + JWT 鉴权 + Docker Compose | 有真实业务接入，多用户并发 | 引入微服务需先满足团队≥10 人、边界稳定、具备运维能力三条 |

每个阶段的「现在不做」清单与「要做的事」同等重要。跳过 P0 直接按产品化设计，是本项目最常见的过度设计风险。

---

## 11. 风险清单

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| LLM 生成 SQL 准确率不稳定 | 答非所问，演示效果不可控 | 语义层注入口径 + few-shot 示例库 + `fix` 节点带错误重试 + `eval/` 评测集回归 |
| 指标口径歧义 | 结论错误且难以解释 | 语义层显式定义 + 前端标注本轮口径 + `caveats` 透传 |
| 模型编造字段或表名 | SQL 执行报错 | 字段与表白名单校验，校验失败走 `fix` |
| 国内 LLM 接口限流或超时 | 请求长时间挂起 | 网关统一超时、指数退避重试、备用模型降级 |
| 多表 JOIN 缺失条件 | 笛卡尔积导致查询卡死 | 校验节点检测并拦截，超时机制兜底 |
| SQLite 并发写锁 | 演示时偶发卡顿 | 只读连接 + WAL 模式；演示场景单用户，实际影响有限 |
| 长查询阻塞页面 | 用户以为系统无响应 | SSE 推送节点进度 + 查询超时 + 行数上限 |
| 评测集缺失导致改动无法验证 | 调整 prompt 后准确率反而下降却不自知 | 从第一天起维护 `eval/cases.yaml`，每条包含问题与期望结果 |
| 模型与接口版本变动 | 代码在某天突然失效 | 模型名称与接口地址全部走配置；`requirements.txt` 锁定依赖版本 |

其中评测集最容易被忽略。没有评测集，每一次 prompt 调整都是在盲改，答辩时也无法给出「准确率是多少」这个必然被问到的问题。

---

## 12. 实施顺序建议

按依赖关系排序，每一步都可独立验证：

1. **数据与元数据**：`seed.py` 造出可复现的样例电商库 + 编写 `tables.yaml`
2. **数据访问层**：Repository 抽象 + SQLite 实现 + SQL 守卫，用单测覆盖校验规则
3. **LLM 网关**：打通一家服务商的调用，跑通一次「文本进、SQL 出」
4. **最小 Agent**：`generate → validate → execute` 三节点线性图，先不做语义层，验证链路可行
5. **语义层**：补 `metrics.yaml` 与召回逻辑，接入 `retrieve` 节点
6. **补齐节点**：`route` / `fix` / `visualize` / `summarize`
7. **前端**：先做能看结果的静态页，再加 SSE 进度与图表
8. **评测集**：`eval/cases.yaml` + `run_eval.py`，建立准确率基线
9. **多轮**：接入 Checkpointer，处理指代消解

先做第 4 步的三节点链路，是为了尽早暴露「模型能不能稳定产出可执行 SQL」这个最大的不确定性。这个前提不成立，后面的语义层与图表都无从谈起。

---

## 附：需要核实的事项

以下内容未在本设计中给出确定值，实施前需查阅最新文档确认：各 LLM 服务商的模型名称、接口地址与计费方式；SQL 解析库的选型与版本（sqlglot 或同类）；LangGraph 与 LangChain 的当前版本及 API 形态；SQLite 日期函数的具体用法。依赖版本建议在 `requirements.txt` 中锁定。
