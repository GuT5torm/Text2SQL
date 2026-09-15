# 实训 4 — AI 共创：数据层与语义层构建

## 一、实训目标

1. 编写造数脚本，生成固定随机种子下可复现的样例电商库
2. 编写三份语义层配置：表结构元数据、指标口径库、同义词与相对时间词映射
3. 实现配置加载、一致性校验与规则召回，理解为什么这里不做向量检索
4. 用一致性校验接口验证语义层登记的表与库内实际表完全一致

## 二、前置条件

| 项 | 要求 |
|---|---|
| 环境 | Python 3.12+，已安装 `sqlalchemy`、`pyyaml`、`pydantic-settings` |
| 目录 | `backend/` 下已有 `data/` 与 `app/semantic/` |
| 前置实训 | 实训 3 已完成，理解了任务依赖顺序 |

## 三、知识点

| 知识点 | 说明 |
|---|---|
| 可复现造数 | 固定随机种子，同一环境重复生成结果一致 |
| 表结构元数据 | 字段名、类型、枚举值、口径说明，供 Prompt 与守卫共用 |
| 指标口径 | 业务词的聚合表达式与必带条件，是准确率的来源 |
| 相对时间词 | 「上个月」这类词由代码换算成区间，不让模型自行解释 |
| 规则召回 | 短语命中打分加字符二元组重叠度兜底，不引入向量库 |

## 四、操作步骤

### 步骤 1：编写建表语句

在 `data/schema.sql` 中写六张表：`users`、`categories`、`products`、`orders`、`order_items`、`refunds`。同时建十个索引，覆盖外交接字段与时间字段。

两条约定要写进注释：时间字段统一存 ISO8601 字符串；`orders.status` 的枚举值为六个，分别是 `pending`、`paid`、`shipped`、`completed`、`cancelled`、`refunded`。六个值都要写进 `tables.yaml` 的枚举说明，少一个就会在退款率与成交总额的口径上出错。

**预期输出**：执行后库内出现六张表。

### 步骤 2：编写造数脚本

`data/seed.py` 中固定随机种子，设置用户数 2000、商品数 500、订单数 12000。让数据带业务规律：促销月份有峰值、月度有增长、周末系数更高、商品热度呈长尾分布。

业务规律比数据量重要。全随机的数据看不出趋势，演示时图表上没有可解读的形态。

**预期输出**：运行后六张表的数据量在预期量级，库文件约 3 MB。

```bash
cd backend
python data/seed.py
```

### 步骤 3：编写表结构元数据

`semantic/tables.yaml` 中逐表列出字段，含类型、中文名、枚举值说明与备注。末尾定义九个维度及其来源表达式。

字段名必须与库内实际一致。写错一个字母，后面生成的所有 SQL 都会被守卫拦下。

**预期输出**：六张表的字段清单与九个维度定义。

### 步骤 4：编写指标口径库

`semantic/metrics.yaml` 中定义七个指标，每个含显示名、口径表达式、依赖表、必带条件、可用维度、边界说明与示例问法。

边界说明写清「不含什么」。例如成交总额不含已取消订单、不扣退款，退款率以订单数为分母而非金额。

**预期输出**：七个指标定义。

### 步骤 5：编写同义词配置

`semantic/synonyms.yaml` 分三组：业务词同义词、枚举值口语说法、相对时间词。

相对时间词是重点。「上个月」「上季度」「近三个月」「近半年」这类词由代码换算成左闭右开区间后写进 Prompt，并明确告诉模型不要重新解释。注意配置里没有「今年以来」这个说法，对应的词条是「今年」与「本年度」，换算逻辑用的是自然年而非滚动区间。

**预期输出**：三组映射，共五十条以上。

### 步骤 6：实现加载与召回

`semantic/loader.py` 做三件事：加载三份 YAML 并做一致性校验、按规则召回相关子集、把召回结果渲染成 Prompt 段落。

一致性校验要一次性列出全部不一致项，而不是遇到第一个就抛异常。改配置时通常不止一处错，逐个报会让人改一晚。

**预期输出**：加载、召回、渲染三个函数可用。

### 步骤 7：验证一致性

本阶段还没有接入层，用命令行直接比对语义层登记的表与库内实际表：

```bash
cd backend
python - <<'PY'
import sqlite3, yaml

tables = yaml.safe_load(open("app/semantic/tables.yaml", encoding="utf-8"))["tables"]
semantic = {t["name"] for t in tables}
conn = sqlite3.connect("data/ecommerce.db")
db = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

print("missing_in_semantic:", sorted(db - semantic))
print("missing_in_db:", sorted(semantic - db))
print("consistent:", semantic == db)
PY
```

加载器 `load_catalog()` 在加载时执行同样的一致性校验，不一致时抛 `SemanticError` 并一次性列出全部问题，不会遇到第一个就退出。

接入层完成后（实训 6），同样的信息由 `GET /api/semantic/health` 以 JSON 返回，字段含义与上面一致：`consistent` 为 `true`，`missing_in_semantic` 与 `missing_in_db` 均为空列表。

**预期输出**：`consistent: True`，两个缺失列表为空。

### 步骤 8：验证召回

直接调用召回函数，检查典型问题命中的指标与维度：

```bash
cd backend
python - <<'PY'
from app.semantic.loader import load_catalog, recall

hit = recall("上个季度华东区各品类的成交总额", load_catalog())
print("metrics:", [m["name"] for m in hit.metrics])
print("dimensions:", [d["name"] for d in hit.dimensions])
print("time_range:", hit.time_range)
print("matched_terms:", hit.matched_terms)
PY
```

接入层完成后，同样的信息由 `GET /api/semantic/recall?q=...` 返回。

**预期输出**：命中 `gmv` 指标，命中 `order_quarter` 与 `category_name` 维度，时间范围换算为上一个季度的起止日期。

## 五、AI 共创要点

**怎么问**：让 AI 生成造数脚本时，把「要看出业务规律」这个要求明确写出来。只说「生成一千条订单数据」，得到的是均匀随机数据，图表上是一条直线，演示时没有可讲的内容。

一个可用的提问结构：

```
任务：写一个 SQLite 造数脚本
要求：
1. 固定随机种子，保证可复现
2. 数据要带业务规律：<促销月峰值 / 月度增长 / 周末系数 / 商品长尾>
3. 时间字段存 ISO8601 字符串
4. 输出每张表的行数
```

**常见偏差**：

| 偏差 | 表现 | 处理 |
|---|---|---|
| 数据全随机 | 图表上没有趋势与头部 | 加入促销月、增长、长尾三类规律 |
| 字段凭常识命名 | 写出 `orders.amount`、`products.price` | 以 `schema.sql` 为准，逐字核对 |
| 枚举值缺失 | `status` 只有 `paid` 与 `cancelled` | 补齐六个状态值：`pending`、`paid`、`shipped`、`completed`、`cancelled`、`refunded` |
| 口径写成自然语言 | 「销售额大概就是金额加起来」 | 改成可直接嵌入 SQL 的表达式 |
| 召回带噪声 | 问区域销售额时把用户表也召回 | 去掉按外键链的自动补齐 |

**审查动作**：改完 YAML 后跑一致性校验接口。这个接口的价值就在于把「配置与库不一致」这类错误暴露在运行之前。

## 六、产出要求

| 文件 | 位置 |
|---|---|
| `schema.sql` | `backend/data/` |
| `seed.py` | `backend/data/` |
| `ecommerce.db` | `backend/data/`（由脚本生成） |
| `tables.yaml` | `backend/app/semantic/` |
| `metrics.yaml` | `backend/app/semantic/` |
| `synonyms.yaml` | `backend/app/semantic/` |
| `loader.py` | `backend/app/semantic/` |

## 七、检查点

- [ ] 固定种子重复运行，生成的行数完全一致
- [ ] 数据呈现促销月峰值与月度增长，图表上有可读形态
- [ ] 六张表的字段名与库内实际完全一致
- [ ] 七个指标都有口径表达式与边界说明
- [ ] 相对时间词换算为左闭右开区间
- [ ] 一致性校验返回 `consistent: true`，两个缺失列表为空
- [ ] 典型问题能召回正确指标与维度，且不引入无关表
- [ ] `orders.status` 六个枚举值在 `tables.yaml` 中登记完整

## 八、常见问题

**问题：一致性校验返回 `consistent: false`，缺失项里出现 `sqlite_stat1`。**
原因：库内执行过 `ANALYZE`，产生了 SQLite 内部统计表。解决：在列实际表时排除 `sqlite_` 前缀的内部表。

**问题：召回把一个不相关的表也带进来了。**
原因：按外键关系做了自动补齐，用户表因订单表的外键被引入。解决：去掉自动补齐，只保留直接命中的表。

**问题：相对时间算出来的区间差一个月。**
原因：把「上个月」当成了最近 30 天。解决：按自然月换算，区间取左闭右开，起止日期用代码计算而非交给模型。

**问题：改了 YAML 但服务里读到的还是旧内容。**
原因：配置在启动时加载并缓存。解决：重启服务，或在开发模式下给加载函数加缓存清除入口。
