# Text2SQL 电商数据分析 Agent

用自然语言提问电商经营数据，系统自动完成口径匹配、SQL 生成、安全校验、只读执行、图表绘制与结论撰写。

与「把建表语句交给大模型直接写 SQL」的做法相比，本项目的差别集中在三处：指标口径写在语义层配置里而非由模型临场判断，生成的语句必须先通过 SQL 安全沙箱再执行，本轮采用的统计范围会随结果一并返回。因此同一个问题在不同时间、不同机器上的口径一致，每一个数字都能追溯到具体的表与条件。

系统面向单机演示与教学实训，业务库以只读方式连接，不提供数据写入能力。

---

## 一、系统能力与边界

**能做的事**

| 能力 | 说明 |
|---|---|
| 自然语言取数 | 支持指标、维度、时间范围、筛选、排序与条数的组合提问 |
| 口径约束 | 七个指标的口径、必带过滤条件与边界说明集中在 `semantic/metrics.yaml` |
| 多轮追问 | 支持指代时间、增删维度、改筛选、改排序等上下文延续 |
| 安全执行 | 语句白名单、表字段白名单、强制行数上限、执行超时与扫描量中断 |
| 图表与结论 | 图型由后端决策，结论不允许出现结果集以外的数字 |
| 异常标记 | 自动识别结果中的异常切片并支持下钻一层 |
| 对比基准 | 与上一周期对比，不完整周期会被隔离并标注 |
| 历史与导出 | 会话落库，可导出 PDF / CSV / PPTX / Markdown，可生成只读分享链接 |
| 异常订阅 | 按规则周期求值，命中或失败写入事件列表 |

**不做的事**

- 不写数据。业务库以只读模式打开，任何写入语句在执行前会被拒绝。
- 不做用户体系与权限控制。单机运行，接口只在本机监听。
- 不接向量数据库。语义层召回基于规则与同义词匹配，适用于演示规模的表数量。
- 不内置定时任务。异常订阅的求值周期由外部计划任务触发，见第十节。

---

## 二、环境要求

| 项目 | 要求 | 本项目实测 |
|---|---|---|
| 操作系统 | Windows 10/11、Linux、macOS | Windows 11 |
| Python | 3.11 及以上 | 3.13.14 |
| Node.js | 18 及以上（Vite 5 的要求） | 22.22.2 |
| LLM 密钥 | 可选。未配置时走桩实现，链路完整但 SQL 来自固定模板 | 示例配置为 `qwen-plus`（百炼兼容入口） |

以下命令均在项目根目录 `C:\Text2SQL` 下展开。PowerShell 与 Git Bash 的路径写法略有差异，文中给出 PowerShell 版本，Git Bash 下把 `.\` 换成 `./` 即可。

---

## 三、安装与初始化

### 3.1 后端依赖

```powershell
cd C:\Text2SQL\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

若 PowerShell 提示「禁止运行脚本」，先执行一次：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

也可以不激活虚拟环境，直接用全路径解释器调用，后续命令都按这个写法给出：

```powershell
.\.venv\Scripts\python.exe <脚本或模块>
```

`requirements.txt` 的版本号全部写死而非写下限。SQL 守卫的判断依赖 sqlglot 的 AST 节点命名，langgraph 1.x 的图编译与 Checkpointer 接口也有过破坏性变更，浮动版本会让同一份代码装出不同的行为。升级依赖时逐个升级，并跑一遍冒烟脚本与单元测试确认链路未断。

### 3.2 前端依赖

```powershell
cd C:\Text2SQL\frontend
npm install
```

`.npmrc` 已把 registry 指向 npmmirror。若所在网络可直连 npmjs，删掉该文件即可。

### 3.3 配置文件

```powershell
cd C:\Text2SQL\backend
Copy-Item .env.example .env
```

配置从 `backend/.env` 读取，代码中不出现密钥字面量。`.env` 已被版本库排除，可安全填写真实密钥。

首次启动最容易出问题的一项是桩实现与密钥的关系：

- `.env` 中 `LLM_API_KEY` 留空且 `LLM_MOCK=true` 时走桩实现，`generate` 节点按问题关键词选择预置 SQL，可以验证整条编排链路，但 SQL 不代表模型能力。
- 填入密钥后必须把 `LLM_MOCK` 置为 `false`，否则仍然走桩实现。

这一对配置也可以在界面上改，见 6.7 节。

### 3.4 生成样例数据库

```powershell
.\.venv\Scripts\python.exe data\seed.py
```

脚本以固定随机种子生成 2000 名用户、500 件商品、12000 笔订单，写入 `backend/data/ecommerce.db`（约 3.4 MB），订单时间覆盖 2025-01-01 至造数当日。同一份代码在任何机器上生成的库完全一致，评测结果才可复现。

| 参数 | 作用 |
|---|---|
| `--keep` | 库已存在时跳过，不覆盖 |
| `--db <路径>` | 指定输出位置 |
| `--end-date <日期>` | 指定数据截止日，用于复现评测基线 |

复现评测基线需要把截止日对齐到评测集声明的参考日：

```powershell
.\.venv\Scripts\python.exe data\seed.py --end-date 2026-09-15
```

---

## 四、启动

### 4.1 开发模式（前后端分离）

前端由 Vite 提供热更新，`/api` 请求代理到后端 8000 端口。开两个终端窗口。

终端一，后端：

```powershell
cd C:\Text2SQL\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

终端二，前端：

```powershell
cd C:\Text2SQL\frontend
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。

### 4.2 交付模式（单进程）

前端构建为静态产物后由后端一并托管，只需启动一个进程。

```powershell
cd C:\Text2SQL\frontend
npm run build

cd C:\Text2SQL\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

浏览器打开 <http://127.0.0.1:8000>。`app/main.py` 启动时检查 `frontend/dist` 是否存在，存在即挂载为静态目录，不存在则只提供 API 并在日志中说明。

注意构建产物的入口 HTML 不缓存、带内容哈希的 chunk 长缓存，这是为了发版后不出现「旧入口请求已删除的旧 chunk」导致的白屏。

### 4.3 进程脚本

`deploy/start.sh` 提供启停与状态查询，适用 Linux、macOS 与 Git Bash：

```bash
./deploy/start.sh start     # 启动并轮询健康检查，最多等 15 秒
./deploy/start.sh status    # 查看进程与探针返回
./deploy/start.sh restart   # 修改 .env 后使用
./deploy/start.sh stop      # 先发 SIGTERM，无响应则 SIGKILL
```

脚本默认只起 1 个 worker。会话记忆若使用 `memory` 模式，图状态保存在进程内存中，多 worker 下同一个会话的请求可能落到不同进程，历史上下文会读不全。需要多 worker 时先把 `CHECKPOINTER` 改为 `sqlite`。

脚本会自动识别 Windows 与 Unix 的解释器路径，并在启动前检查业务库是否存在、端口是否被占用。

### 4.4 启动阶段做了什么

进程启动时会完成一次预热：加载语义层三份 YAML、装配容器、编译 LangGraph 图。这些工作放在启动阶段而不是首个请求里，目的是让配置问题在进程启动时直接暴露——语义层校验不通过会打印完整的问题清单并终止启动，而不是等到用户提问时抛出一个难以定位的错误。

---

## 五、启动自检

三项探针，分别检查服务是否存活、配置是否与库一致、口径能否召回。

### 5.1 存活与服务状态

```powershell
curl.exe http://127.0.0.1:8000/api/health
```

PowerShell 中 `curl` 是 `Invoke-WebRequest` 的别名，需写成 `curl.exe`；或者用 `(Invoke-WebRequest -Uri ...).Content`。

返回字段：

| 字段 | 含义 |
|---|---|
| `status` | 固定为 `ok` |
| `llm_mock` | `true` 表示当前走桩实现，SQL 来自固定模板 |
| `model` | 当前模型名 |
| `checkpointer` | 实际生效的会话记忆模式，配置为 `sqlite` 但依赖缺失时会退回 `memory` |
| `metrics` / `tables` | 语义层登记的指标数与表数，正常为 7 与 6 |
| `data_as_of` | 取订单表与退款表截止日中较早的一个 |
| `alert_last_eval_at` | 异常订阅最近一次求值时刻。长期为空说明外部计划任务没有配置 |
| `alert_unhealthy` | 连续求值失败达到阈值时为 `true` |
| `alert_unread` | 未读事件数，侧边栏角标取这个值 |

### 5.2 语义层一致性

```powershell
curl.exe http://127.0.0.1:8000/api/semantic/health
```

对比语义层登记项与库内实际表名。`consistent` 为 `true` 表示一致；`missing_in_semantic`（库里有、YAML 未登记）与 `missing_in_db`（YAML 登记了、库里没有）两个字段直接指出差异位置，任一非空都要回到 `tables.yaml` 修正。

### 5.3 口径召回

```powershell
curl.exe "http://127.0.0.1:8000/api/semantic/recall?q=上个季度华东区各品类的成交总额&with_context=false"
```

返回命中的指标、表、维度、匹配词与换算后的时间区间。漏召回时通常不是模型问题，而是 `synonyms.yaml` 缺少对应说法。

---

## 六、界面使用

### 6.1 界面结构

界面由左侧会话栏、顶部状态栏与主内容区组成，主内容区按路由切换四个页面。

| 页面 | 路由 | 用途 |
|---|---|---|
| 对话 | `/` | 提问、读结果、追问、下钻、导出与分享 |
| 历史管理 | `/history` | 搜索、筛选、归档、删除、批量导出 |
| 告警订阅 | `/alerts` | 维护规则、查看命中与失败事件 |
| 分享查看 | `/share/{token}` | 只读快照，无需登录 |

左侧会话栏承担日常切换：顶部为数据源、设置、告警订阅、历史管理四个入口，中部为搜索框与「新建对话」按钮，下方会话列表按今天、昨天、7 天内、30 天内、更早分组倒序排列，每条显示标题、轮次与更新时间。悬停可以重命名或删除，点「多选」进入批量删除模式。侧边栏可以收起为图标条。

顶部状态栏固定显示三样东西：只读徽标（点击展开说明业务库只读、语句限定 SELECT、字段以语义层为白名单、结果不含身份字段四条约束）、当前模式标识（桩实现模式为橙色，真实模型显示模型名）、以及异常订阅求值失败时的红色告警标识。

会话采用惰性创建：打开页面或点「新建对话」都不会在库里留下记录，发出第一句提问时才建会话。会话标题由后端在首轮落库时用问句替代默认的「新会话」。

### 6.2 提问

输入框支持 Enter 发送、Shift+Enter 换行，长度上限 500 字。发送后输入框禁用直到本轮结束——后端是单轮串行的，允许连续提交只会让两轮的进度事件混在一起。等待期间可以点「中断本次请求」。

问题包含指标、维度、时间三者中的至少两类，才容易一次命中；信息不足会走澄清分支。

| 组成部分 | 说明 | 示例 |
|---|---|---|
| 指标 | 要算什么 | 成交总额、订单量、退款率、客单价、毛利 |
| 维度 | 按什么分组 | 按品类、按渠道、按大区、按品牌 |
| 时间 | 统计区间 | 上个月、近7天、今年、上个季度 |
| 筛选 | 额外取值限制 | 只看华东、只看某个品牌 |
| 排序与条数 | 要前几名 | 前 10 名、按金额降序 |

相对时间词为精确匹配，写法需与配置一致：配置中登记的是「近7天」而不是「最近 7 天」。「上个月」按上一个自然月换算，不等于最近 30 天。时间区间一律左闭右开，SQL 条件形如 `>= 起始 AND < 结束`。

相对时间词按**数据截止日**换算，不按系统日期：数据截止 2026-09-22 时问「上个月」得到 2026-08，而不是系统日期（如 10 月）所对应的 9 月。截止日取订单与退款两表中较早的最大日期，`/api/health` 的 `data_as_of` 报的就是这个值，问答里算出来的区间与它永远对得上。数据长期不刷新时区间会随之滞后，这是刻意为之——按系统日期换算会落到没有数据的月份，语句能执行、结果必然是空。

### 6.3 一轮回答的构成

回答卡片自上而下依次为：

1. **阶段指示**。按 SSE 到达顺序逐条追加「理解问题 / 检索口径 / 生成查询 / 安全校验 / 执行查询 / 绘制图表 / 分析结果」等节点，完成后收起为一行。
2. **提示条**。按终态与命中情况出现，见第七节。
3. **结论与要点**，附「复制结论」按钮，复制内容含口径与生成时间。
4. **口径标签**。显示本轮命中的指标、时间区间、数据截至日期与对比基准。
5. **异常标记**。识别出的异常切片，每条带下钻入口。
6. **口径定义**（折叠）。展开可看指标的口径表达式、必带条件与来源表。
7. **技术细节**（折叠）。展开可看阶段耗时、执行的 SQL、守卫提示、重试次数、修正历史、问句改写前后对比，以及按分段解释 SQL 的按钮。
8. **图表**。可切换图型与导出图片。
9. **明细表**。超过 50 行时分页，支持单元格下钻。
10. **反馈与导出**。可对结果点赞，或标注问题并选择原因（口径错、字段错、数字错、答非所问）；可导出 PDF、CSV，或生成只读分享链接。

阶段数量不固定：校验或执行失败时会插入修正节点并重走；闲聊只经过意图识别就进入终态；信息不足时进入澄清分支，不执行后续节点。

### 6.4 追问与下钻

第二轮可以直接说「那上个月呢」「按渠道拆开」「改成前 5 名」，意图识别节点在判定意图的同时把问题补全为完整表述后交给下游。上下文由 Checkpointer 按会话 ID 累积，最近 6 轮回灌给模型（`HISTORY_WINDOW`）。

下钻有两个入口：异常标记条目上的按钮，以及明细表单元格。前端不自行拼接问句，而是把槽位交给后端换成问句再提交——下一层维度取自语义层配置，前端拼出来的问句可能与链路能查到的维度对不上。

多轮能接住的是指代时间、增删维度、增减筛选、改排序与条数。隔轮指代、跨指标算术引用、隐含口径切换接不住，表现为重新生成一条无关的 SQL 或走澄清分支。遇到这种情况把问题写完整即可，不必重开会话。

### 6.5 导出与分享

| 出口 | 位置 | 说明 |
|---|---|---|
| PDF | 单轮卡片 | 用浏览器打印实现，自动隐藏顶栏与侧栏，页脚带数据截至与生成时间 |
| CSV | 单轮卡片与明细表 | 导出行数上限 1000，被截断时文件名会标注 |
| 分享链接 | 单轮卡片 | 生成只读快照，链接即凭据，无需登录 |
| PPTX | 历史管理 | 整个会话导出，一页一回合，图表以图片嵌入 |
| Markdown | 历史管理 | 整个会话导出，结果表按 Markdown 表格输出，行数上限 50 |

分享链接只包含已落库的内容，不含聊天上下文，页面上没有提问入口，因此拿到链接的人只能查看这一轮的快照。链接可以设置有效期，过期后访问返回 410。

### 6.6 历史管理

历史管理页提供按标题或提问内容的搜索，以及未归档、已归档、全部三个筛选。归档是软删除：归档后不出现在默认列表，改筛选即可找回；删除是物理移除，连带清除该会话的全部轮次记录。两个动作分开，避免误操作直接抹掉记录。

### 6.7 设置与数据源

**设置**弹窗用于配置模型接入，保存后写入 `backend/.env` 并热重载，无需重启进程：

| 项 | 说明 |
|---|---|
| API Key | 敏感字段，从不回填明文，已配置时留空表示保持不变 |
| Base URL | 模型服务的 OpenAI 兼容入口 |
| 模型 | 模型名，以服务商文档为准 |
| 降级模型 | 可选项。主模型连续失败时降级使用，留空不启用 |
| 桩实现模式 | 开关。关闭且已填密钥后，下一次提问即走真实模型 |

界面可改的字段有白名单限制，其余配置只能改 `.env`，见第八节。

**数据源**弹窗用于在不改配置文件的前提下接入另一个库，支持 SQLite 库文件、PostgreSQL 与 MySQL 连接串。流程是填写地址、测试连接、预览内省结果、应用切换。

应用切换时系统会只读内省新库结构并生成语义层草稿。草稿中表结构取自库内事实，而指标与维度是启发式推测，口径需要人工确认后才能用于生产——这一点在弹窗上有明确提示。

---

## 七、结果解读

### 7.1 口径标签

语义层登记七个指标，命中后显示名与边界说明随结果返回。

| 指标 | 显示名 | 口径表达式 | 边界说明 |
|---|---|---|---|
| `gmv` | 成交总额 | `SUM(oi.quantity * oi.unit_price)` | 不含已取消与未支付订单；不扣除退款 |
| `sales_volume` | 销量 | `SUM(oi.quantity)` | 件数口径，不是订单数 |
| `order_count` | 订单量 | `COUNT(DISTINCT orders.id)` | 不 JOIN 明细表，避免一单多商品重复计数 |
| `aov` | 客单价 | 成交总额 / 订单数 | 分母为订单数口径，非用户数 |
| `refund_rate` | 退款率 | 退款订单数 / 已成交订单数 | 按订单数口径，非金额口径；按原因拆分时一单多退会在各原因下各计一次 |
| `paying_users` | 成交用户数 | `COUNT(DISTINCT orders.user_id)` | 区间内多次下单只计一次 |
| `gross_profit` | 毛利 | `SUM(oi.quantity * (oi.unit_price - p.cost_price))` | 成本取当前成本，不含履约与推广费用 |

三处最容易产生分歧的地方：成交总额不扣退款；退款率的分母是订单数而非金额；时间区间左闭右开。

数字与预期不符时，第一判断依据是口径标签，不是图表。

### 7.2 终态与提示条

每轮由 `status` 决定走向，取值为 `ok`、`clarify`、`chitchat`、`error`。

| 表现 | 含义 | 建议动作 |
|---|---|---|
| 完整回答卡片 | 正常取数 | 读结果或继续追问 |
| 灰色「需要补充信息」 | 问题缺少时间范围或维度 | 补充后重问一次 |
| 常规气泡与可用问法 | 闲聊 | 从示例中选择一个继续 |
| 黄色提示条 | 守卫对语句做了改写，或未命中已登记指标 | 通常无需干预，可展开技术细节核对 |
| 「查询没有返回数据」 | 结果集为空 | 放宽时间范围或减少筛选条件 |
| 「以下数字未能从结果集回溯」 | 结论中的数字无法在结果表中找到 | 人工核对，结论本身未被改动 |
| 红色提示 | 重试耗尽或生成失败 | 缩小范围、减少关联维度，或换一种更具体的问法 |

### 7.3 SQL 安全沙箱

守卫规则共七条，另有一项由仓储层实现的执行中断。

| 规则 | 内容 | 行为 |
|---|---|---|
| R1 | 只允许单条语句 | 拦截 |
| R2 | 只允许 SELECT / UNION / EXCEPT / INTERSECT | 拦截 |
| R3 | 语句树中不得出现 DDL、DML 与管理类关键字 | 拦截 |
| R4 | 表必须在语义层登记 | 拦截，提示疑似模型编造 |
| R5 | 字段必须存在于所属表元数据 | 拦截，并给出形近字段提示 |
| R6 | 无 LIMIT 或超限时包裹为最多 1000 行 | 自动改写并提示 |
| R7 | 多表查询必须带连接条件，禁止 CROSS JOIN | 拦截 |
| R8 | 执行超时或扫描量超限时中断 | 由仓储层 progress handler 实现 |

拦截时展示具体的规则编号与原因，例如「字段 `oi.price` 在表 `order_items` 中不存在，是否为 `unit_price`」，而不是静默失败。R5 的形近字段提示会一并进入修正节点的上下文。

重试配额由校验与执行共享，默认上限 3 次，含义是首次生成之外最多再修正 3 次。空结果回灌另有一个独立配额，默认 1 次，与报错修正分开计数。

### 7.4 结果表格的两个标记

`row_count` 是本轮下发的行数，不是结果集的真实命中行数。`truncated` 为 `true` 时说明已被守卫的行数上限截断，真实行数大于显示行数。需要全量数据应当改写成聚合查询，而不是调大上限。

另外，落库只保留结果集的前 200 行，历史回看看到的是这 200 行，而本轮界面上的结果已随 SSE 完整返回。

### 7.5 图表决策

图型由后端在结果中给出，前端不重新决策，界面右上角的切换只改变渲染形态，不触发重新查询。

| 结果集特征 | 图型 |
|---|---|
| 结果集为空、列数超过 4、无数值度量、无可用横轴维度 | 表格 |
| 问题中含占比、构成一类说法，且单维度单度量、行数不超过 12 | 饼图 |
| 首个维度为时间维度 | 折线 |
| 单维度对比、行数不超过 30 | 柱状 |
| 其余维度组合 | 表格 |

决策顺序是模型优先、规则兜底：可视化节点让模型输出图型与字段映射，图表层校验字段是否真实存在于结果集中，对不上时按上表重新决策。因此模型选错图型不会渲染出空白图表，最多退化为表格。

---

## 八、配置项

全部配置从环境变量或 `backend/.env` 读取，完整清单见 `backend/.env.example`。以下是常改的几项。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 空 | 留空即走桩实现。填入后还需置 `LLM_MOCK=false` |
| `LLM_BASE_URL` | 百炼兼容入口 | 换服务商只改这一项与模型名 |
| `LLM_MODEL` | `qwen-plus` | 模型名以服务商文档为准 |
| `LLM_MOCK` | 样例文件为 `true` | 强制桩模式，用于离线演示与单元测试 |
| `LLM_FALLBACK_MODEL` | 空 | 主模型连续失败时的降级目标，留空不启用 |
| `CHECKPOINTER` | 样例文件为 `memory` | 改 `sqlite` 后会话状态落 `data/app.db`，可跨重启恢复 |
| `SQL_MAX_ROWS` | `1000` | 单次查询下发行数上限 |
| `SQL_MAX_RETRY` | `3` | 首次生成之外允许的修正次数 |
| `SQL_MAX_EMPTY_RETRY` | `1` | 空结果回灌的独立配额 |
| `SQL_TIMEOUT_SECONDS` | `10` | 单次执行超时 |
| `SQL_SCAN_LIMIT` | `20000000` | 扫描行数超过即中断 |
| `HISTORY_WINDOW` | `6` | 历史会话回灌给模型的最近轮数 |
| `ARCHIVED_RETENTION_DAYS` | `90` | 归档会话保留期，超过后在列表请求时惰性清理 |
| `DATA_STALENESS_DAYS` | `7` | 数据超过该天数未更新视为异常 |
| `SQL_DIALECT` | `sqlite` | 守卫与提示词共用的方言，切换到 PostgreSQL 或 MySQL 时改这一项 |

两点需要留意：

**代码默认值与 `.env` 默认值可能不同。** 例如 `CHECKPOINTER` 在代码中的默认值是 `sqlite`，而 `.env.example` 里写的是 `memory`。pydantic 的 BaseSettings 中 `.env` 的优先级高于字段默认值，因此以 `.env` 为准。核对生效值请查 `/api/health` 的 `checkpointer` 字段，不要只看代码。

**修改 `.env` 后需要重启进程。** 配置在进程启动时读取一次，`--reload` 不会因 `.env` 变化而重载。例外是设置弹窗能改的那几项，保存时会写回 `.env` 并热重载容器。

配置项也可以按改动后果分类：

| 改动后仅需重启 | 改动后需要重新造数或核对语义层 |
|---|---|
| `LLM_*` 全部、`HISTORY_WINDOW`、`CHECKPOINTER`、`SQL_*`、日志级别 | `BUSINESS_DB_PATH`、`SQL_DIALECT`（换库后必须重新登记 `tables.yaml` 与 `metrics.yaml`） |

---

## 九、测试与评测

三项检查分别针对链路连通性、单元逻辑与评测准确率。全部在 `backend` 目录下执行。

```powershell
.\.venv\Scripts\python.exe tests\smoke.py       # 端到端冒烟
.\.venv\Scripts\python.exe -m pytest tests\ -q  # 单元测试
.\.venv\Scripts\python.exe eval\run_eval.py     # 评测集
```

冒烟脚本用 TestClient 在进程内走完整链路：健康检查、语义层一致性、口径召回、建会话、三轮问答。退出码 0 表示全部通过，非 0 时逐条打印失败原因。它不依赖密钥，也不要求服务已启动。

单元测试按分层组织，覆盖守卫规则的正反例、仓储的只读约束与截断、语义层一致性校验、接口全生命周期、以及三条分层硬约束的自动核对。

评测集分十二组，每组回答一个互不替代的问题，不合并成单一数字。

| 组 | 考察内容 |
|---|---|
| `recall` | 业务说法到口径的映射准确率 |
| `pipeline` | 生成到执行的端到端可执行率 |
| `safety` | 注入语句的拦截率 |
| `datalineage` | 来源表、数据时效与时间区间一致性 |
| `baseline` | 对比基准的判定与不完整周期隔离 |
| `drilldown` | 下钻加深一层与异常标记字段完整性 |
| `robust` | 结论数字可回溯、脱敏标记零误报 |
| `alert` | 异常订阅命中、未命中、失败三态与幂等 |
| `multiturn` | 多轮改写、排序、TopN、数值过滤与槽位结构化 |
| `advanced` | 窗口函数与 CTE |
| `outofscope` | 域外问题不生成查询、归因问题不给数字 |
| `slotcoverage` | 槽位与语句的覆盖核对本身准不准 |

支持只跑单组、指定时间锚点、打印逐条观察值：

```powershell
.\.venv\Scripts\python.exe eval\run_eval.py --group recall
.\.venv\Scripts\python.exe eval\run_eval.py --today 2026-10-14
.\.venv\Scripts\python.exe eval\run_eval.py --verbose
```

不传 `--today` 时锚点取库内 `data_as_of`（与生产链路同一来源），取不到才退回评测集的 `reference_today`；改种子数据的截止日时，`eval/cases.yaml` 里各条的日期字面量要跟着同步改。

**当前基线**（2026-10-16 实测，桩实现模式，时间锚点 2026-10-14）：单元测试 548 项全部通过；评测集十二组 109 条，通过 104、跳过 5、未通过 0——`recall` 45/45、`pipeline` 9/9、`safety` 8/8、`datalineage` 5/5、`baseline` 6/6、`drilldown` 7/7、`robust` 2/2、`alert` 5/5、`multiturn` 5/5、`outofscope` 8/8、`slotcoverage` 4/4。原始报告见 `backend/eval/report-baseline-2026-10-16.json`。

`pipeline` 与 `multiturn` 在桩实现下各另有 1 条跳过，分别是闲聊分支与多轮改写，都依赖真实模型的意图判断；`advanced` 的 3 条覆盖窗口函数与 CTE，同样标记为需要真实模型。它们切换到真实模型后自动生效。桩模式下的各组只反映链路可用性，不代表模型的生成能力；真实模型下的覆盖率见 `docs/设计文档/已知限制.md`。基线的详细读法见 `backend/eval/README.md`。

评测报告的遥测部分记录查询延迟分位值、模型调用次数与 token 用量、按 `LLM_PRICE_JSON` 估算的成本，以及对比期额外执行的 SQL 次数。

前端的一致性检查与构建：

```powershell
cd C:\Text2SQL\frontend
npm run typecheck   # tsc --noEmit，不产出文件
npm run build       # 先检查类型再构建，产出 dist
```

---

## 十、日常运维

| 操作 | 方式 |
|---|---|
| 换模型或服务商 | 改 `.env` 的 `LLM_BASE_URL` 与 `LLM_MODEL` 后重启，或直接在设置弹窗中修改 |
| 从桩实现切到真实模型 | 填入密钥并把 `LLM_MOCK` 置为 `false` 后重启，或使用设置弹窗 |
| 调整安全边界 | 改 `SQL_TIMEOUT_SECONDS`、`SQL_MAX_ROWS`、`SQL_MAX_RETRY`、`SQL_SCAN_LIMIT` |
| 重建样例库 | 执行 `data/seed.py`，固定种子保证结果一致 |
| 查看模型用量 | `GET /api/stats` 返回本进程内的调用次数、token 用量与估算成本 |
| 备份 | 复制 `data/ecommerce.db` 与 `data/app.db`。业务库可由脚本重建，会话库不可 |
| 部署到反向代理 | 见 `deploy/nginx.conf` 与实训指导书附录 C |

### 异常订阅需要外部计划任务

应用进程内没有调度器。规则求值的周期由外部计划任务调用接口提供，应用只保证同一周期内重复调用是幂等的（由 `ALERT_EVAL_PERIOD_MINUTES` 量化求值时刻，默认 60 分钟）。

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/alert/check
```

Linux 下用 crontab：

```bash
# 每小时第 5 分钟求值一次
5 * * * * curl -s -X POST http://127.0.0.1:8000/api/alert/check > /dev/null
```

Windows 下用任务计划程序创建每小时执行一次的任务，操作填 `curl.exe`，参数填 `-X POST http://127.0.0.1:8000/api/alert/check`。

忘配这项时订阅会静默地不工作，页面不会报错。因此 `/api/health` 专门暴露了 `alert_last_eval_at`，长期为空即说明计划任务没有生效；连续失败次数达到阈值时，顶部状态栏会出现红色标识。

### 日志

日志格式为「时间戳 级别 模块名 | 内容」，输出到标准输出。使用 `deploy/start.sh` 启动时写入 `deploy/logs/backend.log`。处于桩实现模式时启动日志会有一条告警，提示当前 SQL 来自固定模板。

### 接口速查

完整定义与请求体格式见 <http://127.0.0.1:8000/docs>。共 32 个接口，按组分布如下。

| 组 | 接口 |
|---|---|
| 运维 | `GET /api/health`、`GET /api/stats` |
| 对话 | `POST /api/chat`（SSE 流式）、`POST /api/chat/sync`（非流式） |
| 会话 | `POST /api/session`、`GET /api/session`、`GET /api/session/{id}`、`PATCH /api/session/{id}`、`DELETE /api/session/{id}` |
| 语义层 | `GET /api/semantic`、`/metrics`、`/tables`、`/synonyms`、`/recall`、`/health` |
| 下钻 | `POST /api/drilldown` |
| 反馈 | `POST /api/feedback`、`GET /api/feedback/summary`、`GET /api/feedback/export` |
| 分享 | `POST /api/share`、`GET /api/share/{token}` |
| 告警 | `GET/POST /api/alert/rules`、`DELETE /api/alert/rules/{id}`、`POST /api/alert/check`、`GET /api/alert/events`、`POST /api/alert/events/{id}/seen` |
| 设置 | `GET/POST /api/settings` |
| 数据源 | `POST /api/datasource/test`、`POST /api/datasource/connect` |
| SQL 解释 | `POST /api/sql/explain` |

写脚本批量取数用 `/api/chat/sync`，观察节点进度用 `/api/chat` 加 `-N` 关闭缓冲。会话列表接口是单数的 `/api/session`，参数为 `keyword` 与 `include_archived`。

---

## 十一、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动报 `FileNotFoundError: 业务库不存在` | 未执行造数脚本 | 执行 `data\seed.py` |
| 启动报 `SemanticError` 后进程退出 | 语义层配置与库不一致 | 按错误清单修改 YAML 后重启 |
| 提问后一直没有响应 | 未配密钥却把 `LLM_MOCK` 设为 `false` | 补密钥，或把 `LLM_MOCK` 改回 `true` |
| 生成的 SQL 明显是固定模板 | 处于桩实现模式 | 配置密钥并把 `LLM_MOCK` 置为 `false` |
| 页面能打开但只有接口，没有界面 | `frontend/dist` 不存在 | 单独启动 Vite，或在 `frontend` 目录执行 `npm run build` |
| 修改 `.env` 后不生效 | 配置在启动时读取一次 | 重启服务 |
| 端口 8000 被占用 | 已有实例未退出 | 换端口 `--port 8001`，或用 `./deploy/start.sh status` 确认后停掉 |
| 语义层一致性返回 `consistent: false` | YAML 与库表不一致 | 按 `missing_in_semantic` / `missing_in_db` 列表修正 `tables.yaml` |
| 某个指标没有召回 | 同义词未覆盖该说法 | 补 `synonyms.yaml`，再用 `/api/semantic/recall` 验证 |
| 「最近 7 天」没有生效 | 相对时间词精确匹配 | 改用配置中的写法「近7天」 |
| 结果行数恰好是 1000 | 被守卫的行数上限截断 | 看 `truncated` 标记，改写成聚合查询 |
| 重启后追问接不住上文 | `CHECKPOINTER` 为 `memory`，上下文在进程内存中 | 改为 `sqlite`。已落库的问答记录不受影响 |
| 历史会话少了一部分问答 | `CHECKPOINTER` 为 `memory` 时图状态随进程丢失 | 同上。回合数据本身已落 `app.db` |
| 告警订阅一直没有事件 | 外部计划任务没有配置 | 参见第十节，手动调用 `/api/alert/check` 可先验证链路 |
| 分享链接打不开 | 链接已过期 | 重新生成，或在生成时不设置有效期 |
| 数字与预期不一致 | 口径与提问者预期不同 | 依次核对口径标签、`/api/semantic/recall` 的召回结果、再把 SQL 复制到 SQLite 命令行手工执行 |

---

## 十二、目录结构

```
C:\Text2SQL\
├── backend/                 FastAPI 服务、LangGraph 编排、语义层、评测与测试
│   ├── app/
│   │   ├── main.py          入口：路由挂载、CORS、静态托管、启动预热
│   │   ├── config.py        全部配置项
│   │   ├── agent/           编排层：状态、图、检查点、节点、基准、下钻、异常标记
│   │   ├── semantic/        语义层：YAML 加载与召回，纯函数
│   │   ├── llm/             模型网关：结构化输出、重试降级、桩实现、用量统计
│   │   ├── db/              数据访问：安全沙箱、仓储抽象、SQLite 实现、脱敏
│   │   ├── chart/           图表层：图型决策与图表配置构造
│   │   └── api/             接入层：对话、会话、语义层、下钻、反馈、分享、告警等
│   ├── data/                schema.sql、seed.py 与运行时数据库
│   ├── eval/                评测集与执行器
│   └── tests/               冒烟脚本与单元测试
├── frontend/                React 18 + TypeScript + Vite + Ant Design
│   └── src/                 api / hooks / components / pages / lib
├── docs/                    PPT、实训指导书、语料库、设计文档
├── deploy/                  nginx.conf 与 start.sh
└── README.md                本文件
```

`data/ecommerce.db` 与 `data/app.db` 均为运行时产物，不入版本库：前者可由 `seed.py` 重建，后者是会话历史与检查点。

改动代码时有三条分层约束由测试自动核对：`app/agent/nodes/` 下不出现数据库连接，只能调用 `app/db/repository.py` 暴露的方法；`app/semantic/` 下不出现模型调用与 SQL 执行，保持纯函数；密钥只出现在 `.env`，代码与配置文件中不出现字面量。

---

## 十三、相关文档

| 文档 | 内容 |
|---|---|
| `Text2SQL-Agent-系统架构设计.md` | 选型理由与整体设计 |
| `Text2SQL-项目结构与交付规范.md` | 目录约定与交付物构成 |
| `docs/设计文档/PRD-产品需求规格说明书.md` | 需求清单与验收标准 |
| `docs/设计文档/功能与预期效果说明.md` | 功能点与预期表现 |
| `docs/设计文档/使用流程说明.md` | 操作流程与结果解读的展开说明 |
| `docs/实训指导书/` | 七份实训指导书与三份附录 |
| `docs/语料库/` | 十八份 RAG 语料与提示词 |
| `CHANGELOG.md` | 按批次记录的改动与验收结果 |
| `backend/README.md` | 后端视角的启动、验证与排障 |
| `frontend/README.md` | 前端目录、契约与自测步骤 |
| `backend/eval/README.md` | 评测集的口径说明与基线读法 |
