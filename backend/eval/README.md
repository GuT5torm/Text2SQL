# 评测

评测的目的不是凑一个好看的数字，而是回答三个问题：业务说法能不能映射到正确的
口径、映射之后整条链路能不能跑出结果、模型给了危险语句时最后一道防线拦不拦得住。

三个问题对应三组互不替代的指标，合成一个总数会把性质完全不同的东西平均掉，因
此不做。

## 跑法

在 `backend` 目录下：

```bash
.venv/Scripts/python.exe eval/run_eval.py                  # 三组全跑
.venv/Scripts/python.exe eval/run_eval.py --group recall   # 只跑口径召回
.venv/Scripts/python.exe eval/run_eval.py --today 2026-09-15
.venv/Scripts/python.exe eval/run_eval.py --verbose        # 打印每条的观察值
```

`--today` 的默认值取自 `cases.yaml` 的 `meta.reference_today`。时间类的断言依赖
「今天」是谁，复现基线时两个值必须一致。

报告写进 `eval/report.json`（已 gitignore，内容可由评测集复现）。

## 三组各自的口径

| 组 | 输入 | 是否经过模型 | 指标 |
|---|---|---|---|
| `recall` | 问题 → 语义层 | 否 | 完全命中率，另拆指标 / 维度 / 时间 / 空召四个子项 |
| `pipeline` | 问题 → 整张图 | 是 | 可执行率：终态符合预期且行数达到下限 |
| `safety` | 注入语句 → 整张图 | 注入绕过决策 | 拦截率：写操作与越权未被执行成功，缺 LIMIT 的被限流改写 |

`safety` 组不是重复守卫的单测。`test_guard.py` 验的是「给定语句，守卫怎么判」，
这里验的是「模型真的吐出一条坏 SQL 时，链路会不会止住」——包含重试是否收敛、
终态是否为 `error`、会不会把异常抛给用户。

`pipeline` 组在桩实现模式下只反映链路可用性：SQL 由 `app/llm/gateway.py`
的预置模板给出，与模型生成能力无关。切换真实模型后同一组才有端到端意义。
标注了 `requires_llm` 的条目在桩实现下自动跳过，不计入分母。

## 改动三类东西之后要重跑

- 改 `app/semantic/*.yaml` 或 `loader.recall` → 影响 `recall`
- 改 `agent/nodes/`、`chart/builder.py`、`db/guard.py` → 影响 `pipeline` 与 `safety`
- 改 `eval/cases.yaml` → 影响全部，基线随之变化

## 基线的读法

当前 `recall` 未达到的条目集中在四类，均已在 CHANGELOG 中列出成因。其中「时间词
泄漏进维度匹配」一项影响面最大：维度同义词里的「月」「季度」被时间词中的同一段
字符命中，导致「上个月各区域的销售额」会额外召回 `order_month`。
