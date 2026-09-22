"""未收录维度检测。

FR-D-4 覆盖了「指标口径未命中」的显式提示，但没有覆盖另一类更容易骗过
用户的错误：问句要求按某个维度分组，而这个维度在语义层与物理表结构里都
不存在时，生成阶段会就近挑一个语义相近的字段顶替——把「支付方式」落成
`channel` 就是一例。这类 SQL 能跑通、结果有数据、图表看起来正常，但答案
答非所问，用户无法察觉。

本模块负责在召回阶段就把这件事识别出来：

1. 从句式明确的分组诉求里抽出所指的词（「各 X」「按 X 分组」结构中的 X）
2. 拿它对照「已登记词汇池」判断本库是否支持该维度

判定刻意保守，宁可漏报不可误报：

- 只在句式明确给出分组意图（各 / 每个 / 各种 / 不同 / 按…分组）时才抽取，
  不在整句里做开放式名词识别
- 只要抽取到的词与词汇池里任一项存在包含关系就判为已支持
- 词汇池过滤掉单字条目，避免「额」这类碎片词造成虚假包含

误报的代价是给正常查询的 prompt 与答案塞入错误的口径提示，比漏报更糟。
本模块不调用大模型，也不执行 SQL，纯函数。
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from app.semantic.loader import Catalog, UNSUPPORTED_CAVEAT_PREFIX

# 分组意图句式。顺序重要：长触发词必须排在短触发词之前，
# 否则「每个」会先被「各」吃掉，抽出来的 X 变成「个月」这类碎片。
_GROUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:每个|各个|各种|各个|各|不同|不同)\s*"
        r"([\u4e00-\u9fa5A-Za-z_]{2,8}?)"
        r"(?=的|，|,|如何|怎样|分布|占比|排序|排名|趋势|对比|$)"
    ),
    re.compile(
        r"(?:按照|按)\s*"
        r"([\u4e00-\u9fa5A-Za-z_]{2,8}?)"
        r"(?=分组|拆分|统计|汇总|划分|维度|看|来)"
    ),
)

# 抽词可能带上这些口语尾巴，去掉后再与词汇池比对
_TAIL_NOISE: tuple[str, ...] = ("情况", "明细", "数据", "信息", "层面", "角度")

# 介词与虚词。剥离已知片段后常常留下「在」「的」这类孤立字，
# 不清掉会让提示文案读起来像乱码。
_STOP_CHARS: frozenset[str] = frozenset("的在了和与及从到把被让给对为以")

# 明确的量纲/时间粒度词不是业务维度，不参与未收录判定。
# 它们各自对应的 date 函数列ҩ已在物理字段里，用户说到就能命中。
_NON_DIMENSION_TERMS: frozenset[str] = frozenset(
    {
        "每天", "每日", "每月", "每周", "每小时",
        "个小时", "月份", "季度", "年份", "日期", "天天",
        "周一", "周二", "周三", "周四", "周五", "周六", "周日",
    }
)


def _terms_of(item: dict[str, object]) -> set[str]:
    """取一个语义层对象的全部叫法：内部名、中文显示名与同义词。"""
    out = {str(item.get("name") or ""), str(item.get("display") or "")}
    out |= {str(s) for s in (item.get("synonyms") or [])}
    return {x for x in out if x}


def build_vocabulary(catalog: Catalog) -> set[str]:
    """汇总本库已知的全部叫法，作为「这个词是否被支持」的判定依据。

    来源覆盖五个层面：维度、指标、表、字段（含枚举取值与显示值）、
    以及同义词表与时间词的长度归一化写法。
    """
    vocab: set[str] = set()
    for item in catalog.dimensions:
        vocab |= _terms_of(item)
    for item in catalog.metrics:
        vocab |= _terms_of(item)
    for table in catalog.tables:
        vocab |= _terms_of(table)
        for field in table.get("fields") or []:
            vocab |= _terms_of(field)
            for key, value in (field.get("enum") or {}).items():
                vocab |= {str(key), str(value)}

    for item in catalog.synonyms:
        vocab |= {str(item.get("term") or ""), str(item.get("maps_to") or "")}
    for item in catalog.value_synonyms:
        vocab |= {str(item.get("term") or ""), str(item.get("value") or "")}
    for item in catalog.time_terms:
        vocab.add(str(item.get("term") or ""))

    # 单字条目参与包含判定会制造大量虚假命中（如「额」命中任何带额的词）
    return {v.lower() for v in vocab if v and len(v) >= 2}


def extract_dimension_terms(question: str) -> list[str]:
    """抽出问句里明确作为分组依据的词。

    只在句式给出分组意图时抽，抽不到返回空列表。同一个词重复出现只保留一次。
    """
    found: list[str] = []
    for pattern in _GROUP_PATTERNS:
        for raw in pattern.findall(question or ""):
            term = str(raw).strip()
            for noise in _TAIL_NOISE:
                if term.endswith(noise) and len(term) > len(noise) + 1:
                    term = term[: -len(noise)]
            if len(term) < 2:
                continue
            if term in _NON_DIMENSION_TERMS:
                continue
            if term not in found:
                found.append(term)
    return found


def _residue(term: str, vocabulary: set[str]) -> str:
    """剥掉词里已经被本库支持的部分，剩下的才是真正的生词。

    「各品类退款率」会整段落进抽词结果，但「品类」「退款率」各自都在词汇池里，
    剥完之后什么都不剩，说明这条没有未知维度。剥完仍有两字以上才值得警惕。

    「各种支付方式在去年的分布」抽到的是「支付方式在去年」，剥掉已登记的
    「去年」与孤立虚词后剩下的「支付方式」才是用户真正想看的那一列。
    """
    left = term
    changed = True
    while changed and len(left) >= 2:
        changed = False
        for known in vocabulary:
            if len(known) < 2 or known not in left:
                continue
            left = left.replace(known, "")
            changed = True
    left = "".join(ch for ch in left if ch not in _STOP_CHARS)
    return left


def detect_unsupported_dimensions(
    question: str, catalog: Catalog, vocabulary: set[str] | None = None
) -> list[str]:
    """返回问句里有分组诉求、但本库没有任何对应物的维度词。

    判据是「整词不在词汇池里」与「剥掉已知片段后仍有残留」两条同时成立。
    前者拦不住「品类退款率」这类粘连（整词不在池里但每一段都认识），
    后者补上这一层，避免把正常组合口语误判成新维度。

    vocabulary 可传入预先构建好的池子，便于批量调用时避免重复构建。
    池子为空说明 Catalog 未经 load_catalog 初始化，此时一律不判定——
    否则没有参照物，每个词都会被当成生词。
    """
    vocab = vocabulary if vocabulary is not None else build_vocabulary(catalog)
    if not vocab:
        return []
    victim: list[str] = []
    for term in extract_dimension_terms(question):
        # 优先按残片给出。抽词会把相邻的已知词一起吞进来（「支付方式在去年」），
        # 只有剥掉这些之后剩下的部分，才是本库真正缺的那一列。
        residue = _residue(term, vocab)
        if len(residue) < 2:
            continue
        if any(residue in known or known in residue for known in vocab):
            continue
        if residue not in victim:
            victim.append(residue)
    return victim


# --------------------------------------------------------------------------
# 事后核对：拿真实 SQL 反查实际分组字段
# --------------------------------------------------------------------------

def group_by_columns(sql: str) -> list[str]:
    """取 SQL 实际用于分组的列；没有 GROUP BY 时退到 SELECT 里的非聚合列。

    只用 sqlglot 做文本解析，不连接数据库，因此仍属于语义层的纯函数范畴。
    解析失败一律返回空列表——拿不到分组信息就不做提示，好过给出错的信息。
    """
    if not (sql or "").strip():
        return []
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - 解析异常不应打断主链路
        return []
    if tree is None:
        return []
    # WITH 子句时真正的查询挂在 this 上
    tree = tree.this if tree.args.get("with") is not None else tree

    group = tree.args.get("group")
    expressions = list(group.expressions) if group is not None else []
    if not expressions:
        projected = tree.args.get("expressions") or []
        expressions = [e for e in projected if e.find(exp.AggFunc) is None]

    out: list[str] = []
    for item in expressions[:3]:
        if not _is_real_column(item):
            continue
        text = item.sql(dialect="sqlite")
        if text and text not in out:
            out.append(text)
    return out


def _is_real_column(node: exp.Expression) -> bool:
    """字面量常量不构成分组依据。

    这一层过滤是被 p009 的真实输出逼出来的：模型在被告知「支付方式」不存在后，
    会写出 `SELECT '未知' AS 支付方式, COUNT(*) ...` 来维持结果集形态。
    字面量列看着像一个维度，实际不含任何分组信息，画出来就是一根写着「未知」
    的柱子——比顶替成 channel 诚实，但仍然是一张误导人的图。
    """
    if isinstance(node, exp.Alias):
        return _is_real_column(node.this)
    return not isinstance(node, exp.Literal)


def describe_substitution(sql: str, unsupported: list[str]) -> str | None:
    """把「未收录维度」提示精化成指明实际分组字段的文案。

    召回阶段只知道用户想要一个库里没有的维度；等 SQL 出来才知道系统实际用了
    哪个字段顶替。把两件事放在一起说出来，用户才核对得动。
    """
    if not unsupported:
        return None
    columns = group_by_columns(sql)
    if not columns:
        return None
    listed = "、".join(f"「{d}」" for d in unsupported)
    return (
        f"{UNSUPPORTED_CAVEAT_PREFIX}{listed}：本库无对应字段，"
        f"当前结果按 {'、'.join(columns)} 分组，与问题所指口径不同，请核对"
    )
