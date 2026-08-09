"""Voice 渠道：本地语音渠道（TASK-024 无音频骨架 → TASK-025 唤醒录音 ASR 闭环）。

本渠道是 NanoClaw 的第五个渠道，定位「本地对讲机」：短平快、无外部服务器
依赖。TASK-024 只做无音频骨架，证明「本地渠道」概念成立——

- 会话 key 用 ``voice:local:<seq>`` 多会话分片（sender_id=``local:<seq>``，
  Gateway 按 ``f"{channel}:{sender_id}"`` 推导出 ``voice:local:<seq>``）；
- 消息经 ``inject_text()`` 入站进 Agent，回复经 ``send()`` / ``_emit()`` 出站。

TASK-025 给渠道装上「耳朵」：可选注入 ``kws_detector``（KwsWakeDetector）与
``asr_service``（AudioTranscriptionService），``start()`` 进入唤醒监听循环——

    唤醒词命中 → 播放确认回应（甘雨音色，方案 B）→ 自动录音 record_sec 秒
    （内存 WAV） → ASR 转写 → ``inject_text()`` 进 Agent → 回复经 ``_emit()``
    出站

唤醒确认回应（2026-08-09 乖宝拍板方案 B）：唤醒后**先合成并播放**一条甘雨
回应（``voice.wake_replies`` 列表 + ``random.choice``，默认「哎，我在呢，你说
吧」），**播完才进入录音**——用户听到回应就知道小奈在，且录音不截用户的话。
``tts_service`` 为 None 或 ``wake_replies`` 为空/非 list 时跳过回应直接录音
（向后兼容既有行为）；合成/播放任一步失败降级为跳过回应继续录音，不阻塞唤醒
流程。回应播放由 ``voice/kws/player.py`` 完成（纯内存流转、播完才返回）。

- ``inject_text()`` 仍是唯一入站口（唤醒回调最终也调用它）；出站统一走
  ``_emit`` 单一出口（``_reply_sink`` 或打印兜底，TASK-026 将替换为 TTS 播放）。
- 音频全程内存流转不落盘；转写文本才进 Agent。
- ``kws_detector`` / ``asr_service`` 任一为 None 时唤醒链路自动禁用，回退
  到 TASK-024 的空转行为（``test_start_idles_until_stop_event`` 保持通过）。

多会话：以整数序号区分不同会话，对应 ``sender_id="local:<seq>"``，Gateway
据此推导出 session_key ``"voice:local:<seq>"``，每个序号对应一个独立会话。
内置命令 ``/new`` 开新会话、``/sessions`` 列表、``/switch <n>`` 切换；
``/clear`` 只清当前会话——与 CLI 渠道语义一致。
"""

import asyncio
import random

from channels.base import Channel
from bus.queue import InboundMessage, OutboundMessage
from voice.asr.base import ASRError
from voice.kws.errors import KwsError
from voice.kws.player import play_audio
from voice.kws.recorder import record_audio
from voice.media import MediaError


class VoiceChannel(Channel):
    """本地语音渠道（name 固定为 "voice"，bus 由构造参数传入）。

    ``inject_text()`` 是唯一入站口（TASK-025 的唤醒录音/ASR 回调也调用它）；
    出站统一走 ``_emit``。``start()`` 在注入 ``kws_detector`` 时进入唤醒监听
    循环，否则空转等待 ``_stop_event``（TASK-024 行为不变）。
    """

    def __init__(
        self,
        bus,
        *,
        kws_detector=None,
        asr_service=None,
        record_sec: float = 8.0,
        kws_device=None,
        tts_service=None,
        wake_replies=None,
    ) -> None:
        super().__init__(name="voice", bus=bus)
        # 停止事件：start() 的监听/空转循环等待它，stop() 置位唤醒
        self._stop_event = asyncio.Event()
        # TASK-025 唤醒闭环注入（任一无则链路自动禁用，回退空转）
        self._kws_detector = kws_detector
        self._asr_service = asr_service
        self._record_sec = float(record_sec) if record_sec else 8.0
        self._kws_device = kws_device  # 录音设备（None=系统默认输入，与 KWS 同源）
        # TASK-025 方案 B：唤醒确认回应（tts_service None / wake_replies 非
        # list 或空 → 跳过回应直接录音，向后兼容既有行为）
        self._tts_service = tts_service
        self._wake_replies = wake_replies
        # 唤醒防抖状态：动作进行中时新唤醒事件合并
        self._wake_in_progress: bool = False
        self._coalesced_wakes: int = 0
        self._wake_count: int = 0

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

        TASK-025 的唤醒录音/ASR 回调将调用本方法；TASK-024 用于测试与手动验证。
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
        """启动渠道。

        - ``kws_detector`` 为 None：空转等待停止事件（TASK-024 行为）。
        - 注入 ``kws_detector``：启动唤醒监听；唤醒 → 录音 → ASR → inject_text
          由 ``_on_wake`` 完成，本方法负责等待停止事件并在期间响应 stop()。
        - 唤醒启动失败（麦克风权限/设备问题）时降级为空转，渠道仍存活。
        """
        detector = self._kws_detector
        if detector is None:
            await self._stop_event.wait()
            return
        try:
            await detector.start(on_wake=self._on_wake)
        except KwsError as exc:
            self._emit(f"📛 唤醒未就绪：{exc.message}")
            self._kws_detector = None  # 降级：仅 inject_text 可用
        except Exception as exc:  # noqa: BLE001 - 启动失败只降级，不崩溃
            self._emit(f"📛 唤醒未就绪：{exc}")
            self._kws_detector = None
        else:
            self._emit("🔊 唤醒监听已启动（喊「小奈小奈」试试）")
        await self._stop_event.wait()

    async def stop(self) -> None:
        """置位停止事件，结束 ``start()`` 的监听/空转；同步停止 KWS 检测器。"""
        self._stop_event.set()
        detector = self._kws_detector
        if detector is not None:
            try:
                await detector.stop()
            except Exception:  # noqa: BLE001 - 停止失败不阻断渠道关闭
                pass

    async def _on_wake(self) -> None:
        """唤醒回调（在事件循环内执行）：动作进行中时新唤醒事件合并。"""
        if self._wake_in_progress:
            self._coalesced_wakes += 1
            return
        self._wake_in_progress = True
        try:
            await self._handle_wake()
        finally:
            self._wake_in_progress = False

    async def _play_wake_reply(self) -> bool:
        """唤醒确认回应（方案 B）：随机挑一条文本合成并播放，播完返回 True。

        - ``tts_service`` 为 None 或 ``wake_replies`` 为空/非 list → 跳过回应
          直接录音（返回 False，向后兼容 TASK-025 既有行为）；
        - 合成/播放任一步失败（TTSError / KwsError / 其它异常）→ 降级跳过回应
          （``_emit`` 一条轻提示），不阻塞唤醒流程。
        - 返回 True 表示回应已**完整播完**，调用方此时才应进入录音。
        """
        tts = self._tts_service
        replies = self._wake_replies
        if tts is None or not isinstance(replies, list) or not replies:
            return False
        try:
            text = random.choice(replies)
            result = await tts.synthesize(text)
            await play_audio(result.audio, result.media_type)
        except Exception as exc:  # noqa: BLE001 - 回应失败只降级，不阻塞唤醒
            self._emit(
                f"🔇 回应播放失败，继续听你说（{getattr(exc, 'message', exc)}）"
            )
            return False
        return True

    async def _handle_wake(self) -> None:
        """唤醒处理：播放确认回应（方案 B）→ 录音 → ASR 转写 → inject_text。

        音频全程内存流转不落盘；转写文本才进 Agent。任一环节失败走 ``_emit``
        友好提示，不回退崩溃。回应在录音之前播放且**播完才进入录音**，保证
        用户听到小奈在、录音不截用户的话。
        """
        self._wake_count += 1
        if self._kws_detector is None or self._asr_service is None:
            return  # 唤醒链路未装配（缺检测器或 ASR），自动禁用

        # 方案 B：先播甘雨确认回应（播完才返回），再开始录音。
        await self._play_wake_reply()

        wav = None
        try:
            wav = await record_audio(self._record_sec, device=self._kws_device)
        except KwsError as exc:
            self._emit(f"📛 录音失败：{exc.message}")
            return
        if not wav:
            self._emit("📛 没录到声音，再说一次？")
            return

        try:
            result = await self._asr_service.transcribe(
                wav, filename="voice_wake.wav", media_type="audio/wav"
            )
        except (ASRError, MediaError) as exc:
            self._emit(f"📛 没听清，再说一次？（{getattr(exc, 'message', exc)}）")
            return
        except Exception as exc:  # noqa: BLE001 - 转写异常只提示不扩散
            self._emit(f"📛 转写异常：{exc}")
            return

        text = ""
        if result is not None:
            text = str(getattr(result, "text", "") or "").strip()
        if not text:
            self._emit("📛 没听清，再说一次？")
            return
        # 转写文本作为正常入站消息进 Agent（同一会话上下文继续）
        await self.inject_text(text)

    async def send(self, message: OutboundMessage) -> None:
        """把出站回复交给 ``_emit``（注入的 ``_reply_sink`` 或打印兜底）。

        TASK-026 将在此处接入 TTS 播放，但 ``_emit`` 单一出口保持不变。
        """
        self._emit(message.content)
