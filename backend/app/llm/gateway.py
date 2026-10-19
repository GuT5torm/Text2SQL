"""LLM 网关。

所有节点都通过这里调用模型，不直接持有客户端。集中在这一层解决的问题：

  · 超时与重试      超时、限流、连接失败按指数退避重试，其他错误直接抛出
  · 模型降级        主模型连续失败后切备用模型，配置化，不改代码
  · 结构化输出      优先用 JSON mode，不支持时退回文本解析，解析失败强化格式再试一次
  · 用量统计        token 数与耗时落到内存，供 /api/stats 与日志查看
  · 离线桩实现      未配置密钥时用固定模板替代真实调用，让整条链路可以先跑通

注意：网关不含任何业务 Prompt。节点要说什么话，由 agent/prompts/ 下的
Markdown 决定；网关只负责把话送出去、把结果按契约拿回来。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.llm.mock_sql import build_sql, parse_form_block

logger = logging.getLogger(__name__)

try:  # 离线环境下允许缺少 openai 包，桩实现仍可用
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    """网关层面的失败，与业务无关。"""


@dataclass
class UsageRecord:
    tag: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_ms: float
    ok: bool
    fallback: bool = False


@dataclass
class Gateway:
    settings: Settings | None = None
    _client: Any = None
    usage: list[UsageRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.settings = self.settings or get_settings()
        if not self.settings.llm_mock and OpenAI is not None and self.settings.llm_api_key:
            self._client = OpenAI(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                timeout=self.settings.llm_timeout,
                max_retries=0,  # 重试策略由本层统一控制
            )

    @property
    def mock(self) -> bool:
        """是否处于桩实现模式。未配置密钥时自动启用，避免整条链路无法启动。"""
        return self.settings.llm_mock or self._client is None

    # ------------------------------------------------------------------
    # 基础调用
    # ------------------------------------------------------------------
    def complete(
        self,
        prompt: str,
        system: str | None = None,
        *,
        tag: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """返回模型的文本输出。失败按配置重试，仍失败则抛 LLMError。"""
        s = self.settings
        if s is None:
            raise LLMError("配置未初始化")
        if self.mock:
            return _mock_text(tag, prompt)

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        models = [s.llm_model] + ([s.llm_fallback_model] if s.llm_fallback_model else [])
        last_error: Exception | None = None

        for idx, model in enumerate(models):
            for attempt in range(s.llm_max_retries + 1):
                started = time.monotonic()
                try:
                    kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": s.llm_temperature if temperature is None else temperature,
                        "max_tokens": max_tokens or s.llm_max_tokens,
                    }
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    resp = self._client.chat.completions.create(**kwargs)
                    content = (resp.choices[0].message.content or "").strip()
                    self._record(tag, model, resp, started, ok=True, fallback=idx > 0)
                    if not content:
                        raise LLMError("模型返回了空内容")
                    return content
                except Exception as exc:  # noqa: BLE001 - 需要区分可重试与不可重试
                    last_error = exc
                    retryable = _is_retryable(exc)
                    self._record(tag, model, None, started, ok=False, fallback=idx > 0)
                    if not retryable:
                        logger.warning("LLM 调用失败且不可重试：%s", exc)
                        raise LLMError(f"模型调用失败：{exc}") from exc
                    if attempt < s.llm_max_retries:
                        backoff = 2**attempt
                        logger.info("LLM 调用可重试失败（%s），%s 秒后再试", exc, backoff)
                        time.sleep(backoff)
            logger.warning("模型 %s 重试耗尽，%s", model, "尝试备用模型" if idx + 1 < len(models) else "不再降级")

        raise LLMError(f"模型调用失败，已耗尽重试：{last_error}")

    def complete_json(
        self,
        prompt: str,
        schema: type[T],
        *,
        tag: str = "generic",
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        """要求模型返回符合 schema 的 JSON。

        顺序：JSON mode → 解析失败则去掉 JSON mode 重试 → 仍失败则强化格式提示再试一次。
        国内模型对 function calling 的支持程度不一，因此以 JSON mode 为主、文本解析兜底。
        """
        if self.mock:
            return _mock_json(tag, prompt, schema)

        raw = self.complete(
            prompt, system, tag=tag, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        parsed = _parse_json(raw)
        if parsed is not None:
            model = _validate(schema, parsed)
            if model is not None:
                return model

        logger.info("[%s] JSON 解析或校验失败，去掉 JSON mode 重试一次", tag)
        raw = self.complete(
            prompt, system, tag=tag, temperature=temperature, max_tokens=max_tokens, json_mode=False
        )
        parsed = _parse_json(raw)
        if parsed is not None:
            model = _validate(schema, parsed)
            if model is not None:
                return model

        strengthened = (
            f"{prompt}\n\n"
            "上一轮输出无法解析为要求的 JSON。请严格只输出一个 JSON 对象，"
            "不要包含解释文字、Markdown 代码围栏或多余前后缀。"
        )
        raw = self.complete(
            strengthened,
            system,
            tag=tag,
            temperature=0.0,
            max_tokens=max_tokens,
            json_mode=True,
        )
        parsed = _parse_json(raw)
        if parsed is None:
            raise LLMError(f"[{tag}] 三次尝试均无法得到可解析的 JSON")
        model = _validate(schema, parsed)
        if model is None:
            raise LLMError(f"[{tag}] 输出结构不符合 {schema.__name__} 约定")
        return model

    # ------------------------------------------------------------------
    def _record(self, tag, model, resp, started, *, ok, fallback=False) -> None:
        prompt_tokens = completion_tokens = 0
        if resp is not None and getattr(resp, "usage", None) is not None:
            prompt_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        self.usage.append(
            UsageRecord(
                tag=tag,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                ok=ok,
                fallback=fallback,
            )
        )

    def _estimated_cost(self) -> float:
        """按配置价格表估算累计成本（FR-J-3）。

        价格单位是「每千 token」，未配置价格的模型按 0 计，不臆造价格。
        """
        prices = self.settings.llm_prices if self.settings else {}
        total = 0.0
        for u in self.usage:
            price = prices.get(u.model)
            if not price:
                continue
            total += u.prompt_tokens / 1000.0 * price["input"]
            total += u.completion_tokens / 1000.0 * price["output"]
        return round(total, 6)

    def totals(self) -> dict[str, Any]:
        return {
            "calls": len(self.usage),
            "failed": sum(1 for u in self.usage if not u.ok),
            "fallback_calls": sum(1 for u in self.usage if u.fallback),
            "prompt_tokens": sum(u.prompt_tokens for u in self.usage),
            "completion_tokens": sum(u.completion_tokens for u in self.usage),
            # FR-J-3：累计估算成本，价格表走配置（未配置模型按 0 计）
            "estimated_cost": self._estimated_cost(),
            "elapsed_ms": round(sum(u.elapsed_ms for u in self.usage), 1),
            "mock": self.mock,
            "by_tag": _group_usage(self.usage),
        }


def _group_usage(records: list[UsageRecord]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        slot = out.setdefault(r.tag, {"calls": 0, "failed": 0, "elapsed_ms": 0.0})
        slot["calls"] += 1
        slot["failed"] += 0 if r.ok else 1
        slot["elapsed_ms"] = round(slot["elapsed_ms"] + r.elapsed_ms, 1)
    return out


def _is_retryable(exc: Exception) -> bool:
    """只对超时、限流、连接类错误重试。参数错误重试多少次都一样。"""
    name = type(exc).__name__
    if name in {"APITimeoutError", "RateLimitError", "APIConnectionError", "InternalServerError"}:
        return True
    text = str(exc).lower()
    return any(
        k in text
        for k in ("timeout", "timed out", "rate limit", "too many requests", "connection", "502", "503", "504")
    )


def _parse_json(raw: str) -> Any:
    """从模型输出中取出 JSON。容忍代码围栏与前后说明文字。"""
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 退而求其次：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _validate(schema: type[T], data: Any) -> T | None:
    if not isinstance(data, dict):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        logger.info("结构化输出校验失败：%s", exc.errors()[:2])
        return None


# ----------------------------------------------------------------------
# 离线桩实现
#
# 仅在未配置密钥时启用，用途是「在拿到密钥之前先把整条链路跑通」以及
# 单元测试。返回的是固定模板，不代表真实模型能力，不要据此判断准确率。
# ----------------------------------------------------------------------

@lru_cache(maxsize=1)
def _mock_catalog() -> Any:
    """桩实现用的语义层目录。

    桩 SQL 要与语义层口径一致，就得读 metrics.yaml / tables.yaml。按需加载并
    缓存，import 放在函数体内——只有桩模式会走到这里，真实模型路径不碰。

    替换掉了原先的 _MOCK_SQL_BY_KEYWORD：那份关键词到整条 SQL 的映射里只有
    成交总额与退款率两种聚合表达式，问毛利、问订单量、问客单价、问成交用户数
    都会落到成交总额模板上，于是出现「口径提示写毛利、结果列写成交总额」而
    系统不报错的情形。
    """
    from app.semantic.loader import load_catalog

    return load_catalog()


_QUESTION_SECTION_RE = re.compile(
    r"##[^\n#]*问题[^\n]*\n(?P<body>.*?)(?=\n##[^#]|\Z)", re.DOTALL
)
# route.md 用「本轮问题：」标记问题位置，generate.md 用「## 问题」。
# 两种标记都要识别，否则 route 节点会把整段 prompt 当成问题做关键词匹配，
# 误触发改写（prompt 里的示例「按品类拆一下」含「按」字）。
_CURRENT_QUESTION_RE = re.compile(r"本轮问题：\s*")


def _mock_subject(prompt: str) -> str:
    """桩实现的关键词匹配对象。

    取「## …问题」这小节的正文，到下一个二级标题为止；route.md 没有这一节，
    用「本轮问题：」标记兜底。

    必须按小节截断，不能像先前那样取标题之后的全部内容：fix.md 的
    「## 原始问题」后面还跟着「## 语义层信息」「## 失败语句」「## 报错原文」，
    语义层段落里列着本项目全部指标名，桩实现会照着「退款率」去生成一条退款
    查询，与用户问的成交额毫无关系。
    """
    matches = list(_QUESTION_SECTION_RE.finditer(prompt))
    if matches:
        body = matches[-1].group("body").strip()
        if body:
            return body
    # route.md 路径：取最后一个「本轮问题：」之后的内容
    matches = list(_CURRENT_QUESTION_RE.finditer(prompt))
    if matches:
        return prompt[matches[-1].end() :].strip() or prompt
    return prompt


def _mock_text(tag: str, prompt: str) -> str:
    if tag in {"generate", "fix"}:
        # SQL 由语义层派生，不再按关键词查写死的模板，原因见 _mock_catalog。
        # 表单路径的条件与分组维度不在问句里，从提示词的条件节回读：桩实现若
        # 不带条件，表单条件的强断言在桩模式下必然全红，那时它验的是桩的短板
        # 而不是语句的缺陷。
        conditions, dimensions = parse_form_block(prompt)
        return build_sql(
            _mock_subject(prompt),
            _mock_catalog(),
            conditions=conditions or None,
            dimensions=dimensions or None,
        )
    if tag == "route":
        return "{}"
    if tag == "summarize":
        return "{}"
    if tag == "visualize":
        return "{}"
    return ""


# FR-A-1/A-2：桩模式下识别追问关键词。命中时填 rewritten_question，
# 让多轮改写在桩模式下可观测。切到真实模型后由模型自行判断。
_MOCK_REWRITE_KEYWORDS: tuple[str, ...] = (
    "那上个月", "那上个月呢", "上个月呢", "换成", "改成", "按", "只看",
    "那本周", "本周呢", "那昨天", "昨天呢",
)

# FR-A-3：排序与 TopN 关键词
_MOCK_TOPN_KEYWORDS: tuple[str, ...] = (
    "前5", "前 5", "前五", "top5", "top 5", "前10", "前 10", "前十", "top10",
    "前3", "前 3", "前三", "top3",
)
_MOCK_RANK_KEYWORDS: tuple[str, ...] = ("排名", "排行", "排第几", "榜单")

# FR-A-4：数值过滤关键词（阈值不被当作维度取值）
_MOCK_NUMERIC_GT: tuple[str, ...] = ("大于", "超过", "高于", "以上", ">", "＞")
_MOCK_NUMERIC_LT: tuple[str, ...] = ("小于", "低于", "以下", "<", "＜")

# 域外问题：要一个尚未发生的数、查库里没有的实体、或根本不是取数任务。
# 桩模式靠这张表识别，真实模式由 route.md 的判定标准交给模型。两边必须同时
# 维护：只改一边会出现「桩模式下新分支永不触发」的评测假绿。
_MOCK_OUT_OF_SCOPE_KEYWORDS: tuple[str, ...] = (
    # 预测未来
    "预测", "预估", "预报", "预计", "未来", "下个月会", "下季度会", "下个月能",
    # 库里没有的实体
    "库存", "存货", "备货", "仓储", "物流", "快递", "员工", "绩效", "广告投放",
    # 不是取数任务
    "爬虫", "写代码", "写个代码", "帮我写", "翻译成", "翻译一下", "生成代码",
    "天气", "股价", "招聘",
)

# 归因问题：问原因与因果。「原因」二字单用会误伤「各退款原因的退款率」，
# 因此只登记带疑问语素的组合，不登记裸词。
_MOCK_ATTRIBUTION_KEYWORDS: tuple[str, ...] = (
    "为什么", "为何", "怎么会", "怎么回事", "咋回事", "什么原因",
    "原因是什么", "原因何在", "归因", "是怎么造成的", "是谁带",
)


def _mock_route_decision(prompt: str) -> dict[str, Any]:
    """桩模式下按关键词填充 RouteDecision 的 slots 与 rewritten_question。

    桩模式无法真正理解语义，但可以把能被关键词识别的槽位填出来，让
    FR-A-1/A-2/A-3/A-4 的机制与接口在桩模式下可观测、可单测。
    切到真实模型后，模型自行填充，这里不再生效。
    """
    subject = _mock_subject(prompt)
    slots: dict[str, Any] = {
        "metrics": [],
        "dimensions": [],
        "filters": [],
        "time_range": "",
        "order_by": None,
        "top_n": None,
    }

    # 指标识别
    if any(k in subject for k in ("退款", "退货")):
        slots["metrics"].append("退款率")
    if any(k in subject for k in ("成交", "销售额", "销售额", "销售")):
        if "成交总额" not in slots["metrics"]:
            slots["metrics"].append("成交总额")
    if any(k in subject for k in ("订单", "下单")):
        slots["metrics"].append("订单量")
    if any(k in subject for k in ("客单价",)) :
        slots["metrics"].append("客单价")
    if any(k in subject for k in ("毛利",)) :
        slots["metrics"].append("毛利")

    # 维度识别
    if any(k in subject for k in ("区域", "大区", "地区")):
        slots["dimensions"].append("区域")
    if any(k in subject for k in ("品类", "类目", "分类")):
        slots["dimensions"].append("品类")
    if any(k in subject for k in ("渠道",)) :
        slots["dimensions"].append("渠道")

    # 过滤识别（只识别显式维度值过滤，阈值走数值过滤分支）
    # 命中大区名时隐含「区域」维度——「华东区的成交总额」虽不含「区域」二字，
    # 但既然按大区切片，维度列表里就要有区域，否则 slots 断言会落空。
    for region in ("华东", "华北", "华南", "西南", "西北", "东北"):
        if region in subject:
            slots["filters"].append({"field": "区域", "value": region})
            if "区域" not in slots["dimensions"]:
                slots["dimensions"].append("区域")
    for ch in ("线上", "线下", "app", "小程序", "门店"):
        if ch in subject:
            slots["filters"].append({"field": "渠道", "value": ch})
            if "渠道" not in slots["dimensions"]:
                slots["dimensions"].append("渠道")

    # 时间识别（文字描述，不做绝对日期换算）
    for kw in ("上个月", "上月", "上季度", "上周", "昨天", "今天", "本周", "上周",
               "今年", "去年", "近30天", "近7天", "近三个月", "近三个月"):
        if kw in subject:
            slots["time_range"] = kw
            break

    # FR-A-3：排序与 TopN
    topn: int | None = None
    for kw in _MOCK_TOPN_KEYWORDS:
        if kw in subject:
            # 从关键词里取数字
            for digit in ("10", "5", "3"):
                if digit in kw:
                    topn = int(digit)
                    break
            if topn is None:
                topn = 5
            break
    if topn is not None:
        slots["top_n"] = topn
        slots["order_by"] = {"field": slots["metrics"][0] if slots["metrics"] else "", "direction": "desc"}
    elif any(k in subject for k in _MOCK_RANK_KEYWORDS):
        slots["order_by"] = {"field": slots["metrics"][0] if slots["metrics"] else "", "direction": "desc"}

    # FR-A-4：数值过滤（阈值不被当作维度取值）
    # 只在未命中维度过滤时把阈值单独标出来，避免与维度值混淆
    numeric_filters: list[dict[str, Any]] = []
    for kw in _MOCK_NUMERIC_GT:
        if kw in subject:
            numeric_filters.append({"field": slots["metrics"][0] if slots["metrics"] else "", "op": ">", "value": kw})
            break
    for kw in _MOCK_NUMERIC_LT:
        if kw in subject:
            numeric_filters.append({"field": slots["metrics"][0] if slots["metrics"] else "", "op": "<", "value": kw})
            break
    if numeric_filters:
        slots["filters"].extend(numeric_filters)

    # FR-A-1/A-2：多轮改写。命中追问关键词时填 rewritten_question。
    # 桩模式无法真正补全，用「[改写] 前缀 + 原问句」标注改写已发生，
    # 让前端阶段时间线可观测。切到真实模型后由模型自行补全。
    rewritten: str | None = None
    for kw in _MOCK_REWRITE_KEYWORDS:
        if kw in subject:
            rewritten = f"[桩改写]{subject}"
            break

    # 域外与归因先于取数判定：两者都会命中指标词（「预测下个月销量」含「销量」，
    # 「为什么销售额下降」含「销售额」），若放在后面就会被 query 吃掉，
    # 下游照旧生成一条语法正确但与问题无关的 SQL。
    intent = "query"
    reason = "桩实现按关键词填充槽位与改写标记"
    out_kw = next((k for k in _MOCK_OUT_OF_SCOPE_KEYWORDS if k in subject), None)
    att_kw = next((k for k in _MOCK_ATTRIBUTION_KEYWORDS if k in subject), None)
    if out_kw:
        intent = "out_of_scope"
        reason = f"桩实现命中域外关键词「{out_kw}」，本数据集不提供该问题所需的数据"
        rewritten = None
    elif att_kw:
        # 槽位保留：归因终态要用指标与时间范围拼替代问法
        intent = "attribution"
        reason = f"桩实现命中归因关键词「{att_kw}」，一次聚合查询给不出原因"
        rewritten = None

    return {
        "intent": intent,
        "reason": reason,
        "rewritten_question": rewritten,
        "slots": slots,
    }


def _mock_json(tag: str, prompt: str, schema: type[T]) -> T:
    name = schema.__name__
    if name == "RouteDecision":
        payload: dict[str, Any] = _mock_route_decision(prompt)
    elif name == "SqlDraft":
        payload = {
            "sql": _mock_text("generate", prompt),
            "reasoning": "桩实现按问题关键词选择预置语句，仅供链路冒烟",
            "metric_used": [],
        }
    elif name == "ChartSpec":
        subject = _mock_subject(prompt)
        if any(k in subject for k in ("趋势", "走势", "每天", "变化")):
            chart_type = "line"
        elif any(k in subject for k in ("占比", "构成", "比例", "分布")):
            chart_type = "pie"
        else:
            chart_type = "bar"
        payload = {
            "chart_type": chart_type,
            "x_field": None,
            "y_fields": [],
            "series_field": None,
            "title": "桩实现输出的图表",
            "reason": "字段映射交由规则兜底填充",
        }
    elif name == "Insight":
        payload = {
            "conclusion": "桩实现未生成真实结论。配置 LLM_API_KEY 后重新提问可得到模型解读。",
            "highlights": [],
            "caveats": [],
        }
    else:  # pragma: no cover - 兜底
        payload = {}
    return schema.model_validate(payload)
