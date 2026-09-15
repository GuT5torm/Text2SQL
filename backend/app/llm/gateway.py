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
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings

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

    def totals(self) -> dict[str, Any]:
        return {
            "calls": len(self.usage),
            "failed": sum(1 for u in self.usage if not u.ok),
            "fallback_calls": sum(1 for u in self.usage if u.fallback),
            "prompt_tokens": sum(u.prompt_tokens for u in self.usage),
            "completion_tokens": sum(u.completion_tokens for u in self.usage),
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

_MOCK_SQL_BY_KEYWORD: list[tuple[tuple[str, ...], str]] = [
    (
        ("退款", "退货"),
        """SELECT o.region AS 大区,
       ROUND(COUNT(DISTINCT r.order_id) * 1.0 / COUNT(DISTINCT o.id), 4) AS 退款率
FROM orders o
LEFT JOIN refunds r ON r.order_id = o.id
GROUP BY o.region
ORDER BY 退款率 DESC
LIMIT 1000""",
    ),
    (
        ("品类", "类目", "分类"),
        """SELECT c.name AS 品类,
       ROUND(SUM(oi.quantity * oi.unit_price), 2) AS 成交总额
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
JOIN categories c ON c.id = p.category_id
WHERE o.status NOT IN ('cancelled', 'pending')
GROUP BY c.name
ORDER BY 成交总额 DESC
LIMIT 1000""",
    ),
    (
        ("渠道",),
        """SELECT o.channel AS 渠道,
       COUNT(DISTINCT o.id) AS 订单量,
       ROUND(SUM(oi.quantity * oi.unit_price), 2) AS 成交总额
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
WHERE o.status NOT IN ('cancelled', 'pending')
GROUP BY o.channel
ORDER BY 订单量 DESC
LIMIT 1000""",
    ),
    (
        ("区域", "大区", "地区"),
        """SELECT o.region AS 大区,
       ROUND(SUM(oi.quantity * oi.unit_price), 2) AS 成交总额
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
WHERE o.status NOT IN ('cancelled', 'pending')
GROUP BY o.region
ORDER BY 成交总额 DESC
LIMIT 1000""",
    ),
]

_MOCK_SQL_DEFAULT = """SELECT strftime('%Y-%m', o.created_at) AS 月份,
       ROUND(SUM(oi.quantity * oi.unit_price), 2) AS 成交总额
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
WHERE o.status NOT IN ('cancelled', 'pending')
GROUP BY 月份
ORDER BY 月份
LIMIT 1000"""


_QUESTION_HEADING_RE = re.compile(r"##\s*[^\n#]*问题[^\n]*")


def _mock_subject(prompt: str) -> str:
    """桩实现的关键词匹配对象。

    直接对整段 prompt 做匹配会出错：语义层上下文里列出了全部指标名，
    「退款率」几乎出现在每个 prompt 中，于是任何问题都会命中退款分支。
    因此只取最后一个「## …问题」小节之后的内容作为匹配对象。
    """
    matches = list(_QUESTION_HEADING_RE.finditer(prompt))
    if not matches:
        return prompt
    return prompt[matches[-1].end() :].strip() or prompt


def _mock_text(tag: str, prompt: str) -> str:
    if tag in {"generate", "fix"}:
        subject = _mock_subject(prompt)
        for keywords, sql in _MOCK_SQL_BY_KEYWORD:
            if any(k in subject for k in keywords):
                return sql
        return _MOCK_SQL_DEFAULT
    if tag == "route":
        return "{}"
    if tag == "summarize":
        return "{}"
    if tag == "visualize":
        return "{}"
    return ""


def _mock_json(tag: str, prompt: str, schema: type[T]) -> T:
    name = schema.__name__
    if name == "RouteDecision":
        payload: dict[str, Any] = {"intent": "query", "reason": "桩实现固定返回取数意图"}
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
