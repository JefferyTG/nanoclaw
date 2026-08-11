"""Realtime 渠道：豆包端到端全双工实时语音（TASK-037）。

新渠道内部名 ``realtime``，是 ``Channel`` 子类但 **不走消息总线**：

- ``bus=None``、``send()`` 空实现——全双工 S2S 对话发生在豆包服务端内部
  （语音进→语音出），独立闭环，不经过 Gateway / AgentLoop / 记忆；
- 生命周期仍挂 main.py 统一管理（``start()`` / ``stop()``）；
- KWS 待命与对话**串行**：唤醒命中先停 KWS 释放麦克风 → 建会话 → 全双工
  → 优雅退出（``session.close`` 收到回复再断）→ 重启 KWS 回待命。

数据流：``KWS 待命 → 唤醒 → session.create → 播唤醒回应（本地 WAV 缓存）
→ 全双工（上行 16k/20ms/640B Base64；下行 PCM 24k 直通播放，打断由
豆包服务端动态判停）→ 静默超时 / stop → 优雅关闭 → 回 KWS 待命``。

旧 voice 渠道零改动；两渠道麦克风互斥由 main.py 校验（config 二选一）。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

from loguru import logger

from channels.base import Channel
from voice.kws.player import play_audio

from voice.realtime_s2s.client import DEFAULT_MODEL_VERSION, DEFAULT_VOICE, RealtimeS2SClient
from voice.realtime_s2s.downlink import DEFAULT_OUTPUT_SAMPLE_RATE, DownlinkPlayer
from voice.realtime_s2s.fc_bridge import FcBridge
from voice.realtime_s2s.uplink import UplinkSender

# 默认音色：vivi（中文+日文等多语种，全双工默认推荐）
# 默认音色别名：vivi（与 client.DEFAULT_VOICE 同值，供 main.py/测试引用）
DEFAULT_VOICE_TYPE = DEFAULT_VOICE

# 实时通话只从仓库根目录这一个私人文件读取人设与个人记忆；文件不进 Git。
REALTIME_IDENTITY_FILE = Path(__file__).resolve().parent.parent / "realtime_identity.md"


class RealtimeChannel(Channel):
    """实时通话渠道（name="realtime"，bus=None，send 空实现）。"""

    def __init__(
        self,
        bus,
        *,
        api_key: str = "",
        client_factory=None,
        kws_detector=None,
        voice: str = DEFAULT_VOICE_TYPE,
        model: str = DEFAULT_MODEL_VERSION,
        wake_replies_dir: str | None = None,
        silence_timeout_sec: float = 5.0,
        enable_websearch: bool = False,
        device=None,
        playback_params: dict | None = None,
        uplink_factory=None,
        downlink_factory=None,
        fc_bridge=None,
    ) -> None:
        super().__init__(name="realtime", bus=None)
        self._api_key = api_key
        # client_factory：每次对话新建一个 client（会话一换一），测试注入 fake
        self._client_factory = client_factory or (
            lambda: RealtimeS2SClient(api_key=api_key)
        )
        self._kws_detector = kws_detector
        self._identity_file = REALTIME_IDENTITY_FILE
        self._voice = voice or DEFAULT_VOICE_TYPE
        self._model = model or DEFAULT_MODEL_VERSION
        self._wake_replies_dir = (
            wake_replies_dir
            if wake_replies_dir is not None
            else "workspace/voice/wake_replies/"
        )
        self._wake_audio_cache: list[bytes] | None = None
        self._silence_timeout_sec = float(silence_timeout_sec or 0.0)
        self._enable_websearch = bool(enable_websearch)
        self._device = device
        self._playback_params = playback_params or {}
        self._uplink_factory = uplink_factory
        self._downlink_factory = downlink_factory
        self._fc_bridge = fc_bridge or FcBridge()

        self._stop_event = asyncio.Event()
        self._in_conversation = False
        self._conversation_task: asyncio.Task | None = None
        self._uplink = None
        self._downlink = None
        self._current_client = None
        self._last_user_voice = 0.0
        self._awaiting_response = False  # 用户说完 → 模型回复中（不算静默）
        self._wake_count = 0

    # —— Channel 生命周期 ——

    async def start(self) -> None:
        """KWS 待命循环：检测器就绪则监听唤醒词；否则空转等 stop。"""
        self._stop_event = asyncio.Event()
        if self._kws_detector is not None:
            await self._kws_detector.start(self._on_wake)
        await self._stop_event.wait()

    async def stop(self) -> None:
        """停止渠道：先优雅结束进行中的对话，再停 KWS 回待命。"""
        self._stop_event.set()
        if self._conversation_task is not None:
            task, self._conversation_task = self._conversation_task, None
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._kws_detector is not None:
            await self._kws_detector.stop()

    async def send(self, message) -> None:
        """不走消息总线：出站空实现（全双工回复由豆包直接下发）。"""
        return None

    # —— 唤醒 → 对话 → 退出状态机 ——

    async def _on_wake(self) -> None:
        """唤醒回调（KWS 事件循环内调用）：后台任务进入对话，不阻塞 KWS 分发。"""
        if self._in_conversation:
            logger.debug("realtime 唤醒忽略：已在对话中")
            return
        logger.info("🔔 realtime 唤醒命中，进入全双工对话")
        self._in_conversation = True
        self._conversation_task = asyncio.create_task(self._handle_wake())

    async def _handle_wake(self) -> None:
        """唤醒处理：停 KWS（释放麦克风）→ 对话 → 重启 KWS 回待命。"""
        self._wake_count += 1
        detector = self._kws_detector
        if detector is None:
            return
        try:
            logger.debug("realtime 停 KWS（释放麦克风）")
            await detector.stop()  # KWS 待命与对话串行：先释放麦克风
            await self._run_conversation()
        finally:
            self._in_conversation = False
            # 对话结束回 KWS 待命（除非渠道已被 stop）
            if detector is not None and not self._stop_event.is_set():
                try:
                    await detector.start(self._on_wake)
                    logger.debug("realtime KWS 已重启，回到待命")
                except Exception as exc:  # noqa: BLE001 - KWS 重启失败只告警
                    logger.warning(f"realtime KWS 重启失败，等待下次唤醒：{exc}")

    async def _run_conversation(self) -> None:
        """建立豆包会话 → 播唤醒回应 → 全双工对话 → 优雅关闭。"""
        client = self._client_factory()
        uplink = (
            self._uplink_factory(client)
            if self._uplink_factory is not None
            else UplinkSender(client, device=self._device)
        )
        downlink = (
            self._downlink_factory()
            if self._downlink_factory is not None
            else DownlinkPlayer(
                sample_rate=DEFAULT_OUTPUT_SAMPLE_RATE, device=self._device
            )
        )
        self._current_client = client
        self._uplink = uplink
        self._downlink = downlink
        try:
            instructions = self._load_identity()
            await client.connect()
            logger.debug("realtime client.connect 完成")
            await client.create_session(
                instructions=instructions,
                voice_type=self._voice,
                model=self._model,
                enable_websearch=self._enable_websearch,
                tools=self._fc_bridge.tools,
            )
            logger.debug("realtime create_session 完成，播唤醒回应")
            # 唤醒回应：本地 WAV 缓存免费秒回（播完才进全双工；缓存空跳过）
            await self._play_wake_reply()
            await downlink.start()
            await uplink.start()
            logger.debug("realtime uplink/downlink 已启动")
            logger.info("🎧 realtime 全双工对话进行中（静默超时/stop 退出）")
            await self._conversation_loop(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单次对话失败回到待唤醒
            logger.warning(f"realtime 对话异常退出：{exc!r}")
        finally:
            await self._graceful_close(client, uplink, downlink)
            self._current_client = None
            self._uplink = None
            self._downlink = None

    def _load_identity(self) -> str:
        """读取唯一的实时通话人设源；每轮会话重读，修改后下次唤醒生效。"""
        try:
            instructions = self._identity_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"实时通话人设读取失败：{self._identity_file}"
            ) from exc
        if not instructions:
            raise RuntimeError(f"实时通话人设为空：{self._identity_file}")
        return instructions

    async def _conversation_loop(self, client) -> None:
        """全双工事件循环：下行播放、打断、静默超时退出检测。"""
        self._last_user_voice = time.monotonic()
        self._awaiting_response = False
        # iter_events 内部每 poll_interval（1s）产出 None 心跳做静默超时检查；
        # 直接用 anext 等待，不再 wait_for 包 anext（取消会杀死 generator）。
        events = client.iter_events()
        while True:
            try:
                event = await anext(events)
            except StopAsyncIteration:
                break  # 连接已结束
            if event is None:
                # 静默超时退出：模型没在播、用户持续没说话
                if (
                    self._silence_timeout_sec > 0
                    and time.monotonic() - self._last_user_voice
                    >= self._silence_timeout_sec
                    and self._downlink is not None
                    and not self._downlink.is_playing
                    and not self._awaiting_response  # 模型思考/回复中不静默
                ):
                    logger.info("realtime 静默超时，退出对话回待唤醒")
                    break
                continue

            etype = event.get("type")
            if etype not in ("response.output_audio.delta",):  # delta 高频不打
                logger.debug(f"realtime 下行事件：{etype}")
            downlink = self._downlink
            if downlink is None:
                break
            if etype == "conversation.item.input_audio_transcription.started":
                # 服务端 VAD 判定用户开口（真插话）→ 刷新静默计时 + 清播放缓冲。
                # 对齐官方 py demo：只 clear_queue，**不再发 response.cancel**——
                # 服务端动态判停自行处理打断（demo 从不发 cancel 照样能打断）；
                # 若只是回声误报（服务端继续下发 delta），播放器仍能继续播完，
                # 避免客户端把「疑似插话」升级成实锤取消（TASK-038 复盘根因）。
                self._last_user_voice = time.monotonic()
                await downlink.clear_audio_buffer()
            elif etype == "conversation.item.input_audio_transcription.completed":
                # 用户说完 → 进入「等待模型回复」状态（模型思考多久都不静默）
                self._awaiting_response = True
            elif etype == "response.output_audio.started":
                # 官方协议：响应开始事件（带 response_id）
                downlink.on_response_start(event.get("response_id"))
            elif etype == "response.output_audio.delta":
                # 官方协议：音频字段名是 delta（非 audio）
                downlink.feed_delta(event.get("delta") or "")
            elif etype == "response.output_audio.done":
                await downlink.on_output_audio_done()
            elif etype == "response.canceled":
                # 服务端动态判停事件；客户端不主动发送 response.cancel。
                await downlink.on_response_cancel()
                self._awaiting_response = False
                self._last_user_voice = time.monotonic()  # 回完刷新，给接话窗口
            elif etype == "response.done":
                await downlink.on_response_done()
                self._awaiting_response = False
                self._last_user_voice = time.monotonic()  # 回完刷新，给接话窗口
            elif etype == "response.function_call_arguments.done":
                await self._fc_bridge.handle(event)
            elif etype == "error":
                err = event.get("error") or event
                logger.warning(
                    f"realtime 服务端错误：{json.dumps(err, ensure_ascii=False)[:300]}"
                )
                break  # 服务端错误 → 结束本轮对话

    async def _graceful_close(self, client, uplink, downlink) -> None:
        """优雅退出：session.close → 等 session.closed → 停上行/下行 → 断 ws。"""
        logger.debug("realtime 开始优雅关闭")
        try:
            await client.close_session()
        except Exception as exc:  # noqa: BLE001 - 关闭失败也要继续清理
            logger.warning(f"realtime session 关闭异常：{exc}")
        for name, component in (("uplink", uplink), ("downlink", downlink)):
            try:
                await component.stop()
                logger.debug(f"realtime {name} 已停止")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"realtime {name} 停止异常：{exc}")
        logger.debug("realtime 优雅关闭完成")

    # —— 唤醒回应（本地 WAV 缓存随机播放，同 voice 体验）——

    async def _play_wake_reply(self) -> bool:
        """唤醒回应：本地 WAV 缓存随机播放（免费秒回）；缓存空跳过。"""
        if self._wake_audio_cache is None:
            self._wake_audio_cache = self._load_wake_audio_cache()
        if not self._wake_audio_cache:
            return False
        audio = random.choice(self._wake_audio_cache)
        try:
            await play_audio(audio, "audio/wav", playback_params=self._playback_params)
            return True
        except Exception as exc:  # noqa: BLE001 - 回应失败只跳过，不阻塞对话
            logger.warning(f"realtime 唤醒回应播放失败（跳过）：{exc}")
            return False

    def _load_wake_audio_cache(self) -> list[bytes]:
        """扫描 ``wake_replies_dir`` 下 ``wake_*.wav`` 读 bytes 存列表（同 voice 约定）。"""
        d = self._wake_replies_dir
        if not d or not os.path.isdir(d):
            return []
        files = sorted(
            f for f in os.listdir(d) if f.startswith("wake_") and f.endswith(".wav")
        )
        cache: list[bytes] = []
        for fname in files:
            try:
                with open(os.path.join(d, fname), "rb") as fp:
                    cache.append(fp.read())
            except OSError:
                continue  # 单文件读取失败跳过，不影响其余
        return cache
