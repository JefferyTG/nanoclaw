"""OpenAI 兼容 Provider。

面向所有「OpenAI 接口兼容」的服务实现统一的 ``LLMProvider``，包括 OpenAI 官方、
硅基流动（SiliconFlow）、Kimi（Moonshot）、DeepSeek、以及各类自建的 OpenAI 兼容
网关。只要对方提供 ``/chat/completions`` 兼容端点，配好 ``base_url`` 即可复用。

几个关键的工程细节：

1. tool_choice 的条件传递
   只有在确实传入了 ``tools`` 时，才把 ``tools`` 和 ``tool_choice="auto"`` 放进请求。
   若无工具（例如仅让模型生成一段摘要），绝不能传 ``tool_choice``，否则部分兼容
   实现（如硅基流动）会报错：
   "The tools parameter must be specified when tool_choice is utilized"。
   这里用 dict 动态拼装请求参数再 ``**kwargs`` 展开，规避该问题。

2. reasoning_content 的双位置容错
   支持「思考」的模型（Kimi-K2.5、DeepSeek-R1 等）返回的推理过程，位置并不统一：
   有的挂在单个 tool_call 上，有的挂在 message 顶层。两处都用 getattr 容错读取，
   优先取 tool_call 自带的，缺失时回退到 message 级别的推理内容。

3. 异常不外抛 + 瞬时错误重试
   任何 API 调用异常都被捕获并转成 ``LLMResponse(content=错误信息,
   finish_reason="error")``，保证 Agent 主循环不因单次请求失败而崩溃。
   对于会自行恢复的瞬时错误（5xx / 429 限流 / 连接超时），会做最多 3 次
   指数退避重试（1s / 2s / 4s）；401 / 400 / 404 等不可恢复错误直接失败，
   不做无谓重试。

4. reasoning_effort / thinking_budget 参数传递
   在 ``__init__`` 中接收 ``reasoning_effort`` 和 ``thinking_budget`` 并保存为
   实例属性。``chat()`` 和 ``chat_stream()`` 的 ``request_kwargs`` 中条件性地
   加入这两个参数：
   - ``reasoning_effort`` 默认为 "high"；只有显式配成空字符串 ``""`` 时才不传。
   - ``thinking_budget`` 默认为 None（不传）；只有配了具体整数才传。
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from providers.usage import parse_prompt_cache_usage, usage_to_dict

logger = logging.getLogger("nanoclaw.llm")

# 触发重试的瞬时错误状态码（服务端过载 / 限流 / 网关错误），这类错误通常会自行恢复
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
# 指数退避基础间隔（秒）：第 1/2 次失败后分别等待 1s / 2s / 4s
_BACKOFF = (1.0, 2.0, 4.0)


def _stream_usage_option_unsupported(exc: Exception) -> bool:
    """Detect a compatible server/SDK that rejects include_usage explicitly."""
    status = getattr(exc, "status_code", None)
    text = (str(exc) + " " + json.dumps(
        getattr(exc, "body", None), ensure_ascii=False, default=str
    )).lower()
    names_option = "stream_options" in text or "include_usage" in text
    return names_option and (status in (400, 422, None))


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容接口的通用 Provider。

    典型用法::

        provider = OpenAICompatProvider(
            api_key="sk-xxx",
            base_url="https://api.siliconflow.cn/v1",
            model="deepseek-ai/DeepSeek-V3",
        )
        resp = await provider.chat(messages, tools=registry.get_definitions())

    参数：
        api_key: API 密钥。
        base_url: OpenAI 兼容接口的 base_url。
        model: 默认模型名。
        reasoning_effort: 思考强度（none / minimal / low / medium / high / xhigh / max）。
            默认 "high"；显式传空字符串 "" 时不传该参数。
        thinking_budget: 思考 token 预算（整数）。默认 None 不传。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: Optional[str] = "high",
        thinking_budget: Optional[int] = None,
    ):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.thinking_budget = thinking_budget
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def _add_reasoning_params(self, kwargs: Dict) -> None:
        """条件性地向 request_kwargs 添加 reasoning_effort 和 thinking_budget。

        - reasoning_effort 默认为 "high"；只有显式配成空字符串 "" 时才不传。
        - thinking_budget 默认 None（不传）；只有配了具体整数才传。
        """
        if self.reasoning_effort and self.reasoning_effort.strip():
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.thinking_budget is not None:
            kwargs["thinking_budget"] = self.thinking_budget

    async def chat(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_budget: Optional[int] = None,
    ) -> LLMResponse:
        # 动态拼装请求参数：仅在有工具时才带 tools / tool_choice，
        # 避免无工具场景下传 tool_choice 触发兼容实现报错。
        request_kwargs: Dict = {
            "model": model or self.model,
            "messages": messages,
        }
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"

        # 推理参数：优先使用方法级参数，回退到实例属性
        re = reasoning_effort if reasoning_effort is not None else getattr(self, "reasoning_effort", "high")
        tb = thinking_budget if thinking_budget is not None else getattr(self, "thinking_budget", None)
        if re and re.strip():
            request_kwargs["reasoning_effort"] = re
        if tb is not None:
            request_kwargs["thinking_budget"] = tb

        # 对瞬时错误（5xx / 429 / 连接超时）做有限次重试 + 指数退避；
        # 不可恢复的错（401 / 400 / 404 等）直接失败，不浪费重试。
        last_exc: Optional[Exception] = None
        completion = None
        for attempt in range(_MAX_RETRIES):
            try:
                completion = await self._client.chat.completions.create(**request_kwargs)
                break
            except (APIConnectionError, APITimeoutError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    logger.warning("LLM 连接/超时错误，准备第 %d 次重试：%s", attempt + 1, exc)
                    await asyncio.sleep(_BACKOFF[attempt])
                    continue
                break
            except APIStatusError as exc:
                last_exc = exc
                status = getattr(exc, "status_code", None)
                if status in _RETRYABLE_STATUS and attempt < _MAX_RETRIES - 1:
                    logger.warning("LLM 返回瞬时错误 %s，准备第 %d 次重试", status, attempt + 1)
                    await asyncio.sleep(_BACKOFF[attempt])
                    continue
                break  # 4xx 等不可恢复错误，直接失败
            except Exception as exc:  # noqa: BLE001 - 非预期异常也兜底，不让主循环崩
                last_exc = exc
                break

        if completion is None:
            return LLMResponse(
                content=f"LLM 调用失败：{last_exc}",
                finish_reason="error",
            )

        choice = completion.choices[0]
        message = choice.message

        # message 顶层的推理内容（部分模型把 reasoning 挂在这里，而非 tool_call 上）
        message_reasoning = getattr(message, "reasoning_content", None)

        # 转换 tool_calls -> ToolCallRequest
        tool_calls: List[ToolCallRequest] = []
        for tc in getattr(message, "tool_calls", None) or []:
            # 参数是 JSON 字符串，容错解析成 dict
            raw_args = tc.function.arguments
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            # 优先取 tool_call 自带的 reasoning_content，缺失回退到 message 级别
            tc_reasoning = getattr(tc, "reasoning_content", None) or message_reasoning

            tool_calls.append(
                ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=arguments,
                    reasoning_content=tc_reasoning,
                )
            )

        # 提取 usage（转成普通 dict，兼容不同 SDK 版本）
        usage = usage_to_dict(getattr(completion, "usage", None))

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=message_reasoning,
            cache_usage=parse_prompt_cache_usage(usage),
        )

    async def chat_stream(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_budget: Optional[int] = None,
    ):
        """逐字流式对话（``stream=True``）。

        增量 yield 两类事件，最后 yield 一个 ``done`` 携带完整 ``LLMResponse``：

            {"type": "reasoning", "content": <推理增量>}   # 部分模型（R1/Kimi）的思考
            {"type": "token",    "content": <文本增量>}      # 最终回答的逐字内容
            {"type": "done",     "response": <LLMResponse>}  # 收尾：含结构化结果

        对工具调用场景，按 OpenAI SSE 规范从 ``delta.tool_calls`` 增量累积出
        完整的 ``tool_calls``（含 id / name / arguments 拼接），保证 ``done``
        返回的 ``LLMResponse`` 与 ``chat()`` 行为一致（可被上层当作普通工具轮处理）。

        瞬时错误（5xx / 429 / 连接超时）在「建立连接」阶段做有限次重试；
        流读取中途异常则尽量把已拿到的内容交付，无法交付时返回 error 响应。
        """
        request_kwargs: Dict = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            # OpenAI-compatible services commonly omit usage from SSE unless
            # explicitly requested.  Providers that do not implement it leave
            # cache_usage unavailable rather than fabricating a cache miss.
            "stream_options": {"include_usage": True},
        }
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"

        # 推理参数：优先使用方法级参数，回退到实例属性
        re = reasoning_effort if reasoning_effort is not None else getattr(self, "reasoning_effort", "high")
        tb = thinking_budget if thinking_budget is not None else getattr(self, "thinking_budget", None)
        if re and re.strip():
            request_kwargs["reasoning_effort"] = re
        if tb is not None:
            request_kwargs["thinking_budget"] = tb

        # —— 建立连接（带瞬时错误重试）——
        stream = None
        last_exc: Optional[Exception] = None
        usage_option_enabled = True
        for attempt in range(_MAX_RETRIES):
            try:
                stream = await self._client.chat.completions.create(**request_kwargs)
                break
            except (APIConnectionError, APITimeoutError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    logger.warning("LLM 流式连接错误，准备第 %d 次重试：%s", attempt + 1, exc)
                    await asyncio.sleep(_BACKOFF[attempt])
                    continue
                break
            except APIStatusError as exc:
                last_exc = exc
                status = getattr(exc, "status_code", None)
                if usage_option_enabled and _stream_usage_option_unsupported(exc):
                    # Preserve streaming compatibility, but the final response
                    # will correctly report cache usage as unavailable.
                    request_kwargs.pop("stream_options", None)
                    usage_option_enabled = False
                    logger.info("LLM 不支持流式 usage，已降级为无 usage 流式请求")
                    continue
                if status in _RETRYABLE_STATUS and attempt < _MAX_RETRIES - 1:
                    logger.warning("LLM 流式返回瞬时错误 %s，准备第 %d 次重试", status, attempt + 1)
                    await asyncio.sleep(_BACKOFF[attempt])
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - 非预期异常也兜底
                last_exc = exc
                if usage_option_enabled and _stream_usage_option_unsupported(exc):
                    request_kwargs.pop("stream_options", None)
                    usage_option_enabled = False
                    logger.info("LLM SDK 不支持流式 usage，已降级为无 usage 流式请求")
                    continue
                break

        if stream is None:
            yield {
                "type": "done",
                "response": LLMResponse(
                    content=f"LLM 调用失败：{last_exc}", finish_reason="error"
                ),
            }
            return

        # —— 增量读取并转发 ——
        content_buf: List[str] = []
        reasoning_buf: List[str] = []
        # 按 delta.tool_calls[].index 累积：idx -> {id, name, arguments}
        tool_calls_acc: Dict[int, Dict] = {}
        finish_reason = "stop"
        usage: Dict = {}

        try:
            async for chunk in stream:
                # OpenAI emits usage in a final empty-choice chunk, but some
                # compatible servers attach it to a normal chunk instead.
                chunk_usage = usage_to_dict(getattr(chunk, "usage", None))
                if chunk_usage:
                    usage = chunk_usage
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # 推理内容增量（思考过程）
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    reasoning_buf.append(rc)
                    yield {"type": "reasoning", "content": rc}

                # 最终回答文本增量
                ct = getattr(delta, "content", None)
                if ct:
                    content_buf.append(ct)
                    yield {"type": "token", "content": ct}

                # 工具调用增量（可能跨多个 chunk 拼接）
                for tc in (getattr(delta, "tool_calls", None) or []):
                    idx = tc.index if tc.index is not None else 0
                    acc = tool_calls_acc.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        acc["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if fn.name:
                            acc["name"] += fn.name
                        if fn.arguments:
                            acc["arguments"] += fn.arguments

                fr = chunk.choices[0].finish_reason
                if fr:
                    finish_reason = fr
        except Exception as exc:  # noqa: BLE001 - 流读取中断兜底
            logger.warning("LLM 流式读取中断：%s", exc)
            if not content_buf and not tool_calls_acc:
                yield {
                    "type": "done",
                    "response": LLMResponse(
                        content=f"LLM 流式失败：{exc}", finish_reason="error"
                    ),
                }
                return

        # —— 组装完整响应 ——
        msg_reasoning = "".join(reasoning_buf) or None
        tool_calls: List[ToolCallRequest] = []
        for idx in sorted(tool_calls_acc):
            acc = tool_calls_acc[idx]
            try:
                arguments = json.loads(acc["arguments"]) if acc["arguments"] else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append(
                ToolCallRequest(
                    id=acc["id"],
                    name=acc["name"],
                    arguments=arguments,
                    # 工具轮推理：沿用 message 级推理（与 chat() 行为一致）
                    reasoning_content=msg_reasoning,
                )
            )

        yield {
            "type": "done",
            "response": LLMResponse(
                content="".join(content_buf) or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                reasoning_content=msg_reasoning,
                cache_usage=parse_prompt_cache_usage(usage),
            ),
        }