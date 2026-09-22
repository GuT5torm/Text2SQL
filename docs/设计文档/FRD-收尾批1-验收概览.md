# FRD 收尾批 1 验收概览（前端消费与导出收尾）

对照文档：`docs/设计文档/FRD-完成度核查-2026-09-21.md` 第五节的三处共性问题。
范围：**FR-C-1、FR-D-2、FR-B-1、FR-N-2** 四条收尾 + 一个实测中发现的缺陷修复。
全部为前端消费与导出链路，不改数据结构、不加接口。本机实测时间：2026-09-21，桩实现模式。

## 一、完成度变化

| 状态 | 收尾前 | 收尾后 |
|---|---|---|
| 完全交付 | 54/68（79.4%） | **58/68（85.3%）** |
| 部分交付 | 8 | 5（FR-A-1、FR-A-2、FR-E-4、FR-H-1、FR-H-5） |
| 未交付 | 5 | 4（FR-F-1、FR-F-2、FR-H-3、FR-H-4） |

本批转完全交付的四条：FR-C-1（写法说明）、FR-D-2（数据截至常驻）、FR-B-1（CSV 截断标注）、FR-N-2（PPTX 图表嵌入）。

## 二、逐条改动

### FR-C-1 写法说明落位到 SQL 面板

后端第 2 步就已返回 `sql_reasoning`，此前前端只有类型声明、零消费。本批：

- `SqlPanel.tsx` 新增 `sqlReasoning` / `fixReasoning` 两个 prop，渲染「写法说明」折叠项；`fix_reasoning` 非空时附加「最后一次修正的理由」小节
- 两个调用点都接线：`ResultPane`（卡片主区）与 `TechDetails`（技术细节），`AnswerCard` 透传
- 字段为空时（历史回看、桩模式无理由）整项不渲染，不占版面

### FR-D-2 数据截至常驻与超期高亮

后端早已算好 `data_freshness`，前端 `is_stale` 无消费点、界面上看不到截止日期。本批：

- `MetricBadge.tsx` 新增 `FreshnessTag`：取 `orders_cutoff` 与 `refunds_cutoff` 中**较早者**（FRD 要求取早者），渲染「数据截至 YYYY-MM-DD（N 天前）」；tooltip 说明两表不同步与取早者的依据
- `is_stale=true` 时标签转红色高亮，tooltip 附阈值与 note
- `AnswerCard` 渲染条件放宽：即使本轮未命中已登记指标，只要有 `data_freshness` 也渲染该行（FRD 要求「常驻」）
- `daysSince` 按本地日期零点计算，避免「0 天前」实为 23 小时的歧义

### FR-B-1 CSV 导出的截断标注

FRD 第 8 节要求「截断的结果在文件名与文件内标注」，原 `buildCsv` 无截断概念。本批：

- `buildCsv(columns, rows, options)` 增加 `truncated / totalRowCount / limit`：截断时文件内首行写 `# 已截断：本文件仅含前 N 行，实际命中 M 行（上限 L 行）`
- 新增 `csvFilename(prefix, truncated)`：截断时文件名追加 `_已截断`
- `ResultTable` 与 `ExportMenu` 两个导出入口都接线；`ResultTable` 的截断标记改取服务端 `truncated` 字段（原实现比较 `rows.length > 1000`，而落库时已被 `PERSIST_ROWS` 截过一轮，该比较恒为假，属隐性失效，一并修正）

### FR-N-2 PPTX 图表以图片嵌入

第 6 批交付时 `getChartImage` 传 `async () => null`，图表区空白。本批：

- 新增 `lib/chartImage.ts::renderChartImage(option)`：离屏 DOM 上 `echarts.init`（显式宽高，避免 `display:none` 拿到 0 尺寸）、关动画渲染、`getDataURL` 取 2 倍图、`dispose` 并移除容器；单图失败返回 null，不中断整份导出
- `HistoryPage` 的 `getChartImage` 接入该函数，与界面同一份 `option`，导出图与屏幕图同源

### 附带修复：数据时效文案方向写反

实测中发现 `_freshness.py` 的 note 与数据矛盾：订单表截止 2026-09-15、退款表 2026-09-20，却输出「退款表 比 订单表 截止早 5 天」。

- 根因：`age_days` 是距今天数，**数值越大代表截止越早**，比较方向与日期大小相反，代码按日期直觉写成了 `orders_age < refunds_age`
- 修复为 `>`，实测 note 变为「订单表 比 退款表 截止早 5 天。」，与数据一致；pytest 410 条无回归

## 三、验证证据

| 项 | 结果 |
|---|---|
| `npx tsc --noEmit` | 0 error |
| `npx vite build`（临时目录，已清理） | 通过，34.35s |
| `pytest -q`（含 `_freshness` 修复） | **410 passed** |
| API 实测（桩模式 + 真实业务库） | 提问「上个月各区域的成交总额」返回 `sql_reasoning="桩实现按问题关键词选择预置语句，仅供链路冒烟"`、`data_freshness={orders_cutoff: 2026-09-15, refunds_cutoff: 2026-09-20, is_stale: false, threshold_days: 7}` |
| 浏览器实测（Vite dev + 真实后端） | 口径标签行出现「数据截至 2026-09-15（6 天前）」；「写法说明」折叠项展开正文正确；既有的「时间区间不一致」橙色 Tag 不受影响 |
| `renderChartImage` 真实浏览器执行 | 产出 84,506 字节 PNG dataURL |
| 端到端 PPTX 构建（历史会话真实数据） | `buildSessionPptx` 产 137,420 字节，**内嵌 PNG 数 = 1**（改动前恒为 0）；页面点「导出 PPTX」提示「已导出 1 页 PPTX」 |
| 截图 | `docs/设计文档/assets/verify-frc1-frd2.png` |
| 冒烟数据 | 随测随清：删除本次及遗留的空白会话 13 条 |

## 四、未覆盖与已知限制

- **FR-B-1 的截断场景未实测**：触发 `truncated=true` 需要 >1000 行的结果集，桩模板只产聚合查询，现有数据集构造不出。标注逻辑由类型检查与代码评审覆盖，切真实模型后出现明细大结果集时应补一次人工确认。
- **FR-D-2 的超期高亮未实测**：当前快照 `is_stale=false`（订单表 6 天 < 阈值 7 天）。高亮分支的渲染路径与正常分支相同，仅颜色不同；如需实测可把 `DATA_STALENESS_DAYS` 临时调小。
- **FR-C-1 / FR-D-2 的历史回看降级**：这两类字段不落库（不在 `turns` 迁移列清单内），历史轮次回看时 `sql_reasoning` 为空、「数据截至」不显示，属设计内的降级而非缺陷；若要回看也带，需要再加迁移列，本批刻意不做。
- **完成度仍差 10 条到全绿**：FR-F-1、FR-F-2、FR-H-3、FR-H-4 四条未交付，FR-E-4、FR-H-1、FR-H-5 三条半成品，FR-A-1、FR-A-2 需真实模型下的跨轮用例。
