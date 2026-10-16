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
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SEMANTIC_DIR = Path(__file__).resolve().parent

# 召回的规模上限。表多了会稀释 prompt，实测 3 个指标 + 5 张表足够覆盖
# 演示级问题，超出部分说明问题本身过于宽泛，应由 route 节点引导用户收敛。
MAX_METRICS = 3
MAX_TABLES = 5
MAX_EXAMPLES = 3

# 近似兜底的通过门槛。字面精确命中为零时才启用字符二元组重叠度兜底，
# 而业务上最容易混淆的两个指标恰恰字形相近：实测「上个月退了多少」在
# 销量与毛利上都得 0.667，两者打平，退款率反而是 0。
# 打平说明这一处说法里没有能区分指标的信号，此时猜一个指标比不猜更糟——
# 猜错会生成一条能执行、有数据、图表正常的语句，全程不报错。
# 因此除重叠度下限外再要求最优候选显著高于次优，二者缺一则整轮不猜指标，
# 由空命中逻辑接手。
APPROX_MIN_OVERLAP = 0.75
APPROX_MIN_GAP = 0.15

# FR-L-5 允许的计数口径。refund_row 按退款笔数，order 按订单数；
# 两者在「一单多退」下不是同一个数，维度必须显式声明取哪一个。
COUNT_GRAINS = {"refund_row", "order"}

# 未收录维度提示的固定前缀。graph 拿到真实 SQL 后据此定位该条 caveat，
# 替换成指明实际分组字段的精化文案，避免同一件事在答案里出现两遍。
UNSUPPORTED_CAVEAT_PREFIX = "未收录维度"


class SemanticError(RuntimeError):
    """语义层配置不自洽。错误信息会列出全部问题，便于一次性改完。"""


# FR-I-3：脱敏掩码的种类。phone 前 3 后 4，idcard 前 6 后 4，
# other 用定长占位符——任何敏感字段都必须显式声明用哪一种。
VALID_MASK_KINDS = {"phone", "idcard", "other"}


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
    # FR-D-4 补充：有分组诉求但本库无对应物的维度词。「支付方式」是典型——
    # 生成阶段若不点破，会就近拿 channel 顶替，SQL 跑得通但答非所问。
    unsupported_dimensions: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.metrics or self.tables or self.dimensions)

    def metric_displays(self) -> list[str]:
        return [m["display"] for m in self.metrics]

    def caveats(self) -> list[str]:
        rows = [
            f"{m['display']}：{m['caveats']}" for m in self.metrics if m.get("caveats")
        ]
        # 未收录维度用固定前缀，便于 graph 在拿到真实 SQL 后定位并替换成
        # 指明实际分组字段的精化文案，避免同一件事说两遍。
        rows.extend(
            f"{UNSUPPORTED_CAVEAT_PREFIX}「{d}」：本库语义层与表结构中均无对应字段"
            for d in self.unsupported_dimensions
        )
        return rows


@dataclass
class Catalog:
    """语义层的全部配置。加载一次即可复用。"""

    metrics: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    dimensions: list[dict[str, Any]]
    synonyms: list[dict[str, Any]]
    value_synonyms: list[dict[str, Any]]
    time_terms: list[dict[str, Any]]
    # FR-M-3 基准规则表。default 键存默认规则，其余键为指标名
    baseline_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    # FR-L-1 异常判定规则表，结构同上：default + 指标名 → 规则
    finding_rules: dict[str, dict[str, Any]] = field(default_factory=dict)

    # —— 派生索引，加载时一次建好 ——
    metric_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    table_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    dimension_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 指标 display（中文名）→ name。metric_used 里放的是 display，
    # 规则表按 name 索引，两处需要一个反向映射
    metric_name_by_display: dict[str, str] = field(default_factory=dict)
    # FR-I-3：敏感字段派生索引。键统一小写，形如 "users.phone"；
    # 值为 {"mask": "phone", "display": "手机号", "table": "users"}
    sensitive_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    # FR-D-4 补充：本库全部已知叫法的汇总池（维度 / 指标 / 表 / 字段 / 枚举 /
    # 同义词）。由 load_catalog 填充，服务于未收录维度检测，避免每次召回重建。
    vocabulary: set[str] = field(default_factory=set)

    def all_field_refs(self) -> dict[str, set[str]]:
        """表名 → 该表的合法字段集合。字段白名单校验的唯一来源。"""
        return {
            t["name"]: {f["name"] for f in t.get("fields", [])} for t in self.tables
        }

    def mask_kind(self, table: str | None, column: str) -> str | None:
        """按 表.字段 或物理字段名 / 中文显示名查掩码种类，查不到返回 None。

        结果集的列可能是物理名（phone）、带表限定名（u.phone 被驱动展平后
        仍是 phone）或中文别名（AS 手机号），三种写法都要能命中。
        """
        col = (column or "").lower()
        if table:
            hit = self.sensitive_fields.get(f"{table.lower()}.{col}")
            if hit:
                return hit["mask"]
        for entry in self.sensitive_fields.values():
            if col in {entry["field"].lower(), entry["display"].lower()}:
                return entry["mask"]
        return None

    def table_names(self) -> set[str]:
        return set(self.table_by_name)

    def baseline_rule(self, metric: str | None) -> dict[str, Any]:
        """取某个指标的基准规则。未配置或指标为空时返回 default。

        入参既可能是指标名（gmv）也可能是显示名（成交总额）——编排层的
        metric_used 来自模型输出或召回结果，两者都出现过，这里都认。
        """
        default = dict(self.baseline_rules.get("default") or {})
        if not metric:
            return default
        name = metric if metric in self.baseline_rules else self.metric_name_by_display.get(metric)
        if name is None:
            return default
        return {**default, **(self.baseline_rules.get(name) or {})}

    def finding_rule(self, metric: str | None) -> dict[str, Any]:
        """取某个指标的异常判定规则。未配置或指标为空时返回 default。

        与 baseline_rule 同构：入参既可能是指标名也可能是显示名，配置只写
        差异项，未写的字段从 default 继承。规则表缺省时返回空字典，由
        findings.py 的内建默认值兜底。
        """
        default = dict(self.finding_rules.get("default") or {})
        if not metric:
            return default
        name = metric if metric in self.finding_rules else self.metric_name_by_display.get(metric)
        if name is None:
            return default
        return {**default, **(self.finding_rules.get(name) or {})}


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


def _read_yaml_optional(path: Path) -> dict[str, Any]:
    """可选配置：文件不存在时返回空字典。

    只有异常判定规则表用这种方式读取。基准规则表缺了会让「未配置的指标取
    哪套规则」无从落地，必须报错；异常判定规则即使完全不配也有可用的内建
    默认值（阈值写在 findings.py），缺文件不该让整个应用起不来。
    """
    if not path.exists():
        return {}
    return _read_yaml(path)


def load_catalog(semantic_dir: Path | None = None) -> Catalog:
    """加载三份 YAML 并做一致性校验。任何不一致都会一次性列出。"""
    base = semantic_dir or SEMANTIC_DIR
    tables_doc = _read_yaml(base / "tables.yaml")
    metrics_doc = _read_yaml(base / "metrics.yaml")
    syn_doc = _read_yaml(base / "synonyms.yaml")
    rules_doc = _read_yaml(base / "baseline_rules.yaml") or {}
    # 规则表是「default + 指标名 → 规则」的扁平映射，yaml 里用 metrics 包一层
    # 只为可读性，加载时压平：后续所有查找都按指标名直接取。
    flat_rules: dict[str, dict[str, Any]] = {"default": rules_doc.get("default") or {}}
    for name, rule in (rules_doc.get("metrics") or {}).items():
        if isinstance(rule, dict):
            flat_rules[name] = rule
    finding_doc = _read_yaml_optional(base / "finding_rules.yaml") or {}
    flat_finding: dict[str, dict[str, Any]] = {"default": finding_doc.get("default") or {}}
    for name, rule in (finding_doc.get("metrics") or {}).items():
        if isinstance(rule, dict):
            flat_finding[name] = rule

    catalog = Catalog(
        metrics=metrics_doc.get("metrics") or [],
        tables=tables_doc.get("tables") or [],
        dimensions=tables_doc.get("dimensions") or [],
        synonyms=syn_doc.get("synonyms") or [],
        value_synonyms=syn_doc.get("value_synonyms") or [],
        time_terms=syn_doc.get("time_terms") or [],
        baseline_rules=flat_rules,
        finding_rules=flat_finding,
    )
    catalog.metric_by_name = {m["name"]: m for m in catalog.metrics}
    catalog.table_by_name = {t["name"]: t for t in catalog.tables}
    catalog.dimension_by_name = {d["name"]: d for d in catalog.dimensions}
    catalog.metric_name_by_display = {
        m["display"]: m["name"] for m in catalog.metrics if m.get("display")
    }
    # FR-I-3：敏感字段索引，供出参掩码管道（app/db/masking.py）使用
    for _t in catalog.tables:
        for _f in _t.get("fields") or []:
            if _f.get("sensitive"):
                catalog.sensitive_fields[f"{_t['name']}.{_f['name']}".lower()] = {
                    "table": _t["name"],
                    "field": _f["name"],
                    "display": _f.get("display") or _f["name"],
                    "mask": _f.get("mask") or "other",
                }

    # FR-D-4 补充：已登记叫法的汇总池，供未收录维度检测复用。
    # 这里延迟导入是因为 dimension_check 反向依赖 Catalog 类型做标注，
    # 顶层互引会形成环；放在加载流程末尾填充，recall 侧就不必每次重建。
    from app.semantic.dimension_check import build_vocabulary

    catalog.vocabulary = build_vocabulary(catalog)

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
            # FR-I-3：敏感字段必须声明合法 mask，且只能是 TEXT——
            # 数值/日期类型谈不上「脱敏」，标了 sensitive 多半是配置写错。
            if f.get("sensitive"):
                mask = f.get("mask")
                if mask not in VALID_MASK_KINDS:
                    problems.append(
                        f"表 {tname} 的敏感字段 {fname} 的 mask 非法：{mask}"
                        f"（允许 {sorted(VALID_MASK_KINDS)}）"
                    )
                if str(f.get("type") or "").upper() != "TEXT":
                    problems.append(
                        f"表 {tname} 的敏感字段 {fname} 必须为 TEXT 类型，"
                        f"当前为 {f.get('type')}"
                    )

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

    # FR-L-5：声明了计数口径的维度，口径取值与表达式必须自洽。
    # 一单多退的场景下「按笔数」与「按订单数」是两套数，口径写错不会报错，
    # 只会让各原因的占比之和超过整体，因此放在加载期就校验。
    for d in c.dimensions:
        dname = d.get("name") or "未命名维度"
        grain = d.get("count_grain")
        if grain is None:
            continue
        if grain not in COUNT_GRAINS:
            problems.append(
                f"维度 {dname} 的 count_grain 非法：{grain}（可选 {'/'.join(sorted(COUNT_GRAINS))}）"
            )
            continue
        exprs = d.get("count_expressions") or {}
        if not isinstance(exprs, dict):
            problems.append(f"维度 {dname} 的 count_expressions 必须是映射")
            continue
        if grain not in exprs:
            problems.append(f"维度 {dname} 声明的 count_grain={grain} 在 count_expressions 中没有对应表达式")

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

    # FR-M-4：含 enum 的字段必须说明划分依据。definition 缺失或纯空白都视为配置错误，
    # 因为「为什么是这 7 个大区」「为什么是这 4 个退款原因」这一类问题口径面板（FR-D-3）
    # 必须能直接回答，把依据写进字段比靠注释稳。
    for _t in c.tables:
        _tname = _t.get("name")
        for _f in _t.get("fields") or []:
            if not _f.get("enum"):
                continue
            _definition = (_f.get("definition") or "").strip()
            if not _definition:
                problems.append(
                    f"表 {_tname} 的字段 {_f.get('name')} 含 enum 但缺少 definition"
                    + "（FR-M-4：需说明划分依据，如收货地址 / 状态机 / 注册入口 / 退款原因归类）"
                )

    problems += _validate_baseline_rules(c)
    problems += _validate_finding_rules(c)

    # FR-M-5：声明同一 denominator 的指标，其 orders.status 过滤必须严格一致。
    # 没有 denominator 的指标不参与校验——它代表的是另一种分母概念（金额、件数等），
    # 校验目的只是阻止「同一分母却用不同状态过滤」这种会让比较错位的配置。
    _denom_groups: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for _m in c.metrics:
        _denom = _m.get("denominator")
        if not _denom:
            continue
        _sf = frozenset(
            _f.strip()
            for _f in (_m.get("required_filters") or [])
            if "orders.status" in _f
        )
        _denom_groups.setdefault(_denom, []).append((_m.get("name", "?"), _sf))

    for _denom, _members in _denom_groups.items():
        _unique = {s for _, s in _members}
        if len(_unique) <= 1:
            continue
        for _name, _sf in _members:
            _others = sorted(
                {n for n, s in _members if s and s != _sf}
                | {f"{n}（无 status 过滤）" for n, s in _members if not s and s != _sf},
            )
            problems.append(
                f"分母概念 '{_denom}' 不一致：指标 {_name} 的 orders.status 过滤为 "
                f"{sorted(_sf) if _sf else '（无）'}，与 {_others} 冲突"
                + "（FR-M-5：同一分母的指标必须使用相同的订单状态过滤）"
            )

    return problems


# --------------------------------------------------------------------------
# FR-M-3 基准规则表校验
# --------------------------------------------------------------------------

# 粒度由粗到细。baselines 模块用同一组常量做「细于下限则跳过」的比较，
# 两处必须一致，否则规则表写 week 而代码按另一套序排会出现反向判定。
GRANULARITY_ORDER: tuple[str, ...] = ("day", "week", "month", "year")
VALID_GRANULARITIES = set(GRANULARITY_ORDER)


def _validate_baseline_rules(c: Catalog) -> list[str]:
    """规则表校验：必须有 default；指标键必须是已登记指标；粒度取值合法。

    default 缺失会让「未配置的指标取默认规则」这条退化成取空字典，调用方
    拿不到 mom / yoy 开关，只能靠代码里的兜底常量——那等于把规则又硬编码
    了一遍，因此这里按错误而不是警告处理。
    """
    problems: list[str] = []
    rules = c.baseline_rules
    if not rules:
        # 未配置规则表（测试里手工构造的 Catalog）时不校验。load_catalog 总会
        # 至少填入 default 一份，因此这里只放过「整表缺失」，不放过「表内缺 default」。
        return []

    default = rules.get("default")
    if not isinstance(default, dict) or not default:
        problems.append("baseline_rules.yaml 缺少 default 规则组")

    for key, rule in rules.items():
        if key == "default":
            continue
        if key not in c.metric_by_name:
            problems.append(f"baseline_rules.yaml 的指标 {key} 未在 metrics.yaml 中登记")
        gran = rule.get("granularity")
        if gran is not None and gran != "auto" and gran not in VALID_GRANULARITIES:
            problems.append(
                f"baseline_rules.yaml 的指标 {key} 的 granularity 非法：{gran}"
                f"（允许 {sorted(VALID_GRANULARITIES)} 或 auto）"
            )
        min_gran = rule.get("min_granularity")
        if min_gran is not None and min_gran not in VALID_GRANULARITIES:
            problems.append(
                f"baseline_rules.yaml 的指标 {key} 的 min_granularity 非法：{min_gran}"
                f"（允许 {sorted(VALID_GRANULARITIES)}）"
            )
        for flag in ("mom", "yoy"):
            if flag in rule and not isinstance(rule[flag], bool):
                problems.append(f"baseline_rules.yaml 的指标 {key} 的 {flag} 必须是布尔值")
    return problems


# --------------------------------------------------------------------------
# FR-L-1 异常判定规则表校验
# --------------------------------------------------------------------------

VALID_SEVERITIES = {"提示", "关注"}
VALID_DIRECTIONS = {"above", "below"}


def _validate_finding_rules(c: Catalog) -> list[str]:
    """规则表校验：指标键已登记；阈值与比例是数值且在合理区间；严重度合法。

    阈值写成字符串或百分数（20 而不是 0.2）是这类配置最容易出的错，且错了
    不报异常——比较时 Python 会抛 TypeError 被 enrich 兜住，表现为「异常
    标记一个都不出」。放在加载期拦下。
    """
    problems: list[str] = []
    rules = c.finding_rules
    if not rules:
        return []

    for key, rule in rules.items():
        label = "default" if key == "default" else f"指标 {key}"
        if key != "default" and key not in c.metric_by_name:
            problems.append(f"finding_rules.yaml 的指标 {key} 未在 metrics.yaml 中登记")

        for field_name in ("deviation_threshold", "rank_ratio", "severity_ratio"):
            value = rule.get(field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"finding_rules.yaml 的 {label} 的 {field_name} 必须是数值")
            elif value <= 0:
                problems.append(f"finding_rules.yaml 的 {label} 的 {field_name} 必须大于 0")

        rank_ratio = rule.get("rank_ratio")
        if isinstance(rank_ratio, (int, float)) and not isinstance(rank_ratio, bool):
            if rank_ratio > 0.5:
                problems.append(
                    f"finding_rules.yaml 的 {label} 的 rank_ratio 不应超过 0.5"
                    "（超过则前后各一半，所有切片都是异常）"
                )

        min_slices = rule.get("min_slices")
        if min_slices is not None:
            if isinstance(min_slices, bool) or not isinstance(min_slices, int):
                problems.append(f"finding_rules.yaml 的 {label} 的 min_slices 必须是整数")
            elif min_slices < 2:
                problems.append(f"finding_rules.yaml 的 {label} 的 min_slices 至少为 2")

        severity = rule.get("severity")
        if severity is not None and severity not in VALID_SEVERITIES:
            problems.append(
                f"finding_rules.yaml 的 {label} 的 severity 非法：{severity}"
                f"（允许 {sorted(VALID_SEVERITIES)}）"
            )

        absolute = rule.get("absolute")
        if absolute is None:
            continue
        if not isinstance(absolute, dict):
            problems.append(f"finding_rules.yaml 的 {label} 的 absolute 必须是映射")
            continue
        threshold = absolute.get("threshold")
        if threshold is None:
            problems.append(f"finding_rules.yaml 的 {label} 的 absolute 缺少 threshold")
        elif isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            problems.append(f"finding_rules.yaml 的 {label} 的 absolute.threshold 必须是数值")
        direction = absolute.get("direction", "above")
        if direction not in VALID_DIRECTIONS:
            problems.append(
                f"finding_rules.yaml 的 {label} 的 absolute.direction 非法：{direction}"
                f"（允许 {sorted(VALID_DIRECTIONS)}）"
            )
        if absolute.get("severity") is not None and absolute["severity"] not in VALID_SEVERITIES:
            problems.append(
                f"finding_rules.yaml 的 {label} 的 absolute.severity 非法：{absolute['severity']}"
            )
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


def _approx_score(question: str, phrases: list[str], min_overlap: float = 0.6) -> float:
    """字面不命中时的近似打分，用字符二元组重叠度兜底。

    min_overlap 由调用方按场景给：指标判定关系到「答的是不是问的那个指标」，
    门槛要比维度判定高，见 APPROX_MIN_OVERLAP 处的说明。
    """
    q_grams = _bigrams(question)
    best = 0.0
    for phrase in phrases:
        p = _normalize(phrase)
        if len(p) < 2:
            continue
        p_grams = _bigrams(p)
        overlap = len(q_grams & p_grams) / len(p_grams)
        # 阈值压得较高，避免「订单量」被「客单价」这类形近词误召回
        if overlap >= min_overlap:
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
    #
    # 现在再加一道闸：候选分数打平时整轮不猜。字形相近的指标（销量 / 毛利 /
    # 退款率共用「X 了多少」这一套说法模板）打平是常态，此时选谁都是错的，
    # 不如留空让空命中逻辑接手——空命中至少看得见，猜错则全程无报错。
    if not picked:
        approx_scored: list[tuple[float, str]] = []
        for m in catalog.metrics:
            phrases = [m["display"], m["name"], *(m.get("synonyms") or [])]
            approx = _approx_score(q, phrases, APPROX_MIN_OVERLAP)
            if approx > 0:
                approx_scored.append((approx, m["name"]))
        approx_scored.sort(key=lambda x: -x[0])

        if approx_scored:
            top_score, top_name = approx_scored[0]
            runner_up = approx_scored[1][0] if len(approx_scored) > 1 else 0.0
            if top_score - runner_up >= APPROX_MIN_GAP:
                picked[top_name] = top_score * 1.5
            else:
                logger.info(
                    "近似兜底放弃：候选分数接近（%s），改由空命中逻辑处理",
                    [(n, round(s, 3)) for s, n in approx_scored[:3]],
                )

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

    # —— 8. 未收录维度（FR-D-4 补充）——
    # 延迟导入的原因同 load_catalog：dimension_check 依赖 Catalog 做类型标注。
    from app.semantic.dimension_check import detect_unsupported_dimensions

    # 词汇池由 load_catalog 填充；手工构造的 Catalog 可能没有，此时跳过检测，
    # 否则每一项都会被判成生词。
    hit.unsupported_dimensions = detect_unsupported_dimensions(
        question, catalog, catalog.vocabulary
    )
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
            + (f"  {d.get('note')}" if d.get("note") else "")
            for d in hit.dimensions
        ]
        parts.append("## 可用维度\n" + "\n".join(rows))

    if hit.examples:
        blocks = [
            f"问题：{ex['q']}\nSQL：\n```sql\n{ex['sql'].strip()}\n```" for ex in hit.examples
        ]
        parts.append("## 参考示例\n" + "\n\n".join(blocks))

    if hit.unsupported_dimensions:
        listed = "、".join(f"「{d}」" for d in hit.unsupported_dimensions)
        parts.append(
            "## 本库未收录的维度\n"
            f"问题里的 {listed} 既不在上面的「可用维度」里，也不是任何一张表的字段，"
            "本库没有这份数据。\n"
            "不要用语义相近的其他字段顶替——例如用「下单渠道」充当「支付方式」。"
            "顶替出来的语句能执行、结果有数据、图表看着正常，但答的已经不是用户问的"
            "那一列，而且全程没有任何报错会让用户察觉。\n"
            "也不要用字面量伪造这一列，例如 `SELECT '未知' AS 支付方式`：那会画出一根"
            "写着「未知」的柱子，看着像答案，实际没有任何分组信息。\n"
            "正确做法：该维度缺位就不给这一列，按全量口径出数；并在 reasoning 里写明"
            "「本库无 X，本轮未按该维度拆分」，供用户核对。"
        )

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
