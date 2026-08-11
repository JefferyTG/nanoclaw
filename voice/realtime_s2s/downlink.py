"""实时通话下行（TASK-037）：豆包全双工音频直通播放。

- ``DownlinkPlayer`` 消费 ``output_audio.delta``（Base64 内嵌 PCM 24k int16），
  经 sounddevice ``OutputStream`` 连续播放；``response.start / done / cancel``
  驱动「播放中/已结束」状态；
- ``stream_factory`` 为测试注入点（默认 ``sd.OutputStream``），fake 流记录
  写入/停止，便于单测解码与打断逻辑，不真开设备。

输出默认要 PCM 24k（session.create 已配 ``audio.output.format=pcm_s16le`` + ``extension.tts.audio_config``），
无需 ffmpeg 解码，直通播放。
"""

from __future__ import annotations

import asyncio
import base64
import queue
from typing import Callable

import numpy as np
import sounddevice as sd

from loguru import logger

DEFAULT_OUTPUT_SAMPLE_RATE = 24000


class DownlinkPlayer:
    """下行音频流播放器：delta 即收即播，打断即停。"""

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        device=None,
        stream_factory: Callable[[], object] | None = None,
    ) -> None:
        self._sample_rate = int(sample_rate)
        self._device = device
        self._stream_factory = stream_factory
        self._stream = None
        # 对齐官方 py demo：播放队列不设客户端丢帧上限，交给播放线程顺序消费。
        self._chunks: queue.Queue[bytes] = queue.Queue()
        self._response_id: str | None = None
        self._playing: bool = False
        self._writer_task: asyncio.Task | None = None

    # —— 状态 ——

    @property
    def is_playing(self) -> bool:
        """是否有响应正在播放（response.start 起 / response.done·cancel 止）。"""
        return self._playing

    @property
    def response_id(self) -> str | None:
        return self._response_id

    # —— 生命周期 ——

    async def start(self) -> None:
        if self._stream is None:
            self._stream = (
                self._stream_factory()
                if self._stream_factory is not None
                else self._open_default_stream()
            )
            await asyncio.to_thread(self._stream.start)
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        """停止播放并释放资源（幂等）。"""
        if self._writer_task is not None:
            task, self._writer_task = self._writer_task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._drain_chunks()
        self._playing = False
        stream, self._stream = self._stream, None
        if stream is not None:
            for method in ("stop", "close"):
                try:
                    await asyncio.to_thread(getattr(stream, method))
                except Exception:  # noqa: BLE001 - 关闭失败不阻断
                    pass

    # —— 下行事件 ——

    def on_response_start(self, response_id: str | None) -> None:
        """新响应开始：记住 response_id 并进入播放态。

        不 drain——response.done 后队列里可能还有未播完的音频（播放线程
        异步消费），start 时清空会造成尾巴截断；打断场景由
        transcription.started / on_response_cancel 负责打断状态。
        """
        self._response_id = response_id or None
        self._playing = True

    def feed_delta(self, audio_b64: str) -> None:
        """收到 ``output_audio.delta``：Base64 解码 PCM 并入播放队列（不阻塞）。"""
        # 官方 py demo 收到 delta 就入队，不依赖本地 response 状态过滤。
        if not audio_b64:
            return
        try:
            pcm = base64.b64decode(audio_b64)
        except (ValueError, TypeError):
            logger.warning("realtime 下行音频 Base64 解码失败，丢弃该包")
            return
        self._chunks.put_nowait(pcm)

    async def on_output_audio_done(self) -> None:
        """一段音频播完：播放态保留（响应可能还有后续段），等 response.done。"""
        return None

    async def on_response_done(self) -> None:
        """响应结束：退出播放态，但**不清缓冲**。

        - 流常开不 stop（stop 后无法再写，曾致 Stream is stopped）；
        - 不 drain：播放线程是异步消费，response.done 到达时队列里可能还有
          未播完的音频，清空会截断（冒烟：只能听到一句话开头）。
        """
        self._playing = False
        self._response_id = None

    async def on_response_cancel(self) -> None:
        """服务端确认打断（``response.canceled`` 事件）。

        对齐官方 py demo（TASK-038 实测差距）：demo 收到 canceled 只打印日志、
        **不清播放队列**——把已收到的音频继续播完，避免「服务端因回声误判
        插话 → 每句只听开头」。真正的停播时机是 ``transcription.started``
        （服务端确认用户开口），由渠道层驱动。
        """
        self._playing = False
        self._response_id = None
        # 不再 _drain_chunks()：已入队的音频播完（demo 同款行为）

    async def clear_audio_buffer(self) -> None:
        """对齐 py demo：用户开口（transcription.started）只清播放缓冲。

        不改播放态（``_playing`` 保持）、**不发 response.cancel**——服务端
        动态判停自行决定是否取消；若只是回声误报（服务端继续下发 delta），
        播放器仍能继续播完，避免客户端把「疑似插话」升级成实锤取消
        （TASK-038 复盘：py/web 官方 demo 均从不主动发 response.cancel，
        打断完全由服务端全双工动态判停完成）。
        """
        self._drain_chunks()

    # —— 内部 ——

    def _open_default_stream(self):
        return sd.OutputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            device=self._device,
            # 对齐官方 py demo（pyaudio frames_per_buffer=1024@24k≈42.7ms）：
            # 播放缓冲越小，回声返回越早，越贴近服务端判停免疫窗口
            blocksize=1024,
        )

    async def _writer_loop(self) -> None:
        while True:
            try:
                chunk = self._chunks.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            if self._stream is None:
                continue
            try:
                # sd.OutputStream.write 需要 array_like（不接受 raw bytes）：
                # bytes → int16 numpy 数组（同 voice/kws/player.py 口径）；
                # 奇数长度补 0（服务端 delta 包边界不必对齐 2 字节）
                if len(chunk) % 2:
                    chunk += b"\x00"
                data = np.frombuffer(chunk, dtype="<i2")
                await asyncio.to_thread(self._stream.write, data)
            except Exception as exc:  # noqa: BLE001 - 单包写入失败不杀死循环
                logger.warning(f"realtime 下行播放写入失败：{exc}")
                await asyncio.sleep(0.05)

    def _drain_chunks(self) -> None:
        while True:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                return
