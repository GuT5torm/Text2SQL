# 前端（React 18 + TypeScript + Vite）

对话式界面：左侧提问与结论，右侧图表、SQL 与查询明细。所有业务判断都在后端，
前端只负责展示、字段校验与交互状态。

## 目录

```
src/
├── api/         与后端的契约层
│   ├── types.ts     与 summarize_result / session 接口逐字段对齐的类型
│   ├── client.ts    fetch 封装，统一抛出 ApiError（带后端 detail 文案）
│   ├── chat.ts      SSE 解析与流读取
│   └── session.ts   会话增删改查
├── hooks/
│   └── useChatStream.ts   一轮问答的状态机：阶段追加、结果落地、中断
├── components/
│   ├── ChatPanel        消息流 + 输入框（Enter 发送，Shift+Enter 换行）
│   ├── AnswerCard       一轮回答卡片，四类终态共用
│   ├── StageIndicator   节点进度，完成后收起为一行
│   ├── ChartPanel       ECharts 容器，字段校验失败退回表格
│   ├── SqlPanel         执行的 SQL 与原始 SQL 折叠展示
│   ├── ResultTable      结果集表格，超 50 行分页
│   ├── MetricBadge      本轮口径标签
│   ├── ResultPane       右侧结果区，展示当前聚焦轮次
│   └── SplitPane        可拖拽分栏，窄屏自动改为上下堆叠
└── pages/
    ├── ChatPage         主页面
    └── HistoryPage      历史会话（搜索 / 筛选 / 查看 / 归档 / 删除）
```

## 运行

```bash
npm install
npm run dev        # http://127.0.0.1:5173，/api 代理到 8000
```

后端需要同时启动：

```bash
cd ../backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

`.npmrc` 把 registry 指向 npmmirror。如果在能直连 npmjs 的网络下，删掉这个文件即可。

本机 install 若报 npm 缓存目录无写权限，改指到项目内目录，并在 `.gitignore` 中排除：

```bash
npm install --cache .npm-cache
```

## 校验与构建

```bash
npm run typecheck   # tsc --noEmit，不产出文件
npm run build       # 先 typecheck 再 vite build，产出 frontend/dist
```

产出的三个 chunk 由 `vite.config.ts` 的 manualChunks 固定拆分（echarts / antd / react），
业务代码改动不会让这两个大包的浏览器缓存失效。

## 自测

后端启动后按下面三步走一遍，可确认前后端契约没有跑偏：

```bash
# 1. 健康检查：llm_mock 为 true 说明后端在桩模式下运行
curl -s http://127.0.0.1:8000/api/health

# 2. 建会话并发一轮提问，应依次看到 7 条 stage、1 条 result、1 条 done
SID=$(curl -s -X POST http://127.0.0.1:8000/api/session \
  -H "Content-Type: application/json" -d '{"title":"冒烟"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -N -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SID\",\"question\":\"上个月各品类销售额排名\"}"

# 3. 回看： turn 的 metrics / result / chart 三个 JSON 串应能原样反序列化
curl -s "http://127.0.0.1:8000/api/session/$SID"
```

反问、闲聊、失败三个终态在本地桩模式下走不到（桩实现固定返回 query 意图），
需要临时替换 `Gateway.complete_json` 的返回值才能复现；四个终态的载荷字段已逐一核对，
前端按 `status` 分支渲染的逻辑见 `components/AnswerCard.tsx`。

## 与后端的契约

| 后端字段 | 前端用途 |
|---|---|
| `stages[]` | 阶段指示。作用域为单轮，每轮重新累积 |
| `status` | 决定渲染哪类终态：`ok` / `clarify` / `chitchat` / `error` |
| `answer` / `insight` / `highlights` | 结论卡片 |
| `metric_used` / `caveats` / `time_range` | 口径标签 |
| `chart.chart_type` / `chart.option` | 图表。图型由后端决定，前端不重新决策 |
| `sql` / `original_sql` | SQL 面板，两者不同说明守卫改写过 |
| `columns` / `rows` / `row_count` / `truncated` | 结果表格 |
| `guard_warnings` | 拦截提示条 |
| `retry_count` | 标注修正次数 |

前端不做口径推断，也不改列名：界面上的每一处文本都直接来自后端返回，
避免出现「界面写着某口径但实际 SQL 并非如此」。

## 已知约束

- SSE 用 `fetch` 读流而非 `EventSource`：`EventSource` 不支持 POST，而问题要走请求体。
- 路由用 `HashRouter`：静态托管（含 FastAPI 挂载 dist）不做 URL 重写也能刷新不 404。
- 图表字段校验是兜底，真正的选择与校验在后端 `app/chart/builder.py`。
- 未引入虚拟滚动，宽表以横向滚动 + 分页呈现；单次下发上限由后端守卫的 LIMIT 约束。
- 结果 >50 行才分页；明细超过 200 行的部分不落库，历史回看只到 200 行。
