"""SQL 安全守卫。

即便演示场景只有自己使用，这一层也不能省。原因有两个：大模型的输出存在
不确定性，一次 DROP TABLE 就意味着重建数据；同时这套机制本身是本项目的
技术亮点，需要有可逐条核对的规则与用例。

守卫在只读连接之前生效，职责是「判能不能执行、必要时改写」，不负责执行。
执行期的时间、行数和结果体积限制由 sqlite_repo 承担。

九条规则与错误码：

  R1_SINGLE_STATEMENT   只允许单条语句
  R2_SELECT_ONLY        只允许查询类语句（SELECT / UNION / EXCEPT / INTERSECT）
  R3_FORBIDDEN_KEYWORD  语句树中不得出现 DDL / DML / 管理类关键字
  R4_TABLE_NOT_ALLOWED  表必须在语义层登记
  R5_COLUMN_NOT_ALLOWED 字段必须存在于其所属表的元数据中
  R6_LIMIT_ENFORCED     无 LIMIT 或 LIMIT 超限时自动包裹
  R7_CARTESIAN_JOIN     多表查询必须带连接条件
  R8_STATEMENT_TIMEOUT  执行超时，由 sqlite_repo 用 progress handler 实现
  R9_FUNCTION_ALLOWED   只允许白名单内的函数
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

# 语句级白名单：只有这四类能通过。其余一律拒绝，比维护一张黑名单更稳，
# 因为新版本 sqlglot 增加的语句类型会自动落在拒绝侧。
ALLOWED_STATEMENTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# 语句树中任何位置都不允许出现的节点。覆盖 SQLite 特有的 ATTACH / PRAGMA，
# 以及 sqlglot 对无法识别的语句给出的兜底节点 Command —— VACUUM 这类没有
# 独立节点类型的管理语句会落到 Command 上，因此不需要单独列。
#
# 节点名按名称取而不是直接写属性：sqlglot 各版本增删过表达式类（例如 Vacuum
# 在 30.x 已被移除），硬写属性会在升级时报 AttributeError。取不到的节点说明
# 该版本不解析这类语句，对应风险自然消失。
_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Create",
    "Alter",
    "AlterTable",
    "TruncateTable",
    "Attach",
    "Detach",
    "Pragma",
    "Command",
    "Merge",
    "Grant",
    "Transaction",
    "Commit",
    "Rollback",
    "Vacuum",
    "Use",
    "Set",
)

FORBIDDEN_NODES = tuple(
    node for node in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES) if node is not None
)

# 允许出现的库名前缀。SQLite 的 main 是默认库，带 main. 前缀属正常写法
ALLOWED_DB_PREFIX = {"", "main"}

# 函数白名单。守卫的意义是不信任模型的任何输出，因此也不能把兜底交给驱动层：
# randomblob 单次可产生 1e9 字节，load_extension / writefile / readfile 是否可用
# 只取决于 SQLite 的编译选项，换个构建就可能有完全不同的行为。这里按名字放行
# 常用函数，白名单外一律拒绝。
#
# 明确被挡在门外的（都不在白名单内）：
#   randomblob / zeroblob   按参数分配内存，一条语句即可耗尽进程内存
#   load_extension          加载外部动态库
#   readfile / writefile    读写文件系统（CLI 专属，但不应依赖这一点）
#   edit / fts3_tokenizer   同样属于不该出现在分析查询里的函数
#   sqlite_*                内部与诊断函数，一并拒绝
ALLOWED_FUNCTIONS = frozenset(
    {
        # 聚合
        "count", "sum", "avg", "min", "max", "total", "group_concat",
        # 数学
        "abs", "round", "ceil", "ceiling", "floor", "trunc", "exp", "ln", "log",
        "log2", "log10", "pow", "power", "sqrt", "mod", "sign",
        "acos", "asin", "atan", "atan2", "cos", "sin", "tan",
        "cosh", "sinh", "tanh", "degrees", "radians", "pi", "random",
        # 字符串
        "length", "char_length", "character_length", "lower", "upper", "ltrim",
        "rtrim", "trim", "substr", "substring", "replace", "instr", "concat",
        "concat_ws", "printf", "format", "reverse", "lpad", "rpad", "left",
        "right", "repeat", "split_part", "like", "glob", "quote", "hex",
        "unhex", "unicode", "char", "chr", "soundex", "startswith", "endswith",
        # 日期时间
        "date", "time", "datetime", "julianday", "strftime", "unixepoch",
        "current_date", "current_time", "current_timestamp",
        # 空值与类型转换
        "coalesce", "ifnull", "nullif", "iif", "if", "cast", "typeof",
        # 窗口函数
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist",
        "ntile", "lag", "lead", "first_value", "last_value", "nth_value",
        # JSON（SQLite 3.38+ 内置）
        "json", "json_array", "json_object", "json_extract", "json_valid",
        "json_type", "json_length", "json_group_array", "json_group_object",
        "json_each", "json_tree", "json_quote", "json_insert", "json_set",
        "json_replace", "json_remove", "json_patch",
    }
)

_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)

# FR-I-2：九条规则的编号必须出现在用户可读文本里，评测与前端按编号分流，
# 不再靠中文子串匹配。notice 形如 {"kind": "rejected"/"rewritten",
# "code": "R3", "message": "R3 ..."}。
_CODE_RE = re.compile(r"^\s*(R\d)\b")


def notice_code(message: str) -> str | None:
    """从一条告警文本取开头的规则编号（R1–R9），没有编号返回 None。"""
    match = _CODE_RE.match(message or "")
    return match.group(1) if match else None


def make_notice(kind: str, message: str) -> dict[str, str]:
    return {"kind": kind, "code": notice_code(message) or "", "message": message}


def rejected_notices_from_text(text: str | None) -> list[dict[str, str]]:
    """从 give_up 落库的 sql_error 文本还原 rejected 通道。

    守卫拒绝时走 give_up，errors 以「；」拼接进 sql_error，结构化对象没有
    随图终态保留；历史回看与本轮返回都靠这个函数按同一规则还原。
    执行层错误（SQLite 原始报错、非守卫的失败）没有编号时 code 留空。
    """
    if not text:
        return []
    return [make_notice("rejected", part.strip()) for part in text.split("；") if part.strip()]

# sqlglot 把逻辑运算符也挂在 Func 之下（And 的 MRO 是 And → Connector → Binary
# → Func），它们不是函数调用。不排除的话每条带 WHERE 的查询都会被 R9 拦下。
# 按名称取而不是写属性，理由同 FORBIDDEN_NODES。
_NON_FUNCTION_NODE_NAMES = ("And", "Or", "Not", "Xor", "Connector", "Binary", "Paren")

NON_FUNCTION_NODES = tuple(
    node for node in (getattr(exp, name, None) for name in _NON_FUNCTION_NODE_NAMES)
    if node is not None
)

# 恒真条件节点。sqlglot 读 sqlite 方言时会给缺 ON 的 JOIN 补一个 TRUE，
# 节点名写成字符串是为了兼容各版本的命名差异（Boolean / True_）
_TRUE_NODE_NAMES = ("Boolean", "True_")


_FUNC_NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _function_name(node: exp.Expression, dialect: str) -> str | None:
    """取函数在目标方言下的真实名字。

    不能直接拿 sql_name() 比对：它给的是 sqlglot 的内部类名，strftime 会被
    解析成 TimeToStr（sql_name 为 TIME_TO_STR），照此比对会把常用日期函数误杀。
    渲染回 SQLite 得到的才是实际发给驱动的函数名。

    取不到名字时返回 None，表示该节点不是真正的函数调用——sqlglot 会给日期
    函数的实参套一层隐式转换节点（TsOrDsToTimestamp），渲染出来是裸标识符，
    没有括号，不属于需要校验的对象。
    """
    try:
        rendered = node.sql(dialect=dialect)
    except Exception:  # 渲染不出来时退到类名，宁可误杀也不放行无法识别的调用
        return (node.sql_name() or "").lower() or None
    match = _FUNC_NAME_RE.match(rendered)
    # 不带括号说明这不是函数调用，而是 sqlglot 给日期参数套的隐式转换节点
    # （TsOrDsToTimestamp 渲染出来就是裸的列引用 o.created_at）
    return match.group(1).lower() if match else None


def _alias_to_real(stmt: exp.Expression) -> dict[str, str]:
    """表别名 → 真实表名。用于把 WHERE 里的限定符还原成表名再比对。"""
    mapping: dict[str, str] = {}
    for table in stmt.find_all(exp.Table):
        real = (table.name or "").lower()
        if real:
            mapping.setdefault(real, real)
        if table.alias:
            mapping[table.alias.lower()] = real
    return mapping


def _linked_pairs(stmt: exp.Expression, alias_to_real: dict[str, str]) -> set[frozenset[str]]:
    """WHERE 中形如 a.x = b.y 的等值关联涉及的表对。"""
    where = stmt.args.get("where")
    pairs: set[frozenset[str]] = set()
    if where is None:
        return pairs
    for eq in where.find_all(exp.EQ):
        sides: set[str] = set()
        for col in (eq.left, eq.right):
            if isinstance(col, exp.Column) and col.table:
                key = col.table.lower()
                sides.add(alias_to_real.get(key, key))
        if len(sides) == 2:
            pairs.add(frozenset(sides))
    return pairs


def _has_join_condition(join: exp.Join) -> bool:
    if join.args.get("using"):
        return True
    on = join.args.get("on")
    if on is None:
        return False
    if type(on).__name__ in _TRUE_NODE_NAMES and getattr(on, "this", True):
        return False  # ON TRUE 等同于没写条件
    return True


def strip_sql_fence(text: str) -> str:
    """去掉模型习惯性加上的 Markdown 代码围栏与结尾分号。"""
    out = _FENCE_RE.sub("", text.strip())
    return out.strip().rstrip(";").strip()


@dataclass
class GuardResult:
    ok: bool
    sql: str  # 通过校验后应当执行的语句，可能已被改写
    original_sql: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    limit_applied: int | None = None

    @property
    def rewritten(self) -> bool:
        return self.sql.strip() != self.original_sql.strip()

    def error_text(self) -> str:
        return "；".join(self.errors)

    def notices(self) -> list[dict[str, str]]:
        """结构化双通道（FR-I-2）：errors→rejected，warnings→rewritten。"""
        return [make_notice("rejected", e) for e in self.errors] + [
            make_notice("rewritten", w) for w in self.warnings
        ]


class SqlGuard:
    """按语义层的表与字段白名单校验 SQL。"""

    def __init__(self, field_refs: dict[str, set[str]], max_rows: int = 1000, dialect: str = "sqlite"):
        self.field_refs = {k.lower(): {f.lower() for f in v} for k, v in field_refs.items()}
        self.max_rows = max_rows
        self.dialect = dialect

    @classmethod
    def from_catalog(cls, catalog: Any, max_rows: int = 1000, dialect: str = "sqlite") -> "SqlGuard":
        """从语义层装配守卫。dialect 由配置传入（FR-J-2），不再硬编码。"""
        return cls(catalog.all_field_refs(), max_rows=max_rows, dialect=dialect)

    # —— 对外入口 ——
    def check(self, sql: str) -> GuardResult:
        original = strip_sql_fence(sql or "")
        errors: list[str] = []
        warnings: list[str] = []

        if not original:
            return GuardResult(False, "", original, errors=["R1 生成的 SQL 为空"])

        # R1 单条语句
        try:
            statements = [s for s in sqlglot.parse(original, read=self.dialect) if s is not None]
        except Exception as exc:  # sqlglot 的解析异常类型随版本变化，统一兜住
            return GuardResult(
                False, original, original, errors=[f"R1 SQL 解析失败，无法校验：{exc}"]
            )

        if len(statements) == 0:
            return GuardResult(False, original, original, errors=["R1 未解析出任何语句"])
        if len(statements) > 1:
            return GuardResult(
                False,
                original,
                original,
                errors=[f"R1 检测到 {len(statements)} 条语句，只允许单条查询"],
            )

        stmt = statements[0]

        # R2 语句类型
        if not isinstance(stmt, ALLOWED_STATEMENTS):
            errors.append(
                f"R2 只允许 SELECT 查询，当前语句类型为 {type(stmt).__name__.upper()}"
            )

        # R3 危险节点
        for forbidden in FORBIDDEN_NODES:
            found = stmt.find(forbidden)
            if found is not None:
                errors.append(f"R3 语句中包含被禁止的 {type(found).__name__.upper()} 操作")
                break

        # R4 / R7 表与连接
        tables, table_errors, join_errors = self._check_tables(stmt)
        errors.extend(table_errors)
        errors.extend(join_errors)

        # R5 字段
        columns, column_errors = self._check_columns(stmt, tables)
        errors.extend(column_errors)

        # R9 函数白名单
        errors.extend(self._check_functions(stmt))

        if errors:
            return GuardResult(
                False,
                original,
                original,
                errors=errors,
                tables=sorted(tables),
                columns=sorted(columns),
            )

        # R6 强制 LIMIT
        final_sql, applied = self._enforce_limit(original, stmt)
        if applied:
            warnings.append(
                f"R6 原语句未限定或超出结果行数上限，已包裹为最多返回 {applied} 行"
            )

        return GuardResult(
            True,
            final_sql,
            original,
            errors=[],
            warnings=warnings,
            tables=sorted(tables),
            columns=sorted(columns),
            limit_applied=applied,
        )

    # —— R4 / R7 ——
    def _check_tables(self, stmt: exp.Expression) -> tuple[set[str], list[str], list[str]]:
        errors: list[str] = []
        join_errors: list[str] = []
        tables: set[str] = set()

        # CTE 名不是真实表，必须排除，否则 WITH 子句会被误判为越权
        cte_names = {
            cte.alias.lower() for cte in stmt.find_all(exp.CTE) if cte.alias
        }
        # 子查询别名同样不是表名
        subquery_aliases = {
            sub.alias.lower()
            for sub in stmt.find_all(exp.Subquery)
            if sub.alias
        }

        for table in stmt.find_all(exp.Table):
            name = (table.name or "").lower()
            db = (table.db or "").lower()
            if db and db not in ALLOWED_DB_PREFIX:
                errors.append(f"R4 不允许访问 {db} 库中的表 {name}")
                continue
            if not name or name in cte_names or name in subquery_aliases:
                continue
            tables.add(name)
            if name not in self.field_refs:
                errors.append(f"R4 表 {name} 未在语义层登记，疑似模型编造")
                continue
            # 指定字段不存在时给出最接近的候选，便于 fix 节点定向修正
            if table.alias and "." in table.alias:
                errors.append(f"R4 表别名不合法：{table.alias}")

        join_errors.extend(self._check_joins(stmt, tables))

        return tables, errors, join_errors

    # —— R7 ——
    def _check_joins(self, stmt: exp.Expression, tables: set[str]) -> list[str]:
        """多表查询必须把表连起来，不能放任笛卡尔积。

        两种写法的判定方式不同：

          JOIN ... ON    条件写在 JOIN 上，只看 on / using 是否存在。
                         这里不能只判 on is None —— sqlglot 读 sqlite 方言时
                         会把「JOIN 后忘了写 ON」补成 ON TRUE，判空会漏掉。
          逗号连接        sqlglot 会归成 CROSS JOIN，但 FROM a, b WHERE a.x = b.y
                         是合法写法，一律拒绝会误伤，因此改看 WHERE 里的等值关联
                         是否覆盖了足够多的表对。
        """
        errors: list[str] = []
        alias_to_real = _alias_to_real(stmt)
        pairs = _linked_pairs(stmt, alias_to_real)

        for join in stmt.find_all(exp.Join):
            if (join.kind or "").upper() == "CROSS":
                # 至少要有 表数-1 条互不相同的表间关联，否则必有表是自由连接的
                needed = max(len(tables) - 1, 1)
                if len(pairs) < needed:
                    errors.append("R7 多表查询缺少连接条件，会产生笛卡尔积")
                break
            if not _has_join_condition(join):
                errors.append("R7 JOIN 缺少 ON 条件，会产生笛卡尔积")
                break
        return errors

    # —— R5 ——
    def _check_columns(
        self, stmt: exp.Expression, tables: set[str]
    ) -> tuple[set[str], list[str]]:
        errors: list[str] = []
        columns: set[str] = set()

        # 别名 → 真实表名。未登记的表在这里给 None，表示该作用域无法校验
        alias_map: dict[str, str | None] = {}
        for table in stmt.find_all(exp.Table):
            real = (table.name or "").lower()
            known = real if real in self.field_refs else None
            alias_map[real] = known
            if table.alias:
                alias_map[table.alias.lower()] = known
        # 子查询与 CTE 的作用域无法逐字段校验
        for sub in stmt.find_all(exp.Subquery):
            if sub.alias:
                alias_map[sub.alias.lower()] = None
        for cte in stmt.find_all(exp.CTE):
            if cte.alias:
                alias_map[cte.alias.lower()] = None

        # 投影别名，供 GROUP BY / ORDER BY 引用（例如 ORDER BY 成交总额）
        projection_aliases: set[str] = set()
        for select in stmt.find_all(exp.Select):
            for proj in select.expressions:
                if proj.alias:
                    projection_aliases.add(proj.alias.lower())

        # 可校验的表集合，以及这些表的字段并集，用于无前缀字段的归属判断
        known_tables = {t for t in tables if t in self.field_refs}
        union_fields: set[str] = set()
        for t in known_tables:
            union_fields |= self.field_refs[t]

        for col in stmt.find_all(exp.Column):
            name = (col.name or "").lower()
            qualifier = (col.table or "").lower()
            if not name or name == "*":
                continue

            if qualifier:
                if qualifier in alias_map:
                    owner = alias_map[qualifier]
                    if owner is None:
                        continue  # 子查询 / CTE 内部字段，超出静态可见范围
                    columns.add(f"{owner}.{name}")
                    if name not in self.field_refs[owner]:
                        errors.append(
                            f"R5 字段 {qualifier}.{name} 在表 {owner} 中不存在"
                            + self._suggest(name, owner)
                        )
                else:
                    # 可能直接写了未注册的表名作前缀
                    if qualifier in self.field_refs:
                        columns.add(f"{qualifier}.{name}")
                        if name not in self.field_refs[qualifier]:
                            errors.append(
                                f"R5 字段 {qualifier}.{name} 不存在"
                                + self._suggest(name, qualifier)
                            )
                    else:
                        errors.append(f"R5 引用了未知的表或别名 {qualifier}")
            else:
                if name in projection_aliases:
                    continue
                if not known_tables:
                    continue
                if name in union_fields:
                    for t in known_tables:
                        if name in self.field_refs[t]:
                            columns.add(f"{t}.{name}")
                            break
                else:
                    errors.append(
                        f"R5 字段 {name} 未在任何被引用的表中登记"
                        + self._suggest(name, None, union_fields)
                    )

        return columns, errors

    @staticmethod
    def _suggest(name: str, owner: str | None, pool: set[str] | None = None) -> str:
        """给出形近字段提示。模型拼错字段名时，这条提示比原始报错更有用。"""
        candidates = pool
        if candidates is None and owner is not None:
            candidates = set()
        if not candidates:
            return ""
        close = [
            c
            for c in sorted(candidates)
            if name[:3] == c[:3] or name in c or c in name
        ][:3]
        return f"，是否为 {'、'.join(close)}？" if close else ""

    # —— R9 ——
    def _check_functions(self, stmt: exp.Expression) -> list[str]:
        """只允许白名单内的函数。

        这一条补的是 R1–R8 留下的缺口：前面几条都在管「查哪些表、取哪些字段、
        返回多少行」，没有管「用什么函数算」。randomblob(1000000000) 不碰任何
        表、只返回一行，R1–R8 全部放行，结果集却有 1 GB。
        """
        errors: list[str] = []
        seen: set[str] = set()
        for node in stmt.find_all(exp.Func):
            if isinstance(node, NON_FUNCTION_NODES):
                continue
            name = _function_name(node, self.dialect)
            if name is None or name in ALLOWED_FUNCTIONS:
                continue
            if name in seen:
                continue
            seen.add(name)
            errors.append(f"R9 不允许使用函数 {name}")
        return errors

    # —— R6 ——
    def _enforce_limit(self, sql: str, stmt: exp.Expression) -> tuple[str, int | None]:
        exist_limit = self._read_limit(stmt)
        if exist_limit is not None and 0 < exist_limit <= self.max_rows:
            return sql, None
        # 用外层包裹而不是改写原语句的 LIMIT，避免依赖 sqlglot 的 AST 改写 API，
        # 各版本行为一致，且保留原语句的 ORDER BY
        body = strip_sql_fence(sql)
        return (
            f"SELECT * FROM (\n{body}\n) AS _guard_limited LIMIT {self.max_rows}",
            self.max_rows,
        )

    @staticmethod
    def _read_limit(stmt: exp.Expression) -> int | None:
        """只认顶层的 LIMIT，禁止向子树里找。

        早期实现在顶层取不到时会退到 stmt.find(exp.Limit)。UNION 的两个分支各
        自带 LIMIT 900 时，顶层没有 LIMIT，find 会取到分支里的 900，判定
        0 < 900 <= 1000 后放行——而分支的 LIMIT 约束的是分支自身，UNION 最终
        返回的是两分支之和。守卫以为完成了限流，实际没有。
        """
        node = stmt.args.get("limit")
        if node is None:
            return None
        # exp.Limit 在不同版本里计数存在 this 或 expression 上，两处都试
        for key in ("expression", "this"):
            value = node.args.get(key)
            if isinstance(value, exp.Literal):
                try:
                    return int(value.this)
                except (TypeError, ValueError):
                    return None
        return None
