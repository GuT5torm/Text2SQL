"""FR-M-1 / FR-M-2 / FR-M-3 对比基准计算。

按 FRD 4.12 设计说明：
  - 实现位置在 summarize 之前，不是之后。结论生成需要基准值作为输入，
    先算基准再交给模型写「较上月下降 8%」这类判断才不会退化为编造。
  - 环比取相邻等长周期，同比取去年同期。
  - 数据缺失或不完整时不输出该基准并说明原因，不是留空（不留空 = 误导）。
  - 不完整周期：周期的自然结束日超出数据截止日期。

对比期查询复用同一套 Repository（同一守卫、同一超时、同一行数上限），
因此同一 SQL 可能在主查询之外被多跑两次。设计上 contrast 查询只取聚合
值（移除 GROUP BY），不需要返回明细。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.db.repository import QueryError, QueryRepository, QueryTimeout
from app.observability import record_comparison_query
from app.semantic.loader import GRANULARITY_ORDER, Catalog

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 粒度判定
#
# 按时间窗口跨度自适配：日 / 周 / 月 / 年。
# 选取原则是「业务可解释」：同样跨度的窗口要么按月要么按周，
# 不会出现「跨度 11 天按月环比」这种口径不通顺的情况。
# ----------------------------------------------------------------------

DAY_THRESHOLD_DAYS = 2
WEEK_THRESHOLD_DAYS = 14
MONTH_THRESHOLD_DAYS = 90


def _infer_granularity(span_days: int) -> str:
    if span_days <= DAY_THRESHOLD_DAYS:
        return "day"
    if span_days <= WEEK_THRESHOLD_DAYS:
        return "week"
    if span_days <= MONTH_THRESHOLD_DAYS:
        return "month"
    return "year"


# ----------------------------------------------------------------------
# 时间谓词改写
#
# 用户的 SQL 可能形如：
#   WHERE created_at >= '2026-08-01' AND created_at < '2026-09-01'
# 或者：
#   WHERE orders.created_at BETWEEN '2026-08-01' AND '2026-09-01'
# 改写的目标：把时间字面量替换成对比期等价的字面量，同时去掉 GROUP BY
# 以便拿到聚合标量。
#
# 识别不出的 SQL（自定义函数、嵌套 CASE 等）返回 None，调用方把这条
# 记进 missing_reasons，让前端提示「对比期数据未附带」。
# ----------------------------------------------------------------------

# 时间字段候选。维度层与事实层都有 created_at；退款是 refunded_at；
# 支付是 paid_at。扩展新字段时在这里追加
TIME_COLUMN_NAMES = {"created_at", "refunded_at", "paid_at"}

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_date_literal(node: exp.Expression) -> bool:
    """判定节点是否是字符串日期字面量。SQLite 比较中常表现为 CAST 或字符串。"""
    if isinstance(node, exp.Literal):
        text = node.this
        return isinstance(text, str) and bool(_DATE_PATTERN.match(text[:10]))
    return False


def _date_from_node(node: exp.Expression) -> dt.date | None:
    if isinstance(node, exp.Literal):
        text = node.this
        if isinstance(text, str):
            try:
                return dt.date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _rewrite_time_predicates(sql: str, new_start: dt.date, new_end: dt.date) -> str | None:
    """遍历 WHERE 中形如 `<time_col> <op> '<date>'` 的比较，把日期字面量替换。

    替换规则：起点比较（>=, >, BETWEEN 起点）→ new_start；终点比较
    （<, <=, BETWEEN 终点）→ new_end。同列多条件只替换一次；跨列情况按列
    命中替换（一个语句里同时出现 created_at 与 refunded_at 是合法形态）。

    改写不涉及 SELECT / GROUP / ORDER 等其它子句，避免破坏用户原意。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except sqlglot.errors.ParseError:
        return None

    modified = False
    for comparison in list(tree.find_all(exp.Binary)):
        # sqlglot 不在 Binary 上存 operator 字段，靠 isinstance 判定
        left, right = comparison.left, comparison.right
        col_node: exp.Column | None = None
        other_node: exp.Expression | None = None
        op_kind: type[exp.Expression] | None = None
        if (
            isinstance(left, exp.Column)
            and left.name in TIME_COLUMN_NAMES
            and isinstance(right, exp.Literal)
        ):
            col_node, other_node, op_kind = left, right, type(comparison)
        elif (
            isinstance(right, exp.Column)
            and right.name in TIME_COLUMN_NAMES
            and isinstance(left, exp.Literal)
        ):
            col_node, other_node, op_kind = right, left, type(comparison)

        if col_node is None or other_node is None or op_kind is None:
            continue

        # 比较类型映射到字面量语义
        if op_kind is exp.GTE:
            new_value, new_op = new_start, ">="
            new_node = sqlglot.parse_one(
                f"{col_node.sql(dialect='sqlite')} >= '{new_value.isoformat()}'",
                dialect="sqlite",
            )
            # 在原 parent 中替换 comparison 节点
            parent = comparison.parent
            if parent is None:
                return None
            parent.set(comparison.arg_key, new_node)
            modified = True
            continue
        if op_kind is exp.GT:
            new_value = new_start
            new_node = sqlglot.parse_one(
                f"{col_node.sql(dialect='sqlite')} > '{new_value.isoformat()}'",
                dialect="sqlite",
            )
            parent = comparison.parent
            if parent is None:
                return None
            parent.set(comparison.arg_key, new_node)
            modified = True
            continue
        if op_kind is exp.LT:
            new_value = new_end
            new_node = sqlglot.parse_one(
                f"{col_node.sql(dialect='sqlite')} < '{new_value.isoformat()}'",
                dialect="sqlite",
            )
            parent = comparison.parent
            if parent is None:
                return None
            parent.set(comparison.arg_key, new_node)
            modified = True
            continue
        if op_kind is exp.LTE:
            new_value = new_end
            new_node = sqlglot.parse_one(
                f"{col_node.sql(dialect='sqlite')} <= '{new_value.isoformat()}'",
                dialect="sqlite",
            )
            parent = comparison.parent
            if parent is None:
                return None
            parent.set(comparison.arg_key, new_node)
            modified = True
            continue
        if op_kind is exp.Between:
            # BETWEEN min AND max；整段重写为两个比较 + AND
            new_pred = (
                f"{col_node.sql(dialect='sqlite')} >= '{new_start.isoformat()}'"
                f" AND {col_node.sql(dialect='sqlite')} < '{new_end.isoformat()}'"
            )
            new_node = sqlglot.parse_one(new_pred, dialect="sqlite")
            parent = comparison.parent
            if parent is None:
                return None
            parent.set(comparison.arg_key, new_node)
            modified = True
            continue
        # 其它比较类型不识别，保持原样
        continue

    if not modified:
        return None

    # 去 GROUP BY：拿到聚合标量
    group = tree.args.get("group")
    if group is not None:
        tree.set("group", None)

    return tree.sql(dialect="sqlite")


_AGG_TYPES = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)


def _contains_aggregate(node: exp.Expression) -> bool:
    """表达式树里是否出现聚合函数。

    不写白名单包裹函数（ROUND/ABS/...）而是向下遍历，原因是 sqlglot 对不同
    方言的解析节点类型不一致：sqlite 方言把 ROUND(x, 2) 解析成 exp.Round
    而非 exp.Anonymous，按类型名判断会漏掉。逐层递归不受节点类型影响。
    """
    if isinstance(node, _AGG_TYPES):
        return True
    for child in node.iter_expressions():
        if _contains_aggregate(child):
            return True
    return False


def _has_aggregate_select(sql: str) -> bool:
    """SELECT 列表里是否存在聚合函数。

    基准值必须是聚合结果：直接从明细行取一列，数字会随 ORDER BY 与 LIMIT
    变化，与对比期不可比。识别不出聚合时整组基准不给，而不是凑一个数。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except sqlglot.errors.ParseError:
        return False
    return any(_contains_aggregate(expr) for expr in tree.expressions)


def _find_aggregate_alias(sql: str) -> str | None:
    """找到 SELECT 里含聚合函数的别名列，用于按名取值。

    别名不是必需的（存在无别名的聚合写法），因此取不到时返回 None，
    调用方退回「第一个数值列」而不是判定为无法聚合。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except sqlglot.errors.ParseError:
        return None

    for expr in tree.expressions:
        if isinstance(expr, exp.Alias) and _contains_aggregate(expr):
            return expr.alias
    return None


def _qualify_time_column(tree: exp.Expression, column: str = "created_at") -> str:
    """给时间列补上主表别名。

    直接写 created_at 在多表语句里会撞上「ambiguous column name」——orders
    与 refunds 都有该字段，SQL 里只要 LEFT JOIN refunds 就执行失败，而失败
    表现为基准整体缺失，排查时看不到任何列名冲突的线索。取 FROM 的第一个
    表（含 JOIN 时的驱动表）作为限定符；没有表或没别名时退回裸列名。
    """
    # sqlglot 的 Select 用 from_ 存 FROM 子句（同一棵树里 group 却不带下划线），
    # 按字面写 args["from"] 会静默拿到 None，退化成裸列名。这里两种取法都试。
    from_clause = tree.args.get("from_") or tree.args.get("from") or tree.find(exp.From)
    if from_clause is not None:
        for table in from_clause.find_all(exp.Table):
            name = table.alias_or_name
            if name:
                return f"{name}.{column}"
    return column


def _inject_time_predicate_if_missing(sql: str, time_range: dict | None) -> str:
    """SQL 中完全没有时间谓词但语义层给出了 time_range 时，补一条 created_at 谓词。

    桩模式下 generate 节点只返回模板语句，不带时间过滤；本步骤让基准计算仍能
    在桩模式下产出有意义数字，避免「数据明明有时段范围，基准却取不到」这种
    演示级的死角。真实模型下 SQL 通常已经带时间谓词，本函数是幂等的——
    检测到已有谓词就不再注入。
    """
    if not time_range:
        return sql
    try:
        start = str(time_range.get("start") or "")[:10]
        end = str(time_range.get("end") or "")[:10]
    except (TypeError, ValueError):
        return sql
    if not start or not end:
        return sql
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except sqlglot.errors.ParseError:
        return sql

    # 已存在任一时间谓词就跳过
    for col in tree.find_all(exp.Column):
        if col.name in TIME_COLUMN_NAMES:
            return sql

    column = _qualify_time_column(tree)
    new_pred_sql = (
        f"{column} >= '{start}' AND {column} < '{end}'"
    )
    new_pred = sqlglot.parse_one(new_pred_sql, dialect="sqlite")
    where = tree.args.get("where")
    if where is None:
        # args["where"] 必须是 exp.Where 节点。直接塞条件表达式时 sqlglot 不报错，
        # 生成 SQL 却会把条件并进 JOIN 的 ON 子句（表现为 `o.idcreated_at >= ...`
        # 这种粘连），执行阶段才以「没有这一列」失败，定位成本很高。
        tree.set("where", exp.Where(this=new_pred))
    else:
        # 已有 WHERE，拼接 AND。把新谓词附加到 AND 树里，而不是外层包 Paren
        existing = where.this if isinstance(where, exp.Where) else where
        merged = exp.and_(existing.copy(), new_pred)
        tree.set("where", exp.Where(this=merged))
    return tree.sql(dialect="sqlite")


def _aggregate_to_scalar_sql(sql: str) -> str | None:
    """把带 GROUP BY 的查询包装为标量。

    思路：找到 SELECT 里出现的聚合列名 → 外层 SELECT SUM(列) FROM (内层)。
    不识别时返回 None，调用方按缺失处理。
    """
    alias = _find_aggregate_alias(sql)
    if not alias:
        return None
    # 校验 SQL 结构合法再包装
    try:
        sqlglot.parse_one(sql, dialect="sqlite")
    except sqlglot.errors.ParseError:
        return None
    return f"SELECT COALESCE(SUM({alias}), 0) AS v FROM ({sql})"


# ----------------------------------------------------------------------
# 对比窗口生成
# ----------------------------------------------------------------------

def _month_floor(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _month_ceil(d: dt.date) -> dt.date:
    """下个月 1 号（左闭右开语义）。"""
    if d.month == 12:
        return dt.date(d.year + 1, 1, 1)
    return dt.date(d.year, d.month + 1, 1)


def _prior_window(start: dt.date, end: dt.date, granularity: str) -> tuple[dt.date, dt.date]:
    """当前周期之前的等长周期（mom 用）。"""
    if granularity == "month":
        # start = 当前月1号，则 mom = 上个月
        if start.month == 1:
            mom_start = dt.date(start.year - 1, 12, 1)
        else:
            mom_start = dt.date(start.year, start.month - 1, 1)
        mom_end = _month_ceil(mom_start)
        return mom_start, mom_end
    # 其它粒度：起点平移 span 天
    span = (end - start).days
    return start - dt.timedelta(days=span), start


def _shift_years(d: dt.date, years: int = -1) -> dt.date:
    """平移整年。2-29 在非闰年回落为 2-28，而不是抛 ValueError。

    闰日只在个别年份出现，但一旦出现就是整条链路抛异常。回落规则与主流
    报表口径一致（2-29 → 2-28），不做 3-1 的进位。
    """
    year = d.year + years
    try:
        return dt.date(year, d.month, d.day)
    except ValueError:
        return dt.date(year, d.month, d.day - 1)


def _last_year_window(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    return _shift_years(start), _shift_years(end)


# ----------------------------------------------------------------------
# 不完整周期判定（FR-M-2）
# ----------------------------------------------------------------------

def _period_completeness(start: dt.date, end: dt.date, today: dt.date) -> dict[str, Any]:
    """返回本周期完整度：complete / partial / future。

    判定依据是周期的自然结束日（左闭右开区间下为 end - 1 天）与数据截止日
    的关系，而不是窗口跨度：跨度只决定粒度，不决定数据是否收齐。

    例：窗口 2026-09-01 至 2026-10-01，自然结束日 9-30，today=9-16 → partial。
    """
    if start > today:
        return {"state": "future", "label": "周期未开始"}
    natural_end = end - dt.timedelta(days=1)
    if natural_end <= today:
        return {"state": "complete", "label": "完整周期"}
    return {"state": "partial", "label": "本期未完整"}


def _granularity_rank(granularity: str) -> int:
    """粒度由细到粗的序号。规则表的 min_granularity 比较用。"""
    try:
        return GRANULARITY_ORDER.index(granularity)
    except ValueError:
        return -1


# ----------------------------------------------------------------------
# 标量执行
# ----------------------------------------------------------------------

def _exec_scalar(repo: QueryRepository, sql: str, prefer: str | None = None) -> float | None:
    """执行聚合查询，返回标量。失败/空结果返回 None。

    prefer 是聚合列的别名。去掉 GROUP BY 之后同一条语句会带上维度列，
    「取第一个数值列」在维度本身是数值（年份、金额档位）时会取错列，
    因此有别名时优先按别名取。
    """
    try:
        result = repo.execute_readonly(sql)
    except (QueryError, QueryTimeout) as exc:
        logger.info("基准查询失败：%s", exc)
        return None
    if not result.rows:
        return None
    first = result.rows[0]
    if prefer and prefer in first and isinstance(first[prefer], (int, float)):
        return float(first[prefer])
    for v in first.values():
        if isinstance(v, (int, float)):
            return float(v)
    return None


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------

def compute_baselines(
    *,
    sql: str,
    rows: list[dict[str, Any]],
    metric_used: list[str],
    retrieved_time_range: dict[str, Any] | None,
    data_freshness: dict[str, Any] | None,
    catalog: Catalog,
    repo: QueryRepository,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """计算本轮查询的对比基准值。

    返回字典（任何字段都可能为 None 或不存在，调用方按字段渲染）：
      metric           主指标名（metric_used 首项，可能是显示名）
      granularity      day / week / month / year
      current_value    本期聚合值（重新跑聚合 SQL 取标量）
      current_period   {start, end, label, complete, state}
      mom              {start, end, label, value, deviation, comparable} 或 None
      yoy              同上
      partial_period   本期是否未走完
      note             给前端的一句话结论，取「无对比期数据」等固定文案
      rule             实际采用的规则表条目，回看时用来解释为什么某个基准没给
      missing_reasons  [str]，计算失败/无法计算的具体原因
    """
    metric = metric_used[0] if metric_used else None
    out: dict[str, Any] = {
        "metric": metric,
        "granularity": None,
        "current_value": None,
        "current_period": None,
        "mom": None,
        "yoy": None,
        "partial_period": False,
        "note": "",
        "rule": {},
        "missing_reasons": [],
    }
    if today is None:
        today = dt.date.today()

    # FR-M-3：规则按指标名索引，未配置取 default。catalog 缺该字段（例如测试
    # 里手工构造的 Catalog）时 baseline_rule 返回空字典，下面一律按「放开」处理。
    rule = catalog.baseline_rule(metric)
    out["rule"] = dict(rule)

    reasons: list[str] = out["missing_reasons"]  # 别名，写入时直接 append

    # 前置：必须有时间窗口。语义层未命中时间词时按「未限定」处理，
    # 这种情况下 baseline 没有意义，记一条原因。
    if not retrieved_time_range:
        reasons.append("未限定时间范围，无对比期可计算")
        return _finish(out)
    try:
        cur_start = dt.date.fromisoformat(retrieved_time_range["start"][:10])
        cur_end = dt.date.fromisoformat(retrieved_time_range["end"][:10])
    except (KeyError, ValueError, TypeError):
        reasons.append("时间范围解析失败，无法计算对比期")
        return _finish(out)

    span_days = (cur_end - cur_start).days
    if span_days <= 0:
        reasons.append("时间范围跨度为零")
        return _finish(out)

    # 桩模式下 generate 节点不写入时间过滤；这里补一条，让基准在桩模式下也可算
    working_sql = _inject_time_predicate_if_missing(sql, retrieved_time_range)

    fixed_granularity = rule.get("granularity")
    granularity = (
        _infer_granularity(span_days)
        if not fixed_granularity or fixed_granularity == "auto"
        else fixed_granularity
    )
    out["granularity"] = granularity

    completeness = _period_completeness(cur_start, cur_end, today)
    out["partial_period"] = completeness["state"] == "partial"
    out["current_period"] = {
        "start": cur_start.isoformat(),
        "end": cur_end.isoformat(),
        "label": retrieved_time_range.get("label") or "",
        "complete": completeness["state"] == "complete",
        "state": completeness["state"],
    }

    # 本期聚合标量。
    #
    # 走与对比期完全同一条路径：把原语句的时间谓词改写到本期窗口（值不变，
    # 实际作用是去掉 GROUP BY）后执行。两期算法一致才有可比性——
    # 此前本期用「外层 SUM 包住分组结果」，对比期用「去掉 GROUP BY 重算」，
    # 加法型指标两者相同，比率型指标则完全不同：退款率会变成各区比率之和
    # （0.348）与整体比率（0.051）相比，偏离度失真。
    if not _has_aggregate_select(working_sql):
        reasons.append("无法识别聚合列（本期聚合跳过）")
        return _finish(out)

    agg_alias = _find_aggregate_alias(working_sql)
    current_sql = _rewrite_time_predicates(working_sql, cur_start, cur_end)
    if current_sql:
        current_value = _exec_scalar(repo, current_sql, agg_alias)
        out["current_value"] = current_value
        if current_value is None:
            reasons.append("本期聚合查询失败")
    else:
        # 时间谓词改写不了（自定义函数、嵌套 CASE 等）时退回外层 SUM 包装，
        # 该路径下比率型指标不可比，因此一并记一条原因
        scalar_sql = _aggregate_to_scalar_sql(working_sql)
        if scalar_sql:
            current_value = _exec_scalar(repo, scalar_sql)
            out["current_value"] = current_value
            if current_value is None:
                reasons.append("本期聚合查询失败")
            else:
                reasons.append("本期聚合按分组求和近似，比率型指标的偏离度不可比")
        else:
            reasons.append("无法识别聚合列（本期聚合跳过）")

    # FR-M-3 粒度下限：细于下限的基准不输出。这里不给「降级后的值」，
    # 因为噪声值比缺失更危险——使用者会拿它当结论用。
    min_granularity = rule.get("min_granularity")
    too_fine = bool(
        min_granularity
        and _granularity_rank(granularity) >= 0
        and _granularity_rank(granularity) < _granularity_rank(min_granularity)
    )
    if too_fine:
        reasons.append(
            f"当前粒度（{granularity}）低于该指标的基准粒度下限（{min_granularity}），"
            "按规则表不输出对比基准"
        )
        return _finish(out)

    # mom
    if rule.get("mom") is False:
        reasons.append("该指标按规则表不计算环比")
    else:
        mom_start, mom_end = _prior_window(cur_start, cur_end, granularity)
        mom_sql = _rewrite_time_predicates(working_sql, mom_start, mom_end)
        if mom_sql:
            record_comparison_query()
            mom_value = _exec_scalar(repo, mom_sql, agg_alias)
            if mom_value is None:
                reasons.append("环比期数据缺失")
            elif out["current_value"] is not None:
                out["mom"] = {
                    "start": mom_start.isoformat(),
                    "end": mom_end.isoformat(),
                    "label": "环比",
                    "value": mom_value,
                    # FR-M-2：本期未走完时不给偏离度。值仍返回供前端标注参考，
                    # 但 comparable=False 且 deviation 为空，结论生成侧拿不到
                    # 一个可以写进「较上月下降 X%」的数字。
                    "deviation": _deviation(out["current_value"], mom_value, out["partial_period"]),
                    "comparable": not out["partial_period"],
                }
                if out["partial_period"]:
                    reasons.append("本期未完整，环比不参与结论生成")
        else:
            reasons.append("无法识别时间谓词（环比期跳过）")

    # yoy
    if rule.get("yoy") is False:
        reasons.append("该指标按规则表不计算同比")
    else:
        yoy_start, yoy_end = _last_year_window(cur_start, cur_end)
        yoy_sql = _rewrite_time_predicates(working_sql, yoy_start, yoy_end)
        if yoy_sql:
            record_comparison_query()
            yoy_value = _exec_scalar(repo, yoy_sql, agg_alias)
            if yoy_value is None:
                reasons.append("同比期数据缺失")
            elif out["current_value"] is not None:
                # 同比的对比期是去年同期，通常已完整；本期不完整只影响环比口径，
                # 不连带禁用同比——但偏离度同样不给，避免同一个句子里
                # 「同比 +xxx%」与「本期未完整」同时出现产生误导。
                out["yoy"] = {
                    "start": yoy_start.isoformat(),
                    "end": yoy_end.isoformat(),
                    "label": "同比",
                    "value": yoy_value,
                    "deviation": _deviation(out["current_value"], yoy_value, out["partial_period"]),
                    "comparable": not out["partial_period"],
                }
        else:
            reasons.append("无法识别时间谓词（同比期跳过）")

    return _finish(out)


def _deviation(current: float | None, base: float | None, partial: bool) -> float | None:
    """偏离度（本期 / 基准 - 1）。分母为零或本期不完整时返回 None。

    返回 None 而不是 0 或 ±inf：这两个值都会被下游当成「算出来了」写进结论。
    """
    if current is None or base is None or not base or partial:
        return None
    return (current - base) / base


def _finish(out: dict[str, Any]) -> dict[str, Any]:
    """收尾：给 note 填固定文案（FR-M-1）。

    FRD 要求无法计算时输出「无对比期数据」而不是留空——留空等于让使用者
    自行猜测是没算还是算了没给。note 是给前端的一句话，missing_reasons
    保留技术细节供排查，两者分工不同。
    """
    if out["mom"] is None and out["yoy"] is None:
        out["note"] = "无对比期数据"
    elif out["partial_period"]:
        out["note"] = "本期未完整，对比基准仅供参考"
    return out


__all__ = [
    "compute_baselines",
    "_infer_granularity",
    "_prior_window",
    "_last_year_window",
    "_period_completeness",
    "_granularity_rank",
]