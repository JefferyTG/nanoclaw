"""飞书渠道：通过飞书长连接（WebSocket）接收消息，经总线转发给 Agent，再把回复发回飞书。

要点：
- 使用 lark-oapi 的 ``WSClient`` 长连接模式，本地无需公网 IP / 回调域名；
- 事件回调运行在 SDK 内部线程，必须「只投总线、立即返回」，不能在其中做阻塞的
  LLM 调用（飞书事件处理有 3 秒超时）。真正的处理在 Gateway 的异步入站循环中完成；
- 跨线程投递用 ``run_coroutine_threadsafe`` 把 ``InboundMessage`` 送到主事件循环的 bus；
- 群聊仅在被 @ 时响应；私聊始终响应；
- 图片消息通过飞书鉴权资源接口下载，落入共享 ImageStore 后复用现有视觉链路；
- Agent 生成图片以结构化 ImageRef 随回复传入，上传为飞书 image 消息；
- 回复按 ``chat_id`` 下发（``receive_id_type=chat_id``），兼容群聊与私聊；
- 回复超过单条上限时自动分片发送。

会话模型：以 ``chat_id`` 作为会话键（``session_key = "feishu:<chat_id>:<序号>"``），
因此群聊天然共享一套会话、私聊每个会话独立；与 Gateway 的多会话缓存机制天然契合。

为支持在「同一个聊天框」里开多个会话（与 CLI 的 /new 对称），本渠道内置命令：
``/new`` 开新会话、``/sessions`` 列表、``/switch <n>`` 切换、``/clear`` 清当前会话。
命中命令时直接回复，不经过 Agent；普通消息则把 sender_id 改写为
``"<chat_id>:<当前序号>"``，Gateway 据此派生独立的 session_key。
"""

import asyncio
import io
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
)

from channels.base import Channel
from bus.queue import InboundMessage, OutboundMessage
from voice.asr.base import ASRError
from voice.media import MediaError, encode_to_opus
from voice.tts.base import TTSError

if TYPE_CHECKING:
    from reminders.models import DeliveryResult

logger = logging.getLogger("nanoclaw.feishu")

# 飞书单条文本消息长度上限约 20KB，这里按字符切分并留足余量
_MAX_CHUNK = 4000
# 飞书开放接口限频约 5 QPS，分片发送时片间稍作停顿避免被限流
_CHUNK_SLEEP = 0.25
_MAX_INBOUND_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_OUTBOUND_IMAGE_BYTES = 10 * 1024 * 1024
# ``generate_image`` returns ``image_id=<uuid>`` in its normal text result.  Keep
# this deliberately narrow: it is a local ImageStore identifier, never a path or URL.
_IMAGE_ID_RE = re.compile(r"\bimage_id=([A-Za-z0-9_-]+)")
_IMAGE_MIMES = {
    "image/png": ("png", "image/png"),
    "image/jpeg": ("jpg", "image/jpeg"),
    "image/jpg": ("jpg", "image/jpeg"),
    "image/gif": ("gif", "image/gif"),
    "image/webp": ("webp", "image/webp"),
    "image/bmp": ("bmp", "image/bmp"),
}


@dataclass
class _PendingImageBatch:
    """同一飞书用户在同一会话中等待文字说明的一批图片。"""

    chat_id: str
    sequence: int
    sender_open_id: str
    chat_type: str
    images: list = field(default_factory=list)
    downloads: int = 0
    text: str | None = None
    timer: asyncio.Task | None = None
    expired: bool = False
    discarded: bool = False


class FeishuChannel(Channel):
    """飞书长连接渠道（name 固定为 ``"feishu"``）。"""

    def __init__(
        self,
        name: str,
        bus,
        app_id: str,
        app_secret: str,
        image_store=None,
        image_merge_window_sec: float = 10.0,
        asr_service=None,
        tts_service=None,
        max_voice_chars: int = 300,
        bind_callback=None,
        unbind_callback=None,
    ) -> None:
        super().__init__(name=name, bus=bus)
        self.app_id = app_id
        self.app_secret = app_secret
        # Shared ImageStore is injected by the composition root.  It remains
        # optional so existing callers keep working while the app is upgraded.
        self.image_store = image_store
        try:
            merge_window = float(image_merge_window_sec)
        except (TypeError, ValueError):
            merge_window = 10.0
        # 防止错误配置让消息长期滞留；0 明确表示关闭合并等待。
        self.image_merge_window_sec = max(0.0, min(60.0, merge_window))
        # 可选 ASR 服务由组合根注入；为 None 时收到语音回「未启用」提示，
        # 保持向后兼容（既有调用方不传也能用）。
        self.asr_service = asr_service
        # 可选 TTS 服务由组合根注入；为 None 时语音入站仍正常（文字兜底回复）。
        self.tts_service = tts_service
        try:
            max_voice_chars = int(max_voice_chars)
        except (TypeError, ValueError):
            max_voice_chars = 300
        # 超过该字符数的回复不硬转语音（飞书语音有大小限制、长文本合成慢）。
        self.max_voice_chars = max(0, max_voice_chars)
        # 语音对语音：语音入站成功转写后记录该 chat 待发语音回复标记（chat_id 集合，
        # 幂等 add）。仅由主事件循环访问。
        self._voice_reply_pending: set[str] = set()
        # 仅由主事件循环访问：key=(chat_id, session sequence, sender_open_id)。
        self._pending_image_batches: dict[tuple[str, int, str], _PendingImageBatch] = {}
        self._stopping = False
        self._loop = None              # 主事件循环（跨线程投递用）
        self._client = None            # 用于发送消息的 IM v1 client
        self._ws_client = None         # 长连接客户端
        self._thread = None            # 长连接后台线程（守护）
        # —— 多会话状态（与 CLI 的 /new 机制对称）——
        # 每个 chat_id 维护自己的会话序号：sender_id 形如 "<chat_id>:<序号>"，
        # Gateway 据此派生独立 session_key，从而同一聊天框内可开多个互不干扰的会话。
        # _sessions: chat_id -> {"seq": 已建会话数, "current": 当前活动序号}
        self._sessions: dict = {}
        self._clear_callback = None    # 清空历史回调（/clear 命令调用，同 CLI）
        self._context_callback = None  # 上下文占用查询回调（/context 命令调用，同 CLI）
        # ReminderService owns persistence and authorization; this channel only
        # recognizes deterministic p2p commands and forwards identity fields.
        self._bind_callback = bind_callback
        self._unbind_callback = unbind_callback

    async def start(self) -> None:
        """捕获主事件循环，建好长连接客户端并在后台线程启动监听。"""
        self._loop = asyncio.get_running_loop()
        self._stopping = False

        # 发送消息用的开放 API client（与长连接共用 app_id/secret）
        self._client = (
            lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        )

        # 事件处理器：只负责把消息塞进总线，绝不在这里调 LLM
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )

        # 长连接客户端（无公网 IP 也能用）。auto_reconnect 默认开启，断线自愈。
        self._ws_client = lark.ws.Client(
            app_id=self.app_id,
            app_secret=self.app_secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=event_handler,
        )

        # start() 是阻塞调用，放到守护线程里运行，避免卡住主事件循环
        self._thread = threading.Thread(
            target=self._run_ws, name="feishu-ws", daemon=True
        )
        self._thread.start()
        print("（飞书渠道已启动｜长连接监听中）")

    def _run_ws(self) -> None:
        """在独立线程里跑长连接；异常只记录不抛出，避免线程静默消失。"""
        try:
            self._ws_client.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("飞书长连接异常终止：%s", exc)

    def _on_message(self, data) -> None:
        """飞书消息事件回调（运行在 SDK 内部线程）。

        必须快速返回：只做字段提取 + 投递进总线，绝不在这里调 LLM。
        """
        try:
            event = data.event
            message = event.message
            if message is None or message.message_type not in ("text", "image", "audio"):
                return

            chat_type = message.chat_type        # "p2p" / "group"
            chat_id = message.chat_id
            mentions = message.mentions or []
            sender = getattr(event.sender, "sender_id", None)
            sender_open_id = getattr(sender, "open_id", None) or "unknown"

            # 群聊：仅当被 @ 才响应；私聊始终响应
            if chat_type == "group" and not mentions:
                return

            # 图片下载会走飞书的鉴权资源 API，可能慢于事件回调时限；把整个
            # 下载 + 投递过程交给主事件循环，SDK 回调立刻返回。
            if message.message_type == "image":
                sequence = self._session_state(chat_id)["current"]
                asyncio.run_coroutine_threadsafe(
                    self._queue_image_message(
                        message, chat_type, sequence, sender_open_id
                    ),
                    self._loop,
                )
                return

            # 语音同样要下载资源文件（type=file）+ 转写，可能慢于事件回调时限；
            # 整个下载 + 转写 + 投递交给主事件循环，SDK 回调立刻返回。
            if message.message_type == "audio":
                sequence = self._session_state(chat_id)["current"]
                asyncio.run_coroutine_threadsafe(
                    self._queue_audio_message(
                        message, chat_type, sequence, sender_open_id
                    ),
                    self._loop,
                )
                return

            # 提取正文并去掉 @提醒 前缀（如 "@_user_1 你好" -> "你好"）
            try:
                content = json.loads(message.content or "{}")
            except (ValueError, TypeError):
                return
            text = content.get("text", "")
            for m in mentions:
                key = getattr(m, "key", None)
                if key:
                    text = text.replace(key, "")
            text = text.strip()
            if not text:
                return

            sequence = self._session_state(chat_id)["current"]

            # 命令也是图片批次边界，但不能成为图片说明。放到主事件循环处理，
            # 与图片等待状态串行；命令本身仍在 SDK 线程立即执行，确保紧随其后的
            # 新消息能读取到 /new、/switch 更新后的会话序号。
            if text.split()[0] in (
                "/new",
                "/sessions",
                "/switch",
                "/clear",
                "/context",
                "/bind-reminders",
                "/unbind-reminders",
            ):
                asyncio.run_coroutine_threadsafe(
                    self._handle_pending_for_command(chat_id, text, sequence),
                    self._loop,
                )
                self._try_handle_command(
                    chat_id, text, chat_type=chat_type, sender_open_id=sender_open_id
                )
                return

            # 正常文本也进入主事件循环：若同一用户有待处理图片则合并，否则立即投递。
            asyncio.run_coroutine_threadsafe(
                self._publish_or_merge_text(
                    chat_id, chat_type, sequence, sender_open_id, text
                ),
                self._loop,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理飞书入站消息失败：%s", exc)

    async def _queue_image_message(
        self, message, chat_type: str, sequence: int, sender_open_id: str
    ) -> None:
        """下载图片并放入等待后续文字说明的批次。

        ``file_key`` 仅作为飞书资源 API 的参数使用，绝不当成本地路径。
        图片无法下载或 ImageStore 尚未注入时返回明确错误，避免把不完整消息交给 Agent。
        """
        if self.image_store is None:
            logger.warning("收到飞书图片但未配置 image_store，已忽略")
            await self._publish_image_error(
                message.chat_id, "⚠️ 当前实例未启用图片存储，暂时无法处理图片。"
            )
            return
        try:
            content = json.loads(message.content or "{}")
            image_key = content.get("image_key")
            message_id = getattr(message, "message_id", None)
            if not image_key or not message_id:
                logger.warning("飞书图片事件缺少 image_key 或 message_id，已忽略")
                await self._publish_image_error(
                    message.chat_id, "⚠️ 图片消息缺少资源标识，无法读取。"
                )
                return

            chat_id = message.chat_id
            batch_key = (chat_id, sequence, sender_open_id)
            batch = self._pending_image_batches.get(batch_key)
            if batch is None:
                batch = _PendingImageBatch(
                    chat_id=chat_id,
                    sequence=sequence,
                    sender_open_id=sender_open_id,
                    chat_type=chat_type,
                )
                self._pending_image_batches[batch_key] = batch

            # 先留出顺序槽位再开始下载；并发下载完成顺序不会改变用户发送顺序。
            slot = len(batch.images)
            batch.images.append(None)
            batch.downloads += 1
            self._reset_image_batch_timer(batch_key, batch)

            raw, mime = await asyncio.to_thread(
                self._download_image_sync, message_id, image_key
            )
            # /clear 或渠道停止可能发生在下载过程中；此时不再保存或投递。
            if self._stopping or self._pending_image_batches.get(batch_key) is not batch:
                return
            if not raw:
                await self._publish_image_error(
                    chat_id, "⚠️ 从飞书读取图片失败，请稍后重试。"
                )
                return
            if len(raw) > _MAX_INBOUND_IMAGE_BYTES:
                await self._publish_image_error(
                    chat_id, "⚠️ 图片超过 20 MB，暂时无法处理。"
                )
                return
            image_type = self._image_ext_mime(raw, mime)
            if image_type is None:
                await self._publish_image_error(
                    chat_id,
                    "⚠️ 当前仅支持 PNG、JPEG、GIF、WEBP 和 BMP 图片。",
                )
                return
            ext, mime = image_type
            session_key = self._image_session_key(chat_id, sequence)
            ref = self.image_store.save(session_key, raw, ext, mime)
            batch.images[slot] = ref
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理飞书图片入站消息失败：%s", exc)
            await self._publish_image_error(
                message.chat_id, "⚠️ 处理图片时出错，请稍后重试。"
            )
        finally:
            # metadata 校验失败时 batch 尚未创建。
            if "batch" in locals() and self._pending_image_batches.get(batch_key) is batch:
                batch.downloads = max(0, batch.downloads - 1)
                if batch.downloads == 0:
                    if batch.text is not None or batch.expired:
                        await self._flush_image_batch(batch_key, batch)
                    elif not any(ref is not None for ref in batch.images):
                        self._discard_image_batch(batch_key, batch, delete_files=True)

    async def _queue_audio_message(
        self, message, chat_type: str, sequence: int, sender_open_id: str
    ) -> None:
        """下载飞书语音并交给 ASR 服务转写，成功后作为普通文本消息投递。

        与图片同理：事件回调里只投递，下载 + 转写都在主事件循环完成，
        SDK 回调立即返回；任何失败都回明确提示，绝不静默丢弃。
        """
        if self.asr_service is None:
            logger.warning("收到飞书语音但未启用 ASR，已回提示")
            await self._publish_image_error(
                message.chat_id, "⚠️ 当前实例未启用语音转写（ASR）"
            )
            return
        try:
            content = json.loads(message.content or "{}")
            file_key = content.get("file_key")
            message_id = getattr(message, "message_id", None)
            if not file_key or not message_id:
                logger.warning("飞书语音事件缺少 file_key 或 message_id，已忽略")
                await self._publish_image_error(
                    message.chat_id, "⚠️ 语音消息缺少资源标识，无法读取。"
                )
                return

            chat_id = message.chat_id
            raw, mime = await asyncio.to_thread(
                self._download_audio_sync, message_id, file_key
            )
            if not raw:
                await self._publish_image_error(
                    chat_id, "⚠️ 从飞书读取语音失败，请稍后重试。"
                )
                return

            filename, media_type = self._audio_file_meta(raw, mime, file_key)
            try:
                result = await self.asr_service.transcribe(
                    raw, filename=filename, media_type=media_type
                )
            except ASRError as exc:
                # ASRError 是渠道可安全展示的错误，直接回用户，不扩散堆栈。
                await self._publish_image_error(
                    chat_id, f"⚠️ 语音转写失败：{exc.message}"
                )
                return
            except Exception as exc:  # noqa: BLE001 - 细节只进日志，用户只看到安全提示
                logger.exception("飞书语音转写服务异常：%s", exc)
                await self._publish_image_error(
                    chat_id, "⚠️ 语音转写服务暂时不可用，请稍后重试。"
                )
                return

            text = str(getattr(result, "text", "") or "").strip()
            if not text:
                await self._publish_image_error(
                    chat_id, "⚠️ 语音转写未返回可用文本，请重试。"
                )
                return
            await self._publish_text_inbound(
                chat_id, chat_type, sequence, sender_open_id, text
            )
            # 语音入站成功（转写成功并已投递）→ 本次回复优先回语音气泡。
            # 转写失败路径已提前 return，不会走到这里；set 幂等，多轮竞态可接受。
            self._voice_reply_pending.add(chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理飞书语音入站消息失败：%s", exc)
            await self._publish_image_error(
                message.chat_id, "⚠️ 处理语音时出错，请稍后重试。"
            )

    async def _publish_or_merge_text(
        self,
        chat_id: str,
        chat_type: str,
        sequence: int,
        sender_open_id: str,
        text: str,
    ) -> None:
        """把后续文字并入同一用户、同一会话的待处理图片。"""
        batch_key = (chat_id, sequence, sender_open_id)
        batch = self._pending_image_batches.get(batch_key)
        if batch is not None and not batch.discarded:
            batch.text = text
            self._cancel_image_batch_timer(batch)
            if batch.downloads == 0:
                await self._flush_image_batch(batch_key, batch)
            return

        await self._publish_text_inbound(
            chat_id, chat_type, sequence, sender_open_id, text
        )

    async def _publish_text_inbound(
        self,
        chat_id: str,
        chat_type: str,
        sequence: int,
        sender_open_id: str,
        text: str,
    ) -> None:
        await self.bus.publish_inbound(InboundMessage(
            channel=self.name,
            sender_id=f"{chat_id}:{sequence}",
            chat_id=chat_id,
            content=text,
            raw={"chat_type": chat_type, "sender_open_id": sender_open_id},
        ))

    def _reset_image_batch_timer(
        self,
        batch_key: tuple[str, int, str],
        batch: _PendingImageBatch,
    ) -> None:
        """每收到一张新图片都从头计算等待时间。"""
        self._cancel_image_batch_timer(batch)
        batch.expired = self.image_merge_window_sec <= 0
        if not batch.expired:
            batch.timer = asyncio.create_task(
                self._expire_image_batch(batch_key, batch)
            )

    async def _expire_image_batch(
        self,
        batch_key: tuple[str, int, str],
        batch: _PendingImageBatch,
    ) -> None:
        try:
            await asyncio.sleep(self.image_merge_window_sec)
        except asyncio.CancelledError:
            return
        if self._pending_image_batches.get(batch_key) is not batch:
            return
        batch.timer = None
        batch.expired = True
        if batch.downloads == 0:
            await self._flush_image_batch(batch_key, batch)

    async def _flush_image_batch(
        self,
        batch_key: tuple[str, int, str],
        batch: _PendingImageBatch,
    ) -> None:
        """恰好一次地把图片批次投递为图文消息。"""
        if self._pending_image_batches.get(batch_key) is not batch:
            return
        self._pending_image_batches.pop(batch_key, None)
        self._cancel_image_batch_timer(batch)
        batch.discarded = True
        images = [ref for ref in batch.images if ref is not None]
        if batch.text is None and not images:
            return
        content = batch.text
        if content is None:
            content = "请分析这些图片。" if len(images) > 1 else "请分析这张图片。"
        await self.bus.publish_inbound(InboundMessage(
            channel=self.name,
            sender_id=f"{batch.chat_id}:{batch.sequence}",
            chat_id=batch.chat_id,
            content=content,
            images=images or None,
            raw={
                "chat_type": batch.chat_type,
                "sender_open_id": batch.sender_open_id,
            },
        ))

    def _cancel_image_batch_timer(self, batch: _PendingImageBatch) -> None:
        timer = batch.timer
        batch.timer = None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()

    def _discard_image_batch(
        self,
        batch_key: tuple[str, int, str],
        batch: _PendingImageBatch,
        *,
        delete_files: bool,
    ) -> None:
        if self._pending_image_batches.get(batch_key) is batch:
            self._pending_image_batches.pop(batch_key, None)
        batch.discarded = True
        self._cancel_image_batch_timer(batch)
        if delete_files:
            for ref in batch.images:
                path = getattr(ref, "path", None)
                if not path:
                    continue
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning("清理待处理飞书图片失败：%s", exc)

    async def _handle_pending_for_command(
        self, chat_id: str, text: str, sequence: int
    ) -> None:
        cmd = text.split()[0]
        matching = [
            (key, batch)
            for key, batch in self._pending_image_batches.items()
            if key[0] == chat_id and key[1] == sequence
        ]
        if cmd == "/clear":
            for key, batch in matching:
                self._discard_image_batch(key, batch, delete_files=True)
        elif cmd in ("/new", "/switch"):
            for key, batch in matching:
                batch.expired = True
                self._cancel_image_batch_timer(batch)
                if batch.downloads == 0:
                    await self._flush_image_batch(key, batch)

    async def _publish_image_error(self, chat_id: str, text: str) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(channel=self.name, chat_id=chat_id, content=text)
        )

    def _download_image_sync(self, message_id: str, image_key: str):
        """通过 SDK 下载单张飞书图片；由 ``to_thread`` 调用。"""
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(image_key)
            .type("image")
            .build()
        )
        resp = self._client.im.v1.message_resource.get(request)
        if not resp.success() or getattr(resp, "file", None) is None:
            logger.error("飞书图片下载失败：code=%s msg=%s", resp.code, resp.msg)
            return None, None
        raw_response = getattr(resp, "raw", None)
        headers = getattr(raw_response, "headers", {}) or {}
        mime = headers.get("content-type", "") or headers.get("Content-Type", "")
        resource = resp.file
        try:
            return resource.read(), mime
        finally:
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    def _download_audio_sync(self, message_id: str, file_key: str):
        """通过 SDK 下载一条飞书语音资源；由 ``to_thread`` 调用。

        ``file_key`` 仅作为飞书资源 API 的参数使用，绝不当成本地路径；
        飞书语音/音频/视频统一走 ``type("file")``（不是 ``type("image")``）。
        """
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("file")
            .build()
        )
        resp = self._client.im.v1.message_resource.get(request)
        if not resp.success() or getattr(resp, "file", None) is None:
            logger.error("飞书语音下载失败：code=%s msg=%s", resp.code, resp.msg)
            return None, None
        raw_response = getattr(resp, "raw", None)
        headers = getattr(raw_response, "headers", {}) or {}
        mime = headers.get("content-type", "") or headers.get("Content-Type", "")
        resource = resp.file
        try:
            return resource.read(), mime
        finally:
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _image_ext_mime(raw: bytes, mime: str):
        # 飞书响应头只作为后备；优先根据文件签名识别，避免把未知字节伪装成 PNG。
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png", "image/png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "jpg", "image/jpeg"
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return "gif", "image/gif"
        if raw.startswith(b"BM"):
            return "bmp", "image/bmp"
        if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "webp", "image/webp"
        mime = (mime or "").split(";", 1)[0].strip().lower()
        if mime in _IMAGE_MIMES:
            return _IMAGE_MIMES[mime]
        return None

    @staticmethod
    def _audio_file_meta(raw: bytes, mime: str, file_key: str):
        """从飞书语音下载响应推断文件名与媒体类型。

        飞书语音消息不携带文件名，file_key 仅作资源标识；媒体类型优先取
        下载响应 content-type（预期 audio/opus 等），缺失或非音频类型时
        兜底 ``application/octet-stream``（AudioTranscriptionService 允许）。
        文件名用 file_key 派生 + 推断扩展名，非空即可。
        """
        declared = (mime or "").split(";", 1)[0].strip().lower()
        ext_by_mime = {
            "audio/ogg": "ogg",
            "audio/opus": "opus",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
            "audio/x-m4a": "m4a",
            "audio/aac": "aac",
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/webm": "webm",
            "audio/amr": "amr",
            "audio/x-amr": "amr",
        }
        ext = ext_by_mime.get(declared)
        # 响应头缺失时做一次最小嗅探（Ogg 容器内常见 OPUS 语音）。
        if ext is None and raw.startswith(b"OggS"):
            declared, ext = "audio/ogg", "ogg"
        filename = f"{file_key}.{ext}" if ext else f"{file_key}.audio"
        if declared.startswith("audio/") or declared == "application/octet-stream":
            media_type = declared or "application/octet-stream"
        else:
            media_type = "application/octet-stream"
        return filename, media_type

    def _image_session_key(self, chat_id: str, sequence: int) -> str:
        """与 Gateway 使用的飞书会话 key 保持一致。"""
        return f"{self.name}:{chat_id}:{sequence}"

    # —— 多会话辅助方法（与 CLIChannel 的 /new 机制对称）——
    def _session_state(self, chat_id: str) -> dict:
        """取（或建）某 chat_id 的会话状态；首条消息前默认序号 0。"""
        st = self._sessions.get(chat_id)
        if st is None:
            st = {"seq": 0, "current": 0}
            self._sessions[chat_id] = st
        return st

    def _try_handle_command(
        self,
        chat_id: str,
        text: str,
        chat_type: str = "p2p",
        sender_open_id: str | None = None,
    ) -> bool:
        """解析飞书内置命令。命中返回 True（已回复，调用方跳过 Agent）。

        命令与 CLI 完全对称：/new 开新会话、/sessions 列表、/switch <n> 切换、
        /clear 清当前会话。回复经 bus 直接回飞书，不经过 Gateway 的 Agent。
        """
        parts = text.split()
        if not parts:
            return False
        cmd = parts[0]
        if cmd not in (
            "/new",
            "/sessions",
            "/switch",
            "/clear",
            "/context",
            "/bind-reminders",
            "/unbind-reminders",
        ):
            return False

        if cmd in ("/bind-reminders", "/unbind-reminders"):
            if chat_type != "p2p":
                self._reply(chat_id, "请在与机器人的私聊中使用提醒绑定命令。")
                return True
            callback = (
                self._bind_callback
                if cmd == "/bind-reminders"
                else self._unbind_callback
            )
            if callback is None:
                self._reply(chat_id, "⚠️ 当前实例未启用主动提醒服务。")
                return True
            self._schedule_binding_callback(
                chat_id, sender_open_id or "", callback, cmd
            )
            return True

        st = self._session_state(chat_id)
        if cmd == "/new":
            st["seq"] += 1
            st["current"] = st["seq"]
            self._reply(chat_id,
                        f"🆕 已新建会话 #{st['current']}"
                        f"（旧会话已保留，可 /switch 切回）")
        elif cmd == "/sessions":
            items = []
            for i in range(st["seq"] + 1):
                mark = " ← 当前" if i == st["current"] else ""
                items.append(f"  会话 #{i}{mark}")
            self._reply(chat_id, "📋 已有会话：\n" + "\n".join(items))
        elif cmd == "/switch":
            if len(parts) != 2 or not parts[1].isdigit():
                self._reply(chat_id, "⚠️ 用法：/switch <会话序号>，例如 /switch 0")
            else:
                target = int(parts[1])
                if target < 0 or target > st["seq"]:
                    self._reply(chat_id,
                                f"⚠️ 会话 #{target} 不存在"
                                f"（有效范围 0~{st['seq']}）")
                else:
                    st["current"] = target
                    self._reply(chat_id, f"🔀 已切换到会话 #{target}")
        elif cmd == "/clear":
            key = f"{self.name}:{chat_id}:{st['current']}"
            if self._clear_callback is not None:
                self._clear_callback(key)
            self._reply(chat_id, f"🧹 当前会话 #{st['current']} 历史已清空")
        elif cmd == "/context":
            key = f"{self.name}:{chat_id}:{st['current']}"
            if self._context_callback is not None:
                self._reply(chat_id, self._context_callback(key))
            else:
                self._reply(chat_id, "当前实例未注入上下文占用回调。")
        return True

    def _schedule_binding_callback(
        self, chat_id: str, open_id: str, callback, command: str
    ) -> None:
        """Run a synchronous or asynchronous binding callback on the main loop."""

        async def run_callback() -> None:
            try:
                outcome = callback(chat_id, open_id)
                if hasattr(outcome, "__await__"):
                    outcome = await outcome
                default = (
                    "✅ 已绑定主动提醒。"
                    if command == "/bind-reminders"
                    else "✅ 已解绑主动提醒。"
                )
                self._reply(
                    chat_id,
                    outcome if isinstance(outcome, str) and outcome else default,
                )
            except Exception as exc:  # noqa: BLE001 - keep details out of user reply
                logger.exception("提醒绑定命令失败：%s", exc)
                self._reply(chat_id, "⚠️ 提醒设置失败，请稍后重试。")

        asyncio.run_coroutine_threadsafe(run_callback(), self._loop)

    def _reply(self, chat_id: str, text: str) -> None:
        """命令确认等轻量回复：直接经 bus 回飞书，绕过 Agent。"""
        asyncio.run_coroutine_threadsafe(
            self.bus.publish_outbound(OutboundMessage(
                channel=self.name,
                chat_id=chat_id,
                content=text,
                reply_to=None,
            )),
            self._loop,
        )

    async def send(self, message: OutboundMessage) -> "DeliveryResult":
        """Send one logical reply and report whether Feishu accepted it."""
        chat_id = message.chat_id
        text = message.content or ""
        if not chat_id:
            logger.warning("飞书回复缺少 chat_id，已丢弃")
            return self._delivery_result(
                success=False, code="missing_chat_id", message="missing chat_id"
            )

        # Gateway 现在会附带结构化 ImageRef；优先使用它，避免用户在
        # Agent 执行期间切换会话时按“当前会话”找错图片。image_id 文本解析
        # 仅作为与旧 Gateway 的兼容回退。
        image_refs = self._outbound_image_refs(message)
        image_failure = None
        last_result = None
        strict_delivery = message.delivery_future is not None
        for index, ref in enumerate(image_refs):
            result = await self._send_image(chat_id, ref)
            if not result.success:
                # Reliable reminders are all-or-stop.  Ordinary chat keeps the
                # historical text fallback when an attached image fails.
                if strict_delivery or not text:
                    return result
                image_failure = result
                break
            last_result = result
            if index < len(image_refs) - 1:
                await asyncio.sleep(_CHUNK_SLEEP)

        # 纯图片回复不额外发送空文本；普通文本仍保持原来的分片行为。
        if not text and image_refs:
            return last_result

        # 语音对语音：语音入站后本次出站优先回语音气泡。标记单次消费——先取走
        # 再尝试；若合成期间又来一条新语音（标记被重新加入）则留给下一次回复，
        # 符合多轮竞态约定。tts_service 未注入或文本超长时直接走文字，不残留标记。
        if chat_id in self._voice_reply_pending:
            self._voice_reply_pending.discard(chat_id)
            if (
                self.tts_service is not None
                and text
                and len(text) <= self.max_voice_chars
            ):
                voice_result = await self._try_send_voice_reply(chat_id, text)
                if voice_result is not None:
                    return voice_result

        # 按字符切分；无图片且内容为空时仍发一条占位，保持原有行为。
        chunks = [text[i:i + _MAX_CHUNK] for i in range(0, len(text), _MAX_CHUNK)] or [""]
        for i, chunk in enumerate(chunks):
            payload = json.dumps({"text": chunk}, ensure_ascii=False)
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(payload)
                    .build()
                )
                .build()
            )
            result = await self._send_request(self._client.im.v1.message.create, request)
            if not result.success:
                return result
            last_result = result
            if i < len(chunks) - 1:
                await asyncio.sleep(_CHUNK_SLEEP)
        return image_failure or last_result

    def _outbound_image_refs(self, message: OutboundMessage) -> list:
        if message.images:
            return list(message.images)
        if self.image_store is None:
            return []
        chat_id = message.chat_id
        st = self._session_state(chat_id)
        session_key = self._image_session_key(chat_id, st["current"])
        refs = []
        for image_id in _IMAGE_ID_RE.findall(message.content or ""):
            ref = self.image_store.resolve(session_key, image_id)
            if ref is not None and all(ref.id != old.id for old in refs):
                refs.append(ref)
        return refs

    async def _send_image(self, chat_id: str, ref) -> "DeliveryResult":
        image_key, upload_failure = await self._upload_image(ref)
        if upload_failure is not None:
            return upload_failure
        payload = json.dumps({"image_key": image_key}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("image")
                .content(payload)
                .build()
            )
            .build()
        )
        return await self._send_request(self._client.im.v1.message.create, request)

    async def _upload_image(self, ref):
        """Upload once; ReminderScheduler owns logical retries."""
        try:
            response = await asyncio.to_thread(self._upload_image_sync, ref)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return None, self._delivery_result(
                success=False, retryable=True, message=str(exc)
            )
        if response is None:
            return None, self._delivery_result(
                success=False, message="image upload failed"
            )
        code = getattr(response, "code", None)
        if response.success():
            image_key = getattr(getattr(response, "data", None), "image_key", None)
            if image_key:
                return image_key, None
            return None, self._delivery_result(
                success=False,
                code=code,
                message="image upload returned no image key",
            )
        return None, self._delivery_result(
            success=False,
            retryable=self._retryable_code(code),
            code=code,
            message=f"Feishu image upload error {code}: "
            f"{getattr(response, 'msg', '')}",
        )

    async def _try_send_voice_reply(
        self, chat_id: str, text: str
    ) -> "DeliveryResult | None":
        """尝试用语音气泡回复本次出站文本；成功返回结果，失败回 None（走文字兜底）。

        链路：TTS 合成 → ffmpeg 转 OPUS → ``CreateFileRequest`` 上传（file_type=opus）
        → ``msg_type=audio`` 发送。任何一步失败都记录日志并返回 None，由 send() 回
        文字原文，绝不静默丢弃。
        """
        try:
            tts_result = await self.tts_service.synthesize(text)
        except TTSError as exc:
            logger.warning("语音回复合成失败，改用文字回复：%s", exc.message)
            return None
        except Exception as exc:  # noqa: BLE001 - 细节只进日志，用户走文字兜底
            logger.exception("语音回复合成异常，改用文字回复：%s", exc)
            return None
        audio = getattr(tts_result, "audio", None)
        media_type = getattr(tts_result, "media_type", "audio/wav")
        if not audio:
            logger.warning("语音回复合成返回空音频，改用文字回复")
            return None
        try:
            opus = await self._convert_audio_to_opus(audio, media_type)
        except MediaError as exc:
            logger.warning("语音回复 OPUS 转换失败，改用文字回复：%s", exc.message)
            return None

        file_key, upload_failure = await self._upload_audio(opus)
        if upload_failure is not None:
            logger.warning(
                "语音回复上传失败，改用文字回复：%s", upload_failure.message
            )
            return None

        payload = json.dumps({"file_key": file_key}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("audio")
                .content(payload)
                .build()
            )
            .build()
        )
        result = await self._send_request(self._client.im.v1.message.create, request)
        if not result.success:
            logger.warning("语音回复发送失败，改用文字回复：%s", result.message)
            return None
        logger.info("飞书语音回复已发送（chat_id=%s）", chat_id)
        return result

    @staticmethod
    async def _convert_audio_to_opus(audio: bytes, media_type: str) -> bytes:
        """把 TTS 合成音频转成 16 kHz 单声道 OPUS 字节。

        ``encode_to_opus`` 内部用 asyncio 子进程跑 ffmpeg（非阻塞），随 ASR service
        同款模式在主事件循环 await；临时目录即用即删，音频不落盘。
        """
        with tempfile.TemporaryDirectory(prefix="nanoclaw-tts-") as directory:
            return await encode_to_opus(
                audio,
                media_type=media_type,
                directory=directory,
            )

    async def _upload_audio(self, opus: bytes):
        """上传 OPUS 音频并返回 ``(file_key, failure)``；仿 ``_upload_image`` 的错误分类。"""
        try:
            response = await asyncio.to_thread(self._upload_audio_sync, opus)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return None, self._delivery_result(
                success=False, retryable=True, message=str(exc)
            )
        if response is None:
            return None, self._delivery_result(
                success=False, message="audio upload failed"
            )
        code = getattr(response, "code", None)
        if response.success():
            file_key = getattr(getattr(response, "data", None), "file_key", None)
            if file_key:
                return file_key, None
            return None, self._delivery_result(
                success=False,
                code=code,
                message="audio upload returned no file key",
            )
        return None, self._delivery_result(
            success=False,
            retryable=self._retryable_code(code),
            code=code,
            message=f"Feishu audio upload error {code}: "
            f"{getattr(response, 'msg', '')}",
        )

    def _upload_audio_sync(self, opus: bytes):
        """上传 OPUS 音频并返回 SDK response；由 ``to_thread`` 调用。

        ``file_key`` 仅作飞书资源标识，绝不当成本地路径；音频字节留在内存，
        不落盘。
        """
        with io.BytesIO(opus) as audio_file:
            request = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type("opus")
                    .file_name("reply.opus")
                    .file(audio_file)
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.file.create(request)
        if not resp.success() or not getattr(resp, "data", None):
            logger.error("飞书音频上传失败：code=%s msg=%s", resp.code, resp.msg)
        return resp

    async def _send_request(self, request_fn, request) -> "DeliveryResult":
        """Make one SDK request and classify the result for the scheduler."""
        try:
            response = await asyncio.to_thread(request_fn, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK transport failure
            logger.exception("飞书发送请求失败：%s", exc)
            return self._delivery_result(
                success=False, retryable=True, message=str(exc)
            )

        code = getattr(response, "code", None)
        if response.success():
            return self._delivery_result(
                success=True,
                code=code,
                message=getattr(response, "msg", None) or "accepted",
                provider_message_id=self._provider_message_id(response),
            )
        message = f"Feishu API error {code}: {getattr(response, 'msg', '')}"
        logger.error("飞书发送失败：%s", message)
        return self._delivery_result(
            success=False,
            retryable=self._retryable_code(code),
            code=code,
            message=message,
        )

    @staticmethod
    def _retryable_code(code) -> bool:
        return code == 429 or (isinstance(code, int) and 500 <= code < 600)

    @staticmethod
    def _provider_message_id(response) -> str | None:
        data = getattr(response, "data", None)
        return getattr(data, "message_id", None) if data is not None else None

    @staticmethod
    def _delivery_result(
        *,
        success: bool,
        retryable: bool = False,
        code: int | str | None = None,
        provider_message_id: str | None = None,
        message: str | None = None,
    ) -> "DeliveryResult":
        from reminders.models import DeliveryResult

        return DeliveryResult(
            success=success,
            retryable=retryable,
            code=code,
            provider_message_id=provider_message_id,
            message=message,
        )

    def _upload_image_sync(self, ref):
        """上传 ImageStore 图片并返回 SDK response；由 ``to_thread`` 调用。"""
        if not os.path.isfile(ref.path):
            logger.warning("待发送图片已不存在：%s", ref.path)
            return None
        try:
            size = os.path.getsize(ref.path)
        except OSError as exc:
            logger.warning("无法读取待发送图片大小：%s", exc)
            return None
        if size <= 0 or size > _MAX_OUTBOUND_IMAGE_BYTES:
            logger.warning("待发送图片大小不符合飞书限制：%s bytes", size)
            return None
        with open(ref.path, "rb") as image_file:
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(image_file)
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.image.create(request)
        if not resp.success() or not getattr(resp, "data", None):
            logger.error("飞书图片上传失败：code=%s msg=%s", resp.code, resp.msg)
        return resp

    async def stop(self) -> None:
        """取消图片等待任务并清理尚未投递的图片。"""
        self._stopping = True
        for key, batch in list(self._pending_image_batches.items()):
            self._discard_image_batch(key, batch, delete_files=True)
        # WSClient 无显式 stop()，守护线程会随主进程退出而终止。
