# 评测

评测的目的不是凑一个好看的数字，而是回答八个问题：业务说法能不能映射到正确的
口径、映射之后整条链路能不能跑出结果、模型给了危险语句时最后一道防线拦不拦得
住、结果的来源与时效说不说得清、有没有可对比的基准、指出异常之后能不能继续往下
钻、结论数字与时间区间能不能自证核对、异常订阅规则能不能真的跑出命中与失败。

八个问题对应八组互不替代的指标，合成一个总数会把性质完全不同的东西平均掉，因
此不做。

## 跑法

在 `backend` 目录下：

```bash
.venv/Scripts/python.exe eval/run_eval.py                  # 八组全跑
.venv/Scripts/python.exe eval/run_eval.py --group recall   # 只跑口径召回
.venv/Scripts/python.exe eval/run_eval.py --today 2026-09-15
.venv/Scripts/python.exe eval/run_eval.py --verbose        # 打印每条的观察值
```

`--today` 的默认值取自 `cases.yaml` 的 `meta.reference_today`。时间类的断言依赖
「今天」是谁，复现基线时两个值必须一致。

业务库快照也要与参考日对齐：`drilldown` 等组的断言对分布敏感（如 rank 规则
要求端值对中位数偏离 ≥20%），重新跑 seed 而不指定截止日会多生成一天数据使
断言翻转。复现基线快照用：

```bash
.venv/Scripts/python.exe data/seed.py --end-date 2026-09-15
```

报告写进 `eval/report.json`（已 gitignore，内容可由评测集复现）。顶层除分组
结果外还有 `telemetry`：查询延迟 p50/p95/avg/max、LLM 调用与 token、估算成本
（单价取 `LLM_PRICE_JSON`）、对比期额外 SQL 次数（FR-J-3）。

## 八组各自的口径

| 组 | 输入 | 是否经过模型 | 指标 |
|---|---|---|---|
| `recall` | 问题 → 语义层 | 否 | 完全命中率，另拆指标 / 维度 / 时间 / 空召四个子项 |
| `pipeline` | 问题 → 整张图 | 是 | 可执行率：终态符合预期且行数达到下限 |
| `safety` | 注入语句 → 整张图 | 注入绕过决策 | 拦截率：写操作与越权未被执行成功，缺 LIMIT 的被限流改写 |
| `datalineage` | 问题 → 整张图 | 是 | 来源表子集命中、数据时效字段齐备且日期为 ISO、时间区间一致性标记正确 |
| `baseline` | 问题 → 整张图 | 是 | 对比基准判定率：该给的给了、不该给的给了原因、不完整周期被隔离、每轮查询次数不超上限 |
| `drilldown` | 槽位 → 下钻问句 → 整张图 | 否（构造）/ 是（执行） | 下钻与异常标记判定率：只加深一层、问句可执行、findings 字段齐备、不完整周期隔离偏离类异常 |
| `robust` | 桩结论 + 结果集 / SQL | 否（纯函数） | 结论数字可回溯（FR-B-3）、时间区间一致性（FR-D-5）、脱敏标记（FR-I-3）零误报 |
| `alert` | 规则 upsert → 求值 → 复检 | 是（复用问答链路） | 异常订阅判定率：命中 / 未命中 / 失败三态正确，同周期复检幂等（FR-O-1~O-5） |

`drilldown` 组分两段：先按 `trigger` 构造问句并断言它「只在上一轮槽位上加深一
层」，再把问句丢回既有链路跑一遍。后半段的意义是「拼出来的句子真的能查到东西」——
只断言问句文本，会出现问句看着对但走完链路返回空结果的情况。

FR-L-5 的一单多退计数用例**不在本组**。构造「一个订单两笔退款」需要往业务库里
写数据，而评测集跑的是 `data/ecommerce.db` 这份演示数据，写进去就污染了。该用例
放在 `tests/test_refund_grain.py`，用临时库构造数据；断言维度声明的两种计数表达
式能在真实库上执行的部分，仍留在单测里用 `business_repo` 夹具跑。

`baseline` 组的时间断言只判结构与状态（粒度、`partial_period`、文案），不写绝对
日期：链路里的「今天」取系统日期，写死月份会让整组在下个月变红。

`safety` 组不是重复守卫的单测。`test_guard.py` 验的是「给定语句，守卫怎么判」，
这里验的是「模型真的吐出一条坏 SQL 时，链路会不会止住」——包含重试是否收敛、
终态是否为 `error`、会不会把异常抛给用户。

`pipeline` 组在桩实现模式下只反映链路可用性：SQL 由 `app/llm/gateway.py`
的预置模板给出，与模型生成能力无关。切换真实模型后同一组才有端到端意义。
标注了 `requires_llm` 的条目在桩实现下自动跳过，不计入分母。当前 `flow-p006`
~ `flow-p008` 三条未通过是第 0 步校准时刻意保留的断言（桩模板不产出日粒度与
users JOIN），不是回归；基线为 6/9。

`robust` 组只验纯函数与落标，不依赖模型生成：桩模式结论是固定文案、本身不含
业务数字，因此本组只固化「零误报」；脱敏端到端（users 表真实查询）桩模板不
覆盖，由 `tests/test_masking.py` 直接对 `mask_result` 断言。

`alert` 组固定求值时刻（2026-09-15 10:00）并在 finally 中删除自建规则，
不依赖外部计划任务；同周期复检必须返回同一条事件记录（幂等）。

## 改动三类东西之后要重跑

- 改 `app/semantic/*.yaml` 或 `loader.recall` → 影响 `recall`
- 改 `agent/nodes/`、`chart/builder.py`、`db/guard.py` → 影响 `pipeline` 与 `safety`
- 改 `agent/baselines.py`、`semantic/baseline_rules.yaml` → 影响 `baseline`
- 改 `agent/drilldown.py`、`agent/findings.py`、`semantic/finding_rules.yaml` →
  影响 `drilldown`；改 `tables.yaml` 的维度顺序也会改变下钻的加深目标
- 改 `agent/consistency.py`、`db/masking.py`、`agent/alert_eval.py`、
  `api/alerts.py` → 影响 `robust` / `alert`
- 改 `eval/cases.yaml` → 影响全部，基线随之变化

## 基线的读法

当前 `recall` 未达到的条目集中在四类，均已在 CHANGELOG 中列出成因。其中「时间词
泄漏进维度匹配」一项影响面最大：维度同义词里的「月」「季度」被时间词中的同一段
字符命中，导致「上个月各区域的销售额」会额外召回 `order_month`。
