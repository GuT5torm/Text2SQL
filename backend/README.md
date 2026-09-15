# Text2SQL 电商数据分析 Agent —— 后端

自然语言提问 → 语义层约束口径 → 生成 SQL → 安全沙箱校验 → 只读执行 → 自动出图与结论。
本目录包含 FastAPI 服务、LangGraph 编排链路、语义层配置、SQL 守卫与样例数据库。

面向教学演示，全部功能在单机本地运行，不依赖容器与外网服务。

## 一、环境要求

| 项         | 要求                                     |
| --------- | -------------------------------------- |
| 操作系统      | Windows 10/11、Linux、macOS 均可，本文以 Windows 为主 |
| Python    | 3.11 及以上（实测 3.13.14）                    |
| LLM 密钥    | 可选。缺省时走桩实现，链路完整但 SQL 不由模型生成            |

## 二、三步启动

下列命令均在 `backend/` 目录下执行，每行同时给出 bash 与 PowerShell 版本。

### 第 1 步：建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

若 PowerShell 报「禁止运行脚本」，先执行一次：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

依赖版本写死在 `requirements.txt`（14 个顶层包），取自本机实测通过的组合。
不建议改成浮动版本：`app/db/guard.py` 的判断逻辑依赖 sqlglot 的 AST 节点命名，版本变化会直接影响守卫规则的判定结果。

### 第 2 步：生成样例数据库

```bash
cp .env.example .env
python data/seed.py
```

```powershell
Copy-Item .env.example .env
python data/seed.py
```

`seed.py` 用固定随机种子造数，同一份代码在任何机器上生成的库完全一致，评测结果才可复现。
产出 `data/ecommerce.db`（约 3 MB），订单数据覆盖 2025-01-01 至造数当日。
库已存在时会覆盖重建，如需保留加 `--keep` 参数。

`.env` 仅在本机存在，已被排除在版本库之外。其中 `LLM_API_KEY` 留空即为桩模式。

### 第 3 步：启动服务

```bash
uvicorn app.main:app --reload --port 8000
```

```powershell
uvicorn app.main:app --reload --port 8000
```

启动时会做一次预热：加载语义层、装配容器、编译 LangGraph 图。配置有问题会在这一步直接抛出，不会等到用户提问才报错。

- 交互式接口文档：<http://127.0.0.1:8000/docs>
- 存活探针：<http://127.0.0.1:8000/api/health>

## 三、验证

```bash
python tests/smoke.py                       # 端到端冒烟，不依赖密钥，也不需要服务已启动
python -m pytest tests/ -q                  # 单元测试，122 条，约 4 秒
python eval/run_eval.py                     # 评测集，50 条，约 20 秒
curl http://127.0.0.1:8000/api/health       # PowerShell 下请用 (Invoke-WebRequest ...).Content
```

前两项回答「能不能跑」，第三项回答「跑得准不准」。`eval/run_eval.py` 产出三项互不
替代的指标：口径召回准确率、端到端可执行率、危险语句拦截率，详见 `eval/README.md`。
报告写进 `eval/report.json`，不入库。

冒烟脚本用 FastAPI 的 TestClient 在进程内走完整链路：健康检查 → 语义层一致性 → 口径召回 → 建会话 → 三轮问答。
退出码 0 表示全部通过，非 0 时逐条打印失败原因。

单元测试分五组，与分层一一对应：

| 文件                    | 覆盖对象                                                        |
| --------------------- | ----------------------------------------------------------- |
| `test_guard.py`       | SQL 守卫 R1–R7 各正反例、LIMIT 边界、CTE 与子查询别名、`metrics.yaml` 示例全量过守卫 |
| `test_repository.py`  | 只读连接拒绝写入、行数截断、R8 超时与扫描量中断、会话仓储的增删改查与主键回填             |
| `test_semantic.py`    | 三份 YAML 的一致性校验、相对时间换算的周期边界、规则召回、上下文渲染                        |
| `test_api.py`         | health、会话全生命周期、语义层调试接口、两轮问答的进度与落库、SSE 事件序列                    |
| `test_layering.py`    | 三条分层硬约束的可执行检查：节点不建连、语义层是纯函数、源码无密钥字面量                        |

跑测试需要业务库存在（未造数时依赖业务库的用例会跳过而不是失败）。应用库由夹具指向临时目录，不会写入 `data/app.db`。
接入真实模型后，依赖桩实现的链路用例会自动跳过。

`/api/health` 的典型返回：

```json
{
  "status": "ok",
  "llm_mock": true,
  "model": "qwen-plus",
  "checkpointer": "memory",
  "metrics": 7,
  "tables": 6
}
```

`llm_mock` 为 true 表示当前是桩实现。桩模式下 `generate` 节点按问题关键词选择预置 SQL，
可以验证「生成 → 校验 → 执行 → 出图 → 结论」的链路是否通畅，但不代表模型的真实生成能力。

## 四、目录结构

```
backend/
├── app/
│   ├── main.py            FastAPI 入口：路由、CORS、异常、前端静态托管
│   ├── config.py          全部配置项，从环境变量或 .env 读取
│   ├── agent/             编排层：state、graph、checkpointer、nodes/（八节点 + 三个终态）
│   ├── semantic/          语义层：三份 YAML 的加载与召回，纯函数，不调 LLM 不碰 SQL
│   ├── llm/               LLM 网关：结构化输出、重试降级、桩实现、用量统计
│   ├── db/                数据访问：guard 安全沙箱、repository 抽象、sqlite 实现、ORM 模型
│   ├── chart/             图表层：图型决策与 ECharts option 构造
│   └── api/               接入层：chat（SSE）、session、semantic 三组路由
├── data/
│   ├── schema.sql         六张表的建表语句
│   ├── seed.py            造数脚本，固定随机种子
│   ├── ecommerce.db       样例业务库（造数生成，不入版本库）
│   └── app.db             会话与回合的运行时库（自动生成，不入版本库）
├── eval/                  cases.yaml 评测集 + run_eval.py 执行器，详见 eval/README.md
└── tests/                 smoke.py 端到端冒烟 + 五个测试文件（conftest.py 提供共享夹具）
```

分层上有三条硬约束，改动时请保持：

1. `app/agent/nodes/` 下不出现数据库连接，只能调 `app/db/repository.py` 暴露的方法；
2. `app/semantic/` 下不出现 LLM 调用与 SQL 执行，保持纯函数；
3. 密钥只出现在 `.env`，代码与配置文件中不出现字面量。

三条都由 `tests/test_layering.py` 自动核对，不需要人工评审：该文件用 AST 扫描导入与字面量，违反约束即测试失败。

## 五、常用配置项

完整清单见 `.env.example`，下表是常改的几项。

| 变量               | 默认值                                                     | 说明                                              |
| ---------------- | ------------------------------------------------------- | ----------------------------------------------- |
| `LLM_API_KEY`    | 空                                                       | 留空即启用桩实现。填入后还需置 `LLM_MOCK=false` 并重启                |
| `LLM_BASE_URL`   | `https://dashscope.aliyuncs.com/compatible-mode/v1`      | OpenAI 兼容入口。换服务商只改这一项与模型名                        |
| `LLM_MODEL`      | `qwen-plus`                                             | 模型名以服务商文档为准                                     |
| `LLM_MOCK`       | `true`                                                  | 强制桩模式。用于离线演示与单元测试                               |
| `CHECKPOINTER`   | `memory`                                                | 改 `sqlite` 后图状态落 `data/app.db`，会话可跨重启恢复          |
| `SQL_MAX_ROWS`   | `1000`                                                  | 单次查询下发行数上限                                      |
| `SQL_MAX_RETRY`  | `3`                                                     | 首次生成之外允许的修正次数，合计最多四次尝试                          |
| `HISTORY_WINDOW` | `6`                                                     | 历史会话回灌给模型的最近轮数                                  |
| `BUSINESS_DB_PATH` | 默认 `backend/data/ecommerce.db`（绝对路径）                    | 注释掉时取代码内置默认值。填相对路径会随启动目录变化，Windows 下请避免         |

## 六、常见问题

| 现象                                        | 原因                        | 处理                                                    |
| ----------------------------------------- | ------------------------- | ----------------------------------------------------- |
| 启动报 `FileNotFoundError: 业务库不存在`           | 未执行造数脚本                   | `python data/seed.py`                                 |
| 日志出现「当前为桩实现模式」                            | 未配置密钥，或 `LLM_MOCK` 为 true | 填入 `LLM_API_KEY` 并置 `LLM_MOCK=false`，重启服务              |
| `semantic/health` 返回 `consistent: false`  | 语义层 YAML 与实际库表不一致         | 响应体的 `missing_in_semantic` / `missing_in_db` 会列出差异表名    |
| 端口 8000 被占用                               | 已有实例未退出                   | 换端口：`uvicorn app.main:app --port 8001`                 |
| 修改 `.env` 后不生效                            | 配置在进程启动时读取一次              | 重启服务（`--reload` 不会因 `.env` 变化自动重载）                     |
| 历史会话重启后只剩「半截」                             | `CHECKPOINTER=memory` 时图状态随进程丢失 | 改为 `sqlite`。回合数据本身已落 `app.db`，不受影响                     |

## 七、部署

反向代理与进程启停脚本见上级目录的 `deploy/`，包含 Nginx 的 SSE 缓冲配置与 `start.sh`。
本地演示只需按本文第二步、第三步操作，不需要 Nginx。
