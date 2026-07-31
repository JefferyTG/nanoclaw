"""Gateway：把消息总线、渠道与 Agent 串成一个可运行的调度器。

Gateway 是「渠道无关」的运行时核心。它不关心消息具体来自 CLI 还是飞书，
只负责三件事：

1. 从 bus 取出入站消息，按 ``session_key`` 分配给对应的 ``AgentLoop`` 处理；
2. 把 Agent 的回复封装成 ``OutboundMessage`` 投回 bus；
3. 把出站消息按渠道名分发给对应 ``Channel`` 下发。

多会话由 ``_agents`` 字典按 ``session_key`` 缓存实现：同一用户
（同 ``channel + sender_id``）复用同一个 ``AgentLoop`` 实例，天然支持
多用户 / 多会话并发，且每个会话的历史、压缩状态各自独立。
"""

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable, Dict, List
from zoneinfo import ZoneInfo

from config import validate_iana_timezone

from bus.queue import MessageBus, InboundMessage, OutboundMessage, StreamEvent
from channels.base import Channel
from agent.identity import IdentityBootstrapper
from agent.loop import AgentLoop

if TYPE_CHECKING:
    from reminders.models import DeliveryResult


def _delivery_result(
    *,
    success: bool,
    retryable: bool = False,
    code: int | str | None = None,
    provider_message_id: str | None = None,
    message: str | None = None,
) -> "DeliveryResult":
    """Create the shared DTO lazily so normal startup stays loosely coupled."""
    from reminders.models import DeliveryResult

    return DeliveryResult(
        success=success,
        retryable=retryable,
        code=code,
        provider_message_id=provider_message_id,
        message=message,
    )


def _timestamp_prefix(timezone: str, now=None) -> str:
    """Return the "[YYYY-MM-DD HH:MM]" prefix for ``now`` in an IANA timezone.

    Emits only date and minute (no seconds / weekday / UTC offset) so every
    inbound user turn carries a cheap wall-clock anchor.  The prefix is built
    once when the message arrives and then stays fixed in the appended history,
    leaving the session-stable System Prompt (and therefore the Prompt Cache
    prefix) untouched.
    """
    selected = validate_iana_timezone(timezone)
    instant = now if now is not None else datetime.now(UTC)
    if getattr(instant, "tzinfo", None) is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(ZoneInfo(selected)).strftime("[%Y-%m-%d %H:%M]")


class Gateway:
    """消息网关：驱动所有渠道与 Agent 协同工作。"""

    def __init__(
        self,
        bus: MessageBus,
        channels: List[Channel],
        agent_factory: Callable[[str], AgentLoop],
        identity_bootstrapper: IdentityBootstrapper | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self.bus = bus
        self.channels = channels
        self.agent_factory = agent_factory
        self.identity_bootstrapper = identity_bootstrapper
        # 实例默认时区：注入到每轮用户消息的时间戳前缀使用它（配置已验证）
        self.timezone = validate_iana_timezone(timezone)
        # 按渠道名索引，出站分发时 O(1) 查找
        self._channel_map: Dict[str, Channel] = {ch.name: ch for ch in channels}
        # 按 session_key 缓存 Agent 实例（同一会话复用，互不干扰）
        self._agents: Dict[str, AgentLoop] = {}
        # 每个会话一把锁：同一会话串行、不同会话并发，避免「一条消息卡死」拖垮全局入站
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # 保护 _agents / _session_locks 的并发创建
        self._reg_lock = asyncio.Lock()
        # _process_inbound 为每条消息创建的在途任务。显式登记后，SIGTERM
        # 关闭可以先取消并等待它们，让子 Agent / 工具的 finally 正常回收资源。
        self._inflight_tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """并发启动所有渠道、入站消费循环与出站分发循环。"""
        await asyncio.gather(
            *(ch.start() for ch in self.channels),
            self._process_inbound(),
            self._dispatch_outbound(),
            self._dispatch_stream(),
        )

    async def _process_inbound(self) -> None:
        """入站消费循环：每条消息起一个独立任务处理，绝不在循环里 await 长耗时。

        这样「某条消息卡在慢工具/慢模型」不会阻塞入站循环，其他会话、以及
        同一会话的后续消息（在各自锁上排队）都能得到处理。
        """
        while True:
            msg = await self.bus.consume_inbound()
            session_key = f"{msg.channel}:{msg.sender_id}"

            # 取/建该会话的锁（保护并发创建，避免同会话首条消息竞态）
            async with self._reg_lock:
                lock = self._session_locks.get(session_key)
                if lock is None:
                    lock = asyncio.Lock()
                    self._session_locks[session_key] = lock

            task = asyncio.create_task(self._handle_one(msg, session_key, lock))
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)

    async def _handle_one(self, msg: InboundMessage, session_key: str, lock: asyncio.Lock) -> None:
        """处理单条入站消息：取/建 Agent → 持锁跑 → 回投出站队列。

        同一 ``session_key`` 的多个任务会竞争同一把锁，从而天然串行；
        不同会话的锁互不影响，可并发执行。锁用 ``async with`` 保证异常时释放。
        """
        stream_sink = None
        # 同一会话串行跑（持锁）；首次人设引导也在锁内完成，避免连续两条消息
        # 竞态地都被当成“第一条”。不同会话还会由 Bootstrapper 的实例级锁协调。
        async with lock:
            if self.identity_bootstrapper is not None:
                bootstrap_reply = await self.identity_bootstrapper.handle(
                    session_key, msg.content
                )
                if bootstrap_reply is not None:
                    await self.outbound_safe(bootstrap_reply, msg, None)
                    return

            # 人设就绪后才惰性创建 Agent，避免把首次引导文本写入会话历史或
            # 在缺少人设时发起模型请求。
            async with self._reg_lock:
                agent = self._agents.get(session_key)
                if agent is None:
                    agent = self.agent_factory(session_key)
                    self._agents[session_key] = agent

            # 每轮用户消息在进入 AgentLoop 之前，注入当前时间戳前缀（实例默认时区）。
            # 时间戳在消息进入时生成一次，整体作为本轮模型输入并原样持久化进会话
            # 历史（追加式、固定不变）；System Prompt 保持会话级稳定快照，不影响
            # Prompt Cache 前缀命中。图片消息的默认文本（如「请分析这张图片。」）
            # 已由渠道写入 msg.content，同样会被前缀。
            content = f"{_timestamp_prefix(self.timezone)} {msg.content}"

            # 仅网页渠道挂载流式事件接收方：把 Agent 的逐步事件实时推给网页端。
            stream_sink = self._make_stream_sink(msg) if msg.channel == "web" else None
            try:
                # 把随消息附带的图片引用一并交给 Agent；纯文本消息 images 为 None
                reply = await agent.run(
                    content, images=msg.images, stream_sink=stream_sink
                )
            except Exception as exc:
                reply = f"⚠️ 处理消息时出错：{exc}"
                if stream_sink is not None:
                    try:
                        await stream_sink({"type": "done", "content": reply})
                    except Exception:
                        pass

        reply_images = []
        image_store = getattr(agent, "image_store", None)
        if image_store is not None:
            for image_id in getattr(agent, "last_generated_image_ids", []):
                try:
                    ref = image_store.resolve(session_key, image_id)
                except Exception:  # noqa: BLE001 - 图片回包失败不能影响文字回复
                    ref = None
                if ref is not None:
                    reply_images.append(ref)

        await self.outbound_safe(
            reply, msg, stream_sink, images=reply_images or None
        )

    async def outbound_safe(
        self, reply: str, msg: InboundMessage, stream_sink, images=None
    ) -> None:
        """把回复安全地回投出站队列（锁外执行，避免持锁期间阻塞分发）。"""
        try:
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=reply,
                reply_to=None,
                streamed=(stream_sink is not None),
                images=images,
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 出站投递失败：{exc}")

    def _make_stream_sink(self, msg: InboundMessage):
        """为一条入站消息构造流式事件 sink（发布到总线流的 stream_queue）。"""
        async def sink(event: dict) -> None:
            await self.bus.publish_stream(StreamEvent(
                channel=msg.channel,
                chat_id=msg.chat_id,
                event=event,
            ))
        return sink

    async def _dispatch_stream(self) -> None:
        """流式事件分发循环：取 StreamEvent → 按渠道路由给对应 Channel。

        目前仅 web 渠道消费流式事件；其他渠道的事件在此被忽略（不阻塞）。
        """
        while True:
            ev = await self.bus.consume_stream()
            if ev.channel != "web":
                continue
            channel = self._channel_map.get("web")
            if channel is None or not hasattr(channel, "stream_event"):
                continue
            await channel.stream_event(ev.chat_id, ev.event)

    async def _dispatch_outbound(self) -> None:
        """出站分发循环：取回复 → 按渠道名找到 Channel → 下发。"""
        while True:
            msg = await self.bus.consume_outbound()
            channel = self._channel_map.get(msg.channel)
            if channel is None:
                # 找不到对应渠道就告警并丢弃，不阻塞分发循环
                print(f"⚠️ 出站消息找不到渠道 '{msg.channel}'，已丢弃")
                if msg.delivery_future is not None:
                    self._complete_delivery(
                        msg,
                        _delivery_result(
                            success=False,
                            code="channel_not_found",
                            message=f"channel not found: {msg.channel}",
                        ),
                    )
                continue
            try:
                result = await channel.send(msg)
            except asyncio.CancelledError:
                if msg.delivery_future is not None:
                    self._complete_delivery(
                        msg, _delivery_result(success=False, message="dispatch cancelled")
                    )
                raise
            except Exception as exc:  # noqa: BLE001 - one failure must not kill routing
                print(f"⚠️ 出站消息发送失败（{msg.channel}）：{exc}")
                if msg.delivery_future is not None:
                    self._complete_delivery(
                        msg, _delivery_result(success=False, message=str(exc))
                    )
                continue
            if msg.delivery_future is not None:
                self._complete_delivery(
                    msg,
                    result
                    or _delivery_result(
                        success=False, message="channel returned no delivery result"
                    ),
                )

    @staticmethod
    def _complete_delivery(
        msg: OutboundMessage, result: "DeliveryResult"
    ) -> None:
        """Resolve an optional acknowledgement at most once on every path."""
        future = msg.delivery_future
        if future is not None and not future.done():
            future.set_result(result)

    async def shutdown(self) -> None:
        """取消在途消息，停止所有渠道并清空 Agent 缓存。"""
        pending = list(self._inflight_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight_tasks.clear()
        for ch in self.channels:
            await ch.stop()
        self._agents.clear()
