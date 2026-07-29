"""消息总线（Message Bus）。

把「消息来源渠道」与「Agent 主循环」解耦：各渠道适配器（飞书 / QQ / Web /
CLI 等）把收到的用户消息封装成 :class:`InboundMessage` 投递进
``inbound_queue``，Agent 处理完再把回复封装成 :class:`OutboundMessage` 投递
进 ``outbound_queue``，由对应渠道的发送器取走下发。

整个模块只依赖标准库 ``asyncio`` 与 ``dataclasses``，不引入任何第三方依赖，
便于在任意渠道适配器中复用。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

import asyncio

if TYPE_CHECKING:
    from reminders.models import DeliveryResult


@dataclass
class ImageRef:
    """一张已落盘图片的引用，用于在消息总线上传递图片而不内联字节。

    图片字节落盘到 ``ImageStore`` 指定目录，总线只搬运轻量的引用
    （id / 路径 / 类型），真正读字节生成 base64 是在调用视觉模型时按需进行。
    """

    id: str                       # 全局唯一图片标识（uuid4 hex）
    path: str                     # 落盘绝对路径：<sessions_dir>/<safe_key>_images/<id>.<ext>
    mime: str = "image/png"       # MIME 类型，如 image/png / image/jpeg


@dataclass
class InboundMessage:
    """入站消息：某渠道上用户发来的原始消息。"""

    channel: str                  # 来源渠道名称，如 "feishu" / "qq" / "web" / "cli"
    sender_id: str                # 发送者标识
    chat_id: str                  # 会话标识（群聊或私聊）
    content: str                  # 消息正文
    raw: Optional[dict] = None    # 原始消息（各渠道 SDK 的原始结构），调试用
    images: Optional[List[ImageRef]] = None  # 随消息附带的图片引用（无则为 None）


@dataclass
class OutboundMessage:
    """出站消息：Agent 处理完、准备下发到某渠道的回复。"""

    channel: str                  # 目标渠道
    chat_id: str                  # 目标会话
    content: str                  # 回复正文
    reply_to: Optional[str] = None  # 引用的消息 ID（可选，用于上下文关联）
    # 标记该回包是否已由流式事件（StreamEvent）完整覆盖。网页渠道在收到
    # streamed=True 的回包时直接跳过，避免与流式事件里的思考/最终回答重复显示。
    streamed: bool = False
    # Agent 本轮生成、准备随回复发送的图片引用。渠道自行决定如何呈现：
    # 飞书会上传后发送 image 消息；Web 已由流事件展示；CLI 可忽略。
    images: Optional[List[ImageRef]] = None
    # Optional acknowledgement for callers that need the channel's API result.
    # Ordinary conversation messages leave it as None and remain fire-and-forget.
    delivery_future: "asyncio.Future[DeliveryResult] | None" = None


@dataclass
class StreamEvent:
    """流式事件：Agent 在推理/执行过程中产生的「逐步」事件。

    与 ``OutboundMessage``（仅承载最终回复）不同，StreamEvent 用于把
    Agent 的**思考过程、工具调用、工具结果、逐字生成的最终回答**实时推给
    支持流式展示的渠道（目前为网页渠道）。每个事件是一个自由结构的
    ``dict``，常见类型见 ``agent.loop`` 中的 emit 约定：

        {"type": "thinking",    "content": "<推理增量>"}
        {"type": "token",       "content": "<最终回答增量>"}
        {"type": "tool_call",   "name": ..., "args": ...}
        {"type": "tool_result", "name": ..., "content": ...}
        {"type": "done",        "content": "<最终完整回答>"}

    客户端（网页）据此渐进式渲染，无需等待整轮推理结束。
    """

    channel: str                  # 来源渠道（用于路由，仅 web 消费）
    chat_id: str                  # 目标会话（网页为 conn_id）
    event: dict                   # 事件体


class MessageBus:
    """基于 ``asyncio.Queue`` 的双通道消息总线。

    设计要点：

    - 两个独立的队列分别承载入站 / 出站流量，互不阻塞。
    - ``publish_*`` 是生产者接口（投递消息），``consume_*`` 是消费者接口。
    - ``consume_*`` 内部调用 ``await queue.get()``，在队列为空时会**自动挂起
      等待**，直到有消息抵达，因此调用方可以用 ``while True: msg = await
      bus.consume_inbound()`` 直接写消费循环，无需轮询。
    """

    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        # 流式事件队列：Agent 推理过程中的逐步事件（思考/工具/逐字回答）经此
        # 实时投递，由 Gateway._dispatch_stream 转发给对应渠道（目前仅网页）。
        self.stream_queue: asyncio.Queue[StreamEvent] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """投递一条入站消息到 inbound_queue（生产者侧）。"""
        await self.inbound_queue.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """从 inbound_queue 取出一条入站消息，队列为空时自动等待。"""
        return await self.inbound_queue.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """投递一条出站消息到 outbound_queue（生产者侧）。"""
        await self.outbound_queue.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """从 outbound_queue 取出一条出站消息，队列为空时自动等待。"""
        return await self.outbound_queue.get()

    async def publish_stream(self, event: StreamEvent) -> None:
        """投递一条流式事件到 stream_queue（生产者侧）。"""
        await self.stream_queue.put(event)

    async def consume_stream(self) -> StreamEvent:
        """从 stream_queue 取出一条流式事件，队列为空时自动等待。"""
        return await self.stream_queue.get()
