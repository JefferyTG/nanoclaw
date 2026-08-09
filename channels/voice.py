"""Voice 渠道：本地语音渠道（TASK-024 无音频骨架）。

本渠道是 NanoClaw 的第五个渠道，定位「本地对讲机」：短平快、无外部服务器
依赖。TASK-024 只做无音频骨架，证明「本地渠道」概念成立——

- 会话 key 用 ``voice:local:<seq>`` 多会话分片（sender_id=``local:<seq>``，
  Gateway 按 ``f"{channel}:{sender_id}"`` 推导出 ``voice:local:<seq>``）；
- 消息经 ``inject_text()`` 入站进 Agent，回复经 ``send()`` / ``_emit()`` 出站。

本渠道不依赖 Agent 本体：只负责「收文本 → 投递进 bus → 出站回复交给注入的
``_reply_sink``」。``inject_text`` 是唯一入站口（TASK-025 录音/ASR 回调会调用
它）；出站统一走 ``_emit`` 单一出口（``_reply_sink`` 或打印兜底，TASK-026 将
替换为 TTS 播放）。TASK-024 无音频源，``start()`` 空转等待停止事件即可
（TASK-025 会替换为 KWS 监听循环）。

多会话：以整数序号区分不同会话，对应 ``sender_id="local:<seq>"``，Gateway
据此推导出 session_key ``"voice:local:<seq>"``，每个序号对应一个独立会话。
内置命令 ``/new`` 开新会话、``/sessions`` 列表、``/switch <n>`` 切换；
``/clear`` 只清当前会话——与 CLI 渠道语义一致。
"""

import asyncio

from channels.base import Channel
from bus.queue import InboundMessage, OutboundMessage


class VoiceChannel(Channel):
    """本地语音渠道（name 固定为 "voice"，bus 由构造参数传入）。

    TASK-024 无音频输入源，渠道通过 ``inject_text()`` 公共注入 API 接收文本
    入站（测试 / 手动验证用；TASK-025 的录音/ASR 回调也会调用它）。``start()``
    空转等待 ``_stop_event``，保证渠道存活。
    """

    def __init__(self, bus) -> None:
        super().__init__(name="voice", bus=bus)
        # 空转停止事件：start() 等待它，stop() 置位唤醒（TASK-025 换成 KWS 循环）
        self._stop_event = asyncio.Event()
        # 以下属性由 main.py 或测试注入，VoiceChannel 自身不依赖 Agent
        self._clear_callback = None   # 清空历史回调（/clear 命令调用）
        self._context_callback = None  # 上下文占用查询回调（/context 命令调用）
        self._reply_sink = None        # 出站回复回调（默认 None 时打印兜底）

        # —— 多会话状态 ——
        # 会话以整数序号标识，sender_id="local:<seq>" -> Gateway 的 session_key
        # 为 "voice:local:<seq>"。_session_seq 是已创建会话数（也作最新序号），
        # _current_session 是当前活动会话序号。
        self._session_seq: int = 0
        self._current_session: int = 0

    def _current_sender_id(self) -> str:
        """当前活动会话对应的 sender_id（Gateway 据此推导 session_key）。"""
        return f"local:{self._current_session}"

    def _emit(self, text: str) -> None:
        """统一出站出口：优先交给注入的 ``_reply_sink``，否则打印兜底。

        任何出口路径都不抛异常：注入回调异常只降级为打印，打印本身也不应让
        出站分发循环崩溃（TASK-026 将把这里替换为 TTS 播放）。
        """
        if self._reply_sink is not None:
            try:
                self._reply_sink(text)
                return
            except Exception as exc:  # noqa: BLE001 - 注入回调异常只降级打印
                print(f"[voice] 出站回调异常，降级打印：{exc}")
        print(f"[voice] {text}")

    async def inject_text(self, text: str) -> None:
        """公共注入入站口：处理内置命令或封装为 InboundMessage 投递进总线。

        TASK-025 的录音/ASR 回调将调用本方法；TASK-024 用于测试与手动验证。
        命令分支同步改状态、回执走 ``_emit``；正常消息才进 bus。
        """
        text = str(text).strip()
        if not text:
            return

        # —— 内置命令（语义与 CLI 渠道一致，回执统一走 _emit）——
        if text == "/clear":
            if self._clear_callback is not None:
                self._clear_callback(f"voice:local:{self._current_session}")
            self._emit(f"🧹 当前会话 #{self._current_session} 历史已清空")
            return
        if text == "/context":
            # 直接从回调查询当前会话占用并回显，不经过模型。
            if self._context_callback is not None:
                reply = self._context_callback(
                    f"voice:local:{self._current_session}"
                )
                self._emit(f"📊 {reply}")
            else:
                self._emit("📊 当前实例未注入上下文占用回调。")
            return
        if text == "/new":
            # 新建一个空白会话并立即切换过去；旧会话保留（磁盘+缓存），
            # 可经 /switch 切回，不会被丢弃。
            self._session_seq += 1
            self._current_session = self._session_seq
            self._emit(f"🆕 已新建会话 #{self._current_session}"
                       f"（旧会话已保留，可 /switch 切回）")
            return
        if text == "/sessions":
            items = []
            for i in range(self._session_seq + 1):
                mark = " ← 当前" if i == self._current_session else ""
                items.append(f"  会话 #{i}{mark}")
            self._emit("📋 已有会话：\n" + "\n".join(items))
            return
        if text.startswith("/switch"):
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit():
                self._emit("⚠️ 用法：/switch <会话序号>，例如 /switch 0")
                return
            target = int(parts[1])
            if target < 0 or target > self._session_seq:
                self._emit(f"⚠️ 会话 #{target} 不存在"
                           f"（有效范围 0~{self._session_seq}）")
                return
            self._current_session = target
            self._emit(f"🔀 已切换到会话 #{target}")
            return

        # —— 正常输入：封装成入站消息投递进 bus ——
        # sender_id 携带当前会话序号，Gateway 据此派生独立 session_key，
        # 从而不同会话的历史互不干扰。
        msg = InboundMessage(
            channel="voice",
            sender_id=self._current_sender_id(),
            chat_id="direct",
            content=text,
        )
        await self.bus.publish_inbound(msg)

    async def start(self) -> None:
        """空转等待停止事件（TASK-024 无音频源，渠道保持存活即可）。

        TASK-025 会将其替换为 KWS 监听循环。
        """
        await self._stop_event.wait()

    async def stop(self) -> None:
        """置位停止事件，结束 ``start()`` 的空转等待。"""
        self._stop_event.set()

    async def send(self, message: OutboundMessage) -> None:
        """把出站回复交给 ``_emit``（注入的 ``_reply_sink`` 或打印兜底）。

        TASK-026 将在此处接入 TTS 播放，但 ``_emit`` 单一出口保持不变。
        """
        self._emit(message.content)
