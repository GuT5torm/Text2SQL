"""语义层加载、校验与召回。

本模块是纯函数模块：不连接数据库，不调用大模型。所有函数只做
「读 YAML → 校验一致性 → 按问题召回子集 → 渲染成 prompt 上下文」四件事。
这样语义层的正确性可以用普通单元测试覆盖，不需要任何外部依赖。

对外接口：
    catalog = load_catalog()                 # 加载并校验，失败抛 SemanticError
    hit = recall("上个月华东区的销售额", catalog)
    text = render_context(hit)               # 拼成可直接注入 prompt 的文本
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEMANTIC_DIR = Path(__file__).resolve().parent

# 召回的规模上限。表多了会稀释 prompt，实测 3 个指标 + 5 张表足够覆盖
# 演示级问题，超出部分说明问题本身过于宽泛，应由 route 节点引导用户收敛。
MAX_METRICS = 3
MAX_TABLES = 5
MAX_EXAMPLES = 3


class SemanticError(RuntimeError):
    """语义层配置不自洽。错误信息会列出全部问题，便于一次性改完。"""


@dataclass(frozen=True)
class TimeRange:
    """左闭右开的时间区间，与 SQL 中 created_at >= start AND < end 对应。"""

    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD，不含
    label: str  # 原始说法，如「上个月」，用于结论与前端标注
    resolved: str  # 换算结果，如「2026-08-01 至 2026-08-31」

    def as_filter(self, column: str = "orders.created_at") -> str:
        return f"{column} >= '{self.start}' AND {column} < '{self.end}'"


@dataclass
class SemanticHit:
    """一次召回的结果，字段与 generate 节点的 prompt 组装顺序一一对应。"""

    metrics: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)
    time_range: TimeRange | None = None
    matched_terms: list[str] = field(default_factory=list)
    # 命中的枚举值口语，如「江浙沪」→ orders.region = '华东'
    value_hints: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.metrics or self.tables or self.dimensions)

    def metric_displays(self) -> list[str]:
        return [m["display"] for m in self.metrics]

    def caveats(self) -> list[str]:
        return [
            f"{m['display']}：{m['caveats']}" for m in self.metrics if m.get("caveats")
        ]


@dataclass
class Catalog:
    """语义层的全部配置。加载一次即可复用。"""

    metrics: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    dimensions: list[dict[str, Any]]
    synonyms: list[dict[str, Any]]
    value_synonyms: list[dict[str, Any]]
    time_terms: list[dict[str, Any]]

    # —— 派生索引，加载时一次建好 ——
    metric_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    table_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    dimension_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)

    def all_field_refs(self) -> dict[str, set[str]]:
        """表名 → 该表的合法字段集合。字段白名单校验的唯一来源。"""
        return {
            t["name"]: {f["name"] for f in t.get("fields", [])} for t in self.tables
        }

    def table_names(self) -> set[str]:
        return set(self.table_by_name)


# --------------------------------------------------------------------------
# 加载与校验
# --------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SemanticError(f"语义层文件不存在：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SemanticError(f"{path.name} 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise SemanticError(f"{path.name} 顶层必须是映射结构")
    return data


def load_catalog(semantic_dir: Path | None = None) -> Catalog:
    """加载三份 YAML 并做一致性校验。任何不一致都会一次性列出。"""
    base = semantic_dir or SEMANTIC_DIR
    tables_doc = _read_yaml(base / "tables.yaml")
    metrics_doc = _read_yaml(base / "metrics.yaml")
    syn_doc = _read_yaml(base / "synonyms.yaml")

    catalog = Catalog(
        metrics=metrics_doc.get("metrics") or [],
        tables=tables_doc.get("tables") or [],
        dimensions=tables_doc.get("dimensions") or [],
        synonyms=syn_doc.get("synonyms") or [],
        value_synonyms=syn_doc.get("value_synonyms") or [],
        time_terms=syn_doc.get("time_terms") or [],
    )
    catalog.metric_by_name = {m["name"]: m for m in catalog.metrics}
    catalog.table_by_name = {t["name"]: t for t in catalog.tables}
    catalog.dimension_by_name = {d["name"]: d for d in catalog.dimensions}

    problems = _validate(catalog)
    if problems:
        raise SemanticError(
            "语义层配置存在 %d 处不一致：\n  - %s" % (len(problems), "\n  - ".join(problems))
        )
    return catalog


def _validate(c: Catalog) -> list[str]:
    problems: list[str] = []
    table_names = c.table_names()
    dim_names = set(c.dimension_by_name)

    if not c.metrics:
        problems.append("metrics.yaml 中没有任何指标定义")
    if not c.tables:
        problems.append("tables.yaml 中没有任何表定义")

    # 指标：依赖的表与维度必须存在，表达式不能为空
    for m in c.metrics:
        name = m.get("name")
        if not name:
            problems.append("存在缺少 name 的指标")
            continue
        if not m.get("expression"):
            problems.append(f"指标 {name} 缺少 expression")
        for t in m.get("tables") or []:
            if t not in table_names:
                problems.append(f"指标 {name} 引用了未登记的表 {t}")
        for d in m.get("dimensions") or []:
            if d not in dim_names:
                problems.append(f"指标 {name} 引用了未登记的维度 {d}")
        for ex in m.get("examples") or []:
            if not ex.get("q") or not ex.get("sql"):
                problems.append(f"指标 {name} 的 examples 存在缺少 q 或 sql 的条目")

    # 表：字段重名、ref 指向
    for t in c.tables:
        tname = t.get("name")
        if not tname:
            problems.append("存在缺少 name 的表")
            continue
        seen: set[str] = set()
        for f in t.get("fields") or []:
            fname = f.get("name")
            if not fname:
                problems.append(f"表 {tname} 存在缺少 name 的字段")
                continue
            if fname in seen:
                problems.append(f"表 {tname} 中字段 {fname} 重复定义")
            seen.add(fname)
            ref = f.get("ref")
            if ref and "." in ref:
                rt, rf = ref.split(".", 1)
                if rt not in table_names:
                    problems.append(f"{tname}.{fname} 的 ref 指向未登记的表 {rt}")
                elif rf not in {x["name"] for x in c.table_by_name[rt].get("fields", [])}:
                    problems.append(f"{tname}.{fname} 的 ref 指向不存在的字段 {ref}")

    # 维度：依赖的表必须存在
    for d in c.dimensions:
        dname = d.get("name")
        if not dname:
            problems.append("存在缺少 name 的维度")
            continue
        if not d.get("expression"):
            problems.append(f"维度 {dname} 缺少 expression")
        for t in d.get("tables") or []:
            if t not in table_names:
                problems.append(f"维度 {dname} 引用了未登记的表 {t}")

    # 同义词：指标类要指向真实指标，表 / 字段类要指向真实表或 表.字段
    for s in c.synonyms:
        term, target, kind = s.get("term"), s.get("maps_to"), s.get("type")
        if not term or not target:
            problems.append(f"同义词条目缺少 term 或 maps_to：{s}")
            continue
        if kind == "metric" and target not in c.metric_by_name:
            problems.append(f"同义词「{term}」指向不存在的指标 {target}")
        elif kind == "table" and target not in table_names:
            problems.append(f"同义词「{term}」指向不存在的表 {target}")
        elif kind == "field":
            if "." not in target:
                problems.append(f"同义词「{term}」的字段目标需写成 表.字段，当前为 {target}")
            else:
                tname, fname = target.split(".", 1)
                t = c.table_by_name.get(tname)
                if t is None:
                    problems.append(f"同义词「{term}」指向不存在的表 {tname}")
                elif fname not in {x["name"] for x in t.get("fields", [])}:
                    problems.append(f"同义词「{term}」指向不存在的字段 {target}")
        elif kind not in {"metric", "table", "field"}:
            problems.append(f"同义词「{term}」的 type 非法：{kind}")

    # 枚举值口语：字段必须真实存在
    for v in c.value_synonyms:
        term, fld = v.get("term"), v.get("field")
        if not term or not fld or "." not in fld:
            problems.append(f"枚举值口语条目格式错误：{v}")
            continue
        tname, fname = fld.split(".", 1)
        t = c.table_by_name.get(tname)
        if t is None:
            problems.append(f"枚举值口语「{term}」指向不存在的表 {tname}")
        elif fname not in {x["name"] for x in t.get("fields", [])}:
            problems.append(f"枚举值口语「{term}」指向不存在的字段 {fld}")

    return problems


# --------------------------------------------------------------------------
# 相对时间换算
# --------------------------------------------------------------------------

def _quarter_bounds(year: int, quarter: int) -> tuple[dt.date, dt.date]:
    start_month = 3 * (quarter - 1) + 1
    end_month = start_month + 3
    if end_month > 12:
        return dt.date(year, start_month, 1), dt.date(year + 1, 1, 1)
    return dt.date(year, start_month, 1), dt.date(year, end_month, 1)


def _shift_months(d: dt.date, months: int) -> dt.date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    day = min(d.day, calendar.monthrange(year, month + 1)[1])
    return dt.date(year, month + 1, day)


def resolve_time_term(term: dict[str, Any], today: dt.date | None = None) -> TimeRange:
    """把相对时间词换算成左闭右开的日期区间。

    周期口径固定：周从周一开始，季度与年度按自然季 / 自然年切分。
    把这件事放在语义层而不是交给模型，是为了同一句话在任何时候问都得到同样的边界。
    """
    ref = today or dt.date.today()
    kind = term["kind"]
    label = term["term"]

    if kind == "today":
        start, end = ref, ref + dt.timedelta(days=1)
    elif kind == "yesterday":
        start, end = ref - dt.timedelta(days=1), ref
    elif kind == "this_week":
        start = ref - dt.timedelta(days=ref.weekday())
        end = start + dt.timedelta(days=7)
    elif kind == "last_week":
        start = ref - dt.timedelta(days=ref.weekday() + 7)
        end = start + dt.timedelta(days=7)
    elif kind == "this_month":
        start = ref.replace(day=1)
        end = _shift_months(start, 1)
    elif kind == "last_month":
        end = ref.replace(day=1)
        start = _shift_months(end, -1)
    elif kind == "this_quarter":
        q = (ref.month - 1) // 3 + 1
        start, end = _quarter_bounds(ref.year, q)
    elif kind == "last_quarter":
        q = (ref.month - 1) // 3 + 1
        if q == 1:
            start, end = _quarter_bounds(ref.year - 1, 4)
        else:
            start, end = _quarter_bounds(ref.year, q - 1)
    elif kind == "this_year":
        start, end = dt.date(ref.year, 1, 1), dt.date(ref.year + 1, 1, 1)
    elif kind == "last_year":
        start, end = dt.date(ref.year - 1, 1, 1), dt.date(ref.year, 1, 1)
    elif kind == "last_n_days":
        n = int(term.get("n", 7))
        start, end = ref - dt.timedelta(days=n - 1), ref + dt.timedelta(days=1)
    elif kind == "last_n_months":
        n = int(term.get("n", 3))
        start = _shift_months(ref.replace(day=1), -(n - 1))
        end = ref + dt.timedelta(days=1)
    elif kind == "half_year":
        half = int(term.get("half", 1))
        if half == 1:
            start, end = dt.date(ref.year, 1, 1), dt.date(ref.year, 7, 1)
        else:
            start, end = dt.date(ref.year, 7, 1), dt.date(ref.year + 1, 1, 1)
    else:
        raise SemanticError(f"未实现的时间类型：{kind}")

    resolved = (
        f"{start.isoformat()} 至 {(end - dt.timedelta(days=1)).isoformat()}"
        if end - start <= dt.timedelta(days=1)
        else f"{start.isoformat()} 起至 {(end - dt.timedelta(days=1)).isoformat()} 止"
    )
    return TimeRange(start=start.isoformat(), end=end.isoformat(), label=label, resolved=resolved)


# --------------------------------------------------------------------------
# 规则召回
# --------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """去掉空白与全角标点，英文转小写，便于子串匹配。"""
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[，。？！、；：,\.\?!;:'\"（）\(\)\[\]【】]", "", text)
    return text.lower()


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def _score(question: str, phrases: list[str]) -> tuple[float, list[str]]:
    """短语命中打分。命中的短语越长，权重越高。"""
    score, hits = 0.0, []
    for phrase in phrases:
        p = _normalize(phrase)
        if p and p in question:
            score += 1.0 + 0.35 * len(p)
            hits.append(phrase)
    return score, hits


def _approx_score(question: str, phrases: list[str]) -> float:
    """字面不命中时的近似打分，用字符二元组重叠度兜底。"""
    q_grams = _bigrams(question)
    best = 0.0
    for phrase in phrases:
        p = _normalize(phrase)
        if len(p) < 2:
            continue
        p_grams = _bigrams(p)
        overlap = len(q_grams & p_grams) / len(p_grams)
        # 阈值压得较高，避免「订单量」被「客单价」这类形近词误召回
        if overlap >= 0.6:
            best = max(best, overlap)
    return best


def _matched_time_terms(q: str, time_terms: list[dict[str, Any]]) -> list[str]:
    """命中的相对时间词，长词在前。

    与 time_range 的取首个命中不同，这里要把全部命中的时间词都找出来：
    一句话里可能同时出现「截止昨天」和「这个月」，只有都摘掉才能挡住
    「月」字把「下单月份」维度带进来。
    """
    labels = {_normalize(t["term"]) for t in time_terms}
    return sorted([p for p in labels if p and p in q], key=len, reverse=True)


def _strip_phrases(q: str, phrases: list[str]) -> str:
    """从问题里摘掉指定片段。调用方保证片段已归一化。"""
    for p in phrases:
        q = q.replace(p, "")
    return q


def recall(question: str, catalog: Catalog, today: dt.date | None = None) -> SemanticHit:
    """按问题召回相关指标、表、维度、示例与时间区间。

    策略：先按同义词表精确命中，命中不足时用字符二元组重叠度做兜底。
    全过程无外部依赖，表数增长到数十张之前不需要引入向量检索。
    """
    q = _normalize(question)
    hit = SemanticHit()

    # —— 1. 时间区间：长词优先，避免「去年同期」被「去年」抢先匹配 ——
    for term in sorted(catalog.time_terms, key=lambda t: -len(_normalize(t["term"]))):
        label = _normalize(term["term"])
        if label and label in q:
            hit.time_range = resolve_time_term(term, today)
            break

    time_words = _matched_time_terms(q, catalog.time_terms)

    # —— 2. 枚举值口语：只在召回结果里作提示，不参与打分 ——
    for vs in catalog.value_synonyms:
        if _normalize(vs["term"]) in q:
            hit.value_hints.append(f"「{vs['term']}」对应 {vs['field']} = '{vs['value']}'")

    # —— 3. 指标 ——
    # 精确命中先过一遍「长短语支配短短语」。问「卖了多少钱」时，销量同义词里的
    # 「卖了多少」是同一处说法的前缀，不能因为这一处说法多召回一个指标。
    exact_hits: list[tuple[str, str, float]] = []  # (短语, 指标名, 贡献分)
    original_of: dict[str, str] = {}  # 归一化短语 → 配置里的原始写法
    for m in catalog.metrics:
        phrases = [m["display"], m["name"], *(m.get("synonyms") or [])]
        for phrase in phrases:
            p = _normalize(phrase)
            if p and p in q:
                exact_hits.append((p, m["name"], 1.0 + 0.35 * len(p)))
                original_of.setdefault(p, phrase)

    all_exact = {p for p, _, _ in exact_hits}
    dominated = {
        p
        for p in all_exact
        if any(other != p and len(other) > len(p) and p in other for other in all_exact)
    }

    picked: dict[str, float] = {}
    metric_hits: dict[str, list[str]] = {}
    for p, name, contribution in exact_hits:
        if p in dominated:
            continue
        picked[name] = picked.get(name, 0.0) + contribution
        metric_hits.setdefault(name, []).append(original_of[p])

    # 同义词表命中的指标类词，精确度高，配得上一档固定加分
    syn_metric_hits: dict[str, float] = {}
    for s in catalog.synonyms:
        if s.get("type") == "metric" and _normalize(s["term"]) in q:
            syn_metric_hits[s["maps_to"]] = syn_metric_hits.get(s["maps_to"], 0.0) + 2.0
    for name, bonus in syn_metric_hits.items():
        picked[name] = picked.get(name, 0.0) + bonus

    # 近似匹配只在整句一个精确信号都没有时才兜底。此前它与精确命中并行，
    # 结果是「卖了多少钱」里一个精确命中，就能让「赚了多少」靠字形相近混进来。
    if not picked:
        for m in catalog.metrics:
            phrases = [m["display"], m["name"], *(m.get("synonyms") or [])]
            approx = _approx_score(q, phrases)
            if approx > 0:
                picked[m["name"]] = approx * 1.5

    ordered = sorted(picked.items(), key=lambda x: -x[1])[:MAX_METRICS]
    hit.metrics = [catalog.metric_by_name[n] for n, _ in ordered if n in catalog.metric_by_name]

    # —— 4. 维度 ——
    # 时间词先摘掉再打维度分：否则「上个月」里的「月」会带出 order_month，
    # 「上季度」里的「季度」同理。时间语义已由 time_range 承担，
    # 用户没有要求按月看时不应再出现为一个下钻维度。
    q_dims = _strip_phrases(q, time_words)

    # 说了具体的枚举值，等于说了那个字段：无论是按它过滤还是按它分组，
    # 对应维度都该参与打分，只给取值提示不足以让后续环节知道该怎么 GROUP BY。
    dim_credit: dict[str, float] = {}
    for vs in catalog.value_synonyms:
        if _normalize(vs["term"]) in q_dims:
            for d in catalog.dimensions:
                if vs["field"] in (d.get("expression") or ""):
                    dim_credit[d["name"]] = dim_credit.get(d["name"], 0.0) + 2.0

    dim_scored: list[tuple[float, dict[str, Any]]] = []
    for d in catalog.dimensions:
        phrases = [d["display"], d["name"], *(d.get("synonyms") or [])]
        score, _hits = _score(q_dims, phrases)
        if score == 0:
            score = _approx_score(q_dims, phrases)
        score += dim_credit.get(d["name"], 0.0)
        if score > 0:
            dim_scored.append((score, d))
    dim_scored.sort(key=lambda x: -x[0])
    # 指标自带 dimensions 列表，只保留指标声明过有意义且本次命中的维度
    allowed: set[str] = set()
    for m in hit.metrics:
        allowed |= set(m.get("dimensions") or [])
    hit.dimensions = [
        d for _s, d in dim_scored[:4] if not allowed or d["name"] in allowed
    ][:3]

    # —— 5. 表：指标依赖的表 × 维度依赖的表 × 同义词直指的表 ——
    table_scores: dict[str, float] = {}
    for m in hit.metrics:
        for t in m.get("tables") or []:
            table_scores[t] = table_scores.get(t, 0.0) + 3.0
    for d in hit.dimensions:
        for t in d.get("tables") or []:
            table_scores[t] = table_scores.get(t, 0.0) + 2.0
    for t in catalog.tables:
        phrases = [t["display"], t["name"], *(t.get("synonyms") or [])]
        score, _h = _score(q, phrases)
        if score > 0:
            table_scores[t["name"]] = table_scores.get(t["name"], 0.0) + score
    for s in catalog.synonyms:
        if s.get("type") == "table" and _normalize(s["term"]) in q:
            table_scores[s["maps_to"]] = table_scores.get(s["maps_to"], 0.0) + 2.0

    # 维度命中的间接表：品类维度必然带上 products 与 categories
    for d in hit.dimensions:
        for t in d.get("tables") or []:
            table_scores.setdefault(t, 1.0)

    ordered_tables = sorted(table_scores.items(), key=lambda x: -x[1])[:MAX_TABLES]
    hit.tables = [catalog.table_by_name[n] for n, _ in ordered_tables if n in catalog.table_by_name]

    # —— 6. 示例：只取命中指标的，问题形态相近的排前面 ——
    pool: list[tuple[float, dict[str, Any]]] = []
    for m in hit.metrics:
        for ex in m.get("examples") or []:
            pool.append((_approx_score(q, [ex["q"]]) + len(_bigrams(_normalize(ex["q"])) & _bigrams(q)) * 0.05, ex))
    pool.sort(key=lambda x: -x[0])
    hit.examples = [ex for _s, ex in pool[:MAX_EXAMPLES]]

    # —— 7. 记录命中的词，供日志与前端口径标注 ——
    hit.matched_terms = sorted(
        {t for terms in metric_hits.values() for t in terms}
        | {s["term"] for s in catalog.synonyms if _normalize(s["term"]) in q},
        key=len,
        reverse=True,
    )[:8]
    return hit


# --------------------------------------------------------------------------
# prompt 上下文渲染
# --------------------------------------------------------------------------

def _render_table(t: dict[str, Any]) -> str:
    lines = [f"### {t['name']}（{t['display']}）"]
    if t.get("grain"):
        lines.append(f"- 粒度：{t['grain']}")
    if t.get("join"):
        lines.append(f"- 关联：{t['join']}")
    for f in t.get("fields") or []:
        desc = f"- {f['name']} {f['type']}"
        if f.get("display"):
            desc += f"  {f['display']}"
        if f.get("enum"):
            desc += "  取值：" + "、".join(f"{k}={v}" for k, v in f["enum"].items())
        if f.get("note"):
            desc += f"  注意：{f['note']}"
        lines.append(desc)
    return "\n".join(lines)


def _render_metric(m: dict[str, Any]) -> str:
    lines = [f"### {m['display']}（{m['name']}）"]
    lines.append(f"- 口径表达式：{m['expression']}")
    if m.get("required_filters"):
        lines.append("- 必带条件：" + "；".join(m["required_filters"]))
    if m.get("unit"):
        lines.append(f"- 单位：{m['unit']}")
    if m.get("caveats"):
        lines.append(f"- 口径边界：{m['caveats']}")
    return "\n".join(lines)


def render_context(hit: SemanticHit) -> str:
    """把召回结果渲染成 prompt 中的语义层段落。

    顺序固定为 时间口径 → 指标口径 → 表结构 → 维度 → 示例，与 generate.md
    中的占位符顺序一致，便于人工核对。
    """
    parts: list[str] = []

    if hit.time_range:
        tr = hit.time_range
        parts.append(
            "## 时间范围\n"
            f"用户说的「{tr.label}」已换算为：{tr.start} 起，{tr.end} 之前（左闭右开）。\n"
            f"过滤写法：`{tr.as_filter()}`。不要自行重新解释相对时间。"
        )

    if hit.value_hints:
        parts.append("## 说法对应\n" + "\n".join(f"- {h}" for h in hit.value_hints))

    if hit.metrics:
        parts.append(
            "## 指标口径\n"
            "以下口径为项目认定口径，生成 SQL 时必须按此表达式的含义聚合，不得自行改口径。\n\n"
            + "\n\n".join(_render_metric(m) for m in hit.metrics)
        )
    else:
        parts.append("## 指标口径\n本次未召回任何已登记指标。若问题涉及成交、销量、客单价等业务词，请在 SQL 中给出注释说明所用口径。")

    if hit.tables:
        parts.append("## 表结构\n" + "\n\n".join(_render_table(t) for t in hit.tables))

    if hit.dimensions:
        rows = [
            f"- {d['display']}（{d['name']}）：`{d['expression']}`"
            + (f"  {d['note']}" if d.get("note") else "")
            for d in hit.dimensions
        ]
        parts.append("## 可用维度\n" + "\n".join(rows))

    if hit.examples:
        blocks = [
            f"问题：{ex['q']}\nSQL：\n```sql\n{ex['sql'].strip()}\n```" for ex in hit.examples
        ]
        parts.append("## 参考示例\n" + "\n\n".join(blocks))

    return "\n\n".join(parts)


def describe_catalog(catalog: Catalog) -> dict[str, Any]:
    """给 /api/semantic 用的概览，不含具体配置正文。"""
    return {
        "metrics": [
            {"name": m["name"], "display": m["display"], "unit": m.get("unit"), "caveats": m.get("caveats")}
            for m in catalog.metrics
        ],
        "tables": [
            {"name": t["name"], "display": t["display"], "field_count": len(t.get("fields") or [])}
            for t in catalog.tables
        ],
        "dimensions": [{"name": d["name"], "display": d["display"]} for d in catalog.dimensions],
        "counts": {
            "metrics": len(catalog.metrics),
            "tables": len(catalog.tables),
            "dimensions": len(catalog.dimensions),
            "synonyms": len(catalog.synonyms),
            "value_synonyms": len(catalog.value_synonyms),
            "time_terms": len(catalog.time_terms),
        },
    }
