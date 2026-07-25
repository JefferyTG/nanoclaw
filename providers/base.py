"""LLM Provider 抽象层。

本模块定义 Agent 与具体大模型之间的解耦契约。无论后端是 OpenAI、Kimi、
DeepSeek 还是本地模型，只要实现 ``LLMProvider`` 并给出 ``chat`` 方法，
上层 Agent 就能无差别调用。

包含三部分：

- ``ToolCallRequest``：模型请求调用某个工具时的结构化数据，保存 id、名称、
  参数，以及（部分支持「思考」的模型返回的）推理过程 ``reasoning_content``。
- ``LLMResponse``：一次 ``chat`` 的完整返回，统一承载文本内容与工具调用，
  并提供 ``has_tool_calls`` 便捷判断。
- ``LLMProvider``：Provider 抽象基类，强制子类实现 ``chat``。

关于 ``reasoning_content``：
部分支持「思考 / 推理」能力的模型（如 Kimi-K2.5、DeepSeek-R1）在返回
``tool_calls`` 时，会附带一段模型内部的推理过程。这段文本对调试、审计、
或向用户展示「模型为什么决定调用这个工具」很有价值，因此单独存下来，
而非丢弃。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ToolCallRequest:
    """模型请求调用一个工具的请求。

    属性：
        id: 工具调用的唯一标识，用于把后续的「工具执行结果」回填给模型。
        name: 要调用的工具名（对应 Tool.name / function 定义中的 name）。
        arguments: 工具入参，已解析为字典（模型原始返回通常是 JSON 字符串，
            由具体 Provider 负责解析为本字段）。
        reasoning_content: 模型在发起本次调用时的推理过程（思考内容）。
            仅部分支持「思考」的模型（如 Kimi-K2.5、DeepSeek-R1）会返回，
            不支持时为 None。
    """

    id: str
    name: str
    arguments: Dict
    reasoning_content: Optional[str] = None


@dataclass
class LLMResponse:
    """一次 LLM 对话返回的完整结果。

    属性：
        content: 模型生成的文本回复；若本次只产生了工具调用而无文本，则为 None。
        tool_calls: 模型请求的工具调用列表（可能为空）。
        finish_reason: 结束原因，常见 "stop"（自然结束）、"tool_calls"（因调用工具结束）。
        usage: token 用量统计等元信息，结构由具体 Provider 决定，默认空字典。
        reasoning_content: 模型在生成本次「最终文本回复」时的推理过程（思考内容）。
            仅部分支持「思考」的模型（如 Kimi-K2.5、DeepSeek-R1）会返回，不支持时为 None。
            注意：工具调用场景下的推理过程存放在各 ToolCallRequest.reasoning_content 上，
            本字段专门承载「无工具调用、直接回答」时挂在 message 顶层的推理内容。
    """

    content: Optional[str]
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict = field(default_factory=dict)
    reasoning_content: Optional[str] = None

    @property
    def has_tool_calls(self) -> bool:
        """是否有工具调用请求（非空即 True）。"""
        return bool(self.tool_calls)


class LLMProvider(ABC):
    """大模型 Provider 抽象基类。

    所有具体模型接入方都应继承本类并实现 ``chat``，使上层 Agent 与具体
    厂商 SDK 解耦。``chat`` 的入参与返回都使用本模块定义的通用结构，
    Provider 内部负责与各家 API 的协议做转换。
    """

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        """发起一次对话并返回统一结构的响应。

        参数：
            messages: 对话历史，格式与 OpenAI messages 一致
                （``[{"role": "user"/"assistant"/"system", "content": ...}]``）。
            tools: 可选，OpenAI function-calling 格式的工具定义列表，
                由 ``Tool.to_function_definition()`` 产出后透传。
            model: 可选，指定使用的模型名；未指定时由 Provider 决定默认模型。

        返回：
            ``LLMResponse``：统一封装的文本内容与工具调用。
        """
        ...

    async def chat_stream(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        model: Optional[str] = None,
    ):
        """流式对话（可选能力）。

        默认实现**包裹** ``chat``：一次性拿到完整响应后，把推理内容作为一条
        ``reasoning`` 事件、正文作为一条 ``token`` 事件、最后一条 ``done``
        事件 yield 出去。这样任何未重写本方法的 Provider 也天然「可流式」，
        只是没有逐字增量（整段作为一个 token）。

        子类（如 ``OpenAICompatProvider``）可重写为真正的逐字流式：
        增量 yield ``{"type": "reasoning"|"token", "content": <增量>}``，
        最后 yield ``{"type": "done", "response": <完整 LLMResponse>}``。

        yield 的事件字典约定见 ``bus.queue.StreamEvent``。
        """
        resp = await self.chat(messages, tools, model)
        if resp.reasoning_content:
            yield {"type": "reasoning", "content": resp.reasoning_content}
        if resp.content:
            yield {"type": "token", "content": resp.content}
        yield {"type": "done", "response": resp}
