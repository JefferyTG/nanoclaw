"""飞书渠道：通过飞书长连接（WebSocket）接收消息，经总线转发给 Agent，再把回复发回飞书。

要点：
- 使用 lark-oapi 的 ``WSClient`` 长连接模式，本地无需公网 IP / 回调域名；
- 事件回调运行在 SDK 内部线程，必须「只投总线、立即返回」，不能在其中做阻塞的
  LLM 调用（飞书事件处理有 3 秒超时）。真正的处理在 Gateway 的异步入站循环中完成；
- 跨线程投递用 ``run_coroutine_threadsafe`` 把 ``InboundMessage`` 送到主事件循环的 bus；
- 群聊仅在被 @ 时响应；私聊始终响应；
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
import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from channels.base import Channel
from bus.queue import InboundMessage, OutboundMessage

logger = logging.getLogger("nanoclaw.feishu")

# 飞书单条文本消息长度上限约 20KB，这里按字符切分并留足余量
_MAX_CHUNK = 4000
# 飞书开放接口限频约 5 QPS，分片发送时片间稍作停顿避免被限流
_CHUNK_SLEEP = 0.25


class FeishuChannel(Channel):
    """飞书长连接渠道（name 固定为 ``"feishu"``）。"""

    def __init__(self, name: str, bus, app_id: str, app_secret: str) -> None:
        super().__init__(name=name, bus=bus)
        self.app_id = app_id
        self.app_secret = app_secret
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

    async def start(self) -> None:
        """捕获主事件循环，建好长连接客户端并在后台线程启动监听。"""
        self._loop = asyncio.get_running_loop()

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
            if message is None or message.message_type != "text":
                return  # 只处理文本消息，其余类型忽略

            chat_type = message.chat_type        # "p2p" / "group"
            chat_id = message.chat_id
            mentions = message.mentions or []

            # 群聊：仅当被 @ 才响应；私聊始终响应
            if chat_type == "group" and not mentions:
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

            sender = getattr(event.sender, "sender_id", None)
            sender_open_id = getattr(sender, "open_id", None) or "unknown"

            # 内置命令（/new /sessions /switch /clear）：命中则直接回复并跳过 Agent
            if self._try_handle_command(chat_id, text):
                return

            st = self._session_state(chat_id)
            msg = InboundMessage(
                channel=self.name,
                # sender_id 携带当前会话序号，Gateway 据此派生独立 session_key
                sender_id=f"{chat_id}:{st['current']}",
                chat_id=chat_id,
                content=text,
                raw={"chat_type": chat_type, "sender_open_id": sender_open_id},
            )

            # 跨线程投递到主事件循环的 bus（不 await，立即返回以满足 3 秒超时）
            asyncio.run_coroutine_threadsafe(
                self.bus.publish_inbound(msg), self._loop
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理飞书入站消息失败：%s", exc)

    # —— 多会话辅助方法（与 CLIChannel 的 /new 机制对称）——
    def _session_state(self, chat_id: str) -> dict:
        """取（或建）某 chat_id 的会话状态；首条消息前默认序号 0。"""
        st = self._sessions.get(chat_id)
        if st is None:
            st = {"seq": 0, "current": 0}
            self._sessions[chat_id] = st
        return st

    def _try_handle_command(self, chat_id: str, text: str) -> bool:
        """解析飞书内置命令。命中返回 True（已回复，调用方跳过 Agent）。

        命令与 CLI 完全对称：/new 开新会话、/sessions 列表、/switch <n> 切换、
        /clear 清当前会话。回复经 bus 直接回飞书，不经过 Gateway 的 Agent。
        """
        parts = text.split()
        if not parts:
            return False
        cmd = parts[0]
        if cmd not in ("/new", "/sessions", "/switch", "/clear"):
            return False

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
        return True

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

    async def send(self, message: OutboundMessage) -> None:
        """把回复按 chat_id 发回飞书，长消息自动分片。"""
        chat_id = message.chat_id
        text = message.content or ""
        if not chat_id:
            logger.warning("飞书回复缺少 chat_id，已丢弃")
            return

        # 按字符切分；空内容也至少发一条占位，保证调用方收到回执
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
            try:
                # 网络调用放到线程里，避免阻塞主事件循环
                resp = await asyncio.to_thread(
                    self._client.im.v1.message.create, request
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("飞书发送消息失败：%s", exc)
                return
            if not resp.success():
                logger.error("飞书发送失败：code=%s msg=%s", resp.code, resp.msg)
                return
            if i < len(chunks) - 1:
                await asyncio.sleep(_CHUNK_SLEEP)

    async def stop(self) -> None:
        """长连接运行在守护线程，进程退出时自动结束；此处仅做占位清理。"""
        # WSClient 无显式 stop()，守护线程会随主进程退出而终止
        pass
