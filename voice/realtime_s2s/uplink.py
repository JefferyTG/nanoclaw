"""实时通话上行（TASK-037）：麦克风 16k PCM int16 → 20ms/包（640B）Base64 上行。

- ``UplinkSender``：把麦克风采集的 16k PCM 字节按 20ms 分包
  （``sample_rate * 0.02 * 2`` = 640B），Base64 内嵌为
  ``input_audio_buffer.append`` 事件发送；不足一包的字节留在缓冲等凑齐。
- 分包/编码逻辑独立为 ``send_pcm``（可单测直接驱动）；麦克风采集走
  ``sd.InputStream`` callback 线程把原始字节推入线程安全队列，``_run``
  循环消费并转发。
复用旧 voice 的采样约定（16k int16 mono），不引入新依赖。
"""

from __future__ import annotations

import asyncio
import base64
import queue

import sounddevice as sd

from loguru import logger


class UplinkSender:
    """16k PCM → 20ms 分包 → Base64 → ``input_audio_buffer.append`` 上行发送器。"""

    def __init__(
        self,
        client,
        *,
        sample_rate: int = 16000,
        chunk_ms: int = 20,
        device=None,
        mic_factory=None,
    ) -> None:
        self._client = client
        self._sample_rate = int(sample_rate)
        # 20ms/包字节数：16000 * 0.02 * 2 = 640
        self._chunk_bytes = max(1, int(sample_rate) * int(chunk_ms) // 1000 * 2)
        self._device = device
        self._mic_factory = mic_factory  # 注入点：返回 {start(), close()} 的采集对象
        self._buffer = bytearray()
        self._in_queue: queue.Queue[bytes] = queue.Queue()
        self._mic = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def chunk_bytes(self) -> int:
        return self._chunk_bytes

    def feed_pcm(self, data: bytes) -> None:
        """麦克风回调线程侧：把原始 PCM 字节推入队列（只入队，不阻塞）。"""
        self._in_queue.put(data)

    async def send_pcm(self, data: bytes) -> None:
        """把 PCM 字节按 20ms 分包发送；不足一包留在缓冲等凑齐。"""
        if not data:
            return
        self._buffer.extend(data)
        while len(self._buffer) >= self._chunk_bytes:
            packet = bytes(self._buffer[: self._chunk_bytes])
            del self._buffer[: self._chunk_bytes]
            await self._client.send_event(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(packet).decode("ascii"),
                }
            )

    async def start(self) -> None:
        """打开麦克风并启动上行循环。"""
        if self._mic is None:
            self._mic = await asyncio.to_thread(self._open_mic)
            logger.debug("realtime 麦克风已打开")
        if self._task is None:
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._run())
            logger.debug("realtime 上行循环已启动")

    async def stop(self) -> None:
        """停止上行循环并关闭麦克风（幂等）。"""
        self._stop_event.set()
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        mic, self._mic = self._mic, None
        if mic is not None:
            try:
                await asyncio.to_thread(mic.close)
            except Exception:  # noqa: BLE001 - 关闭失败不阻断
                pass

    async def _run(self) -> None:
        # 麦克风回调是 PortAudio 线程 → 队列必须线程安全（queue.Queue）；
        # 消费侧在事件循环里，绝不能用阻塞 get（会占死循环），用 get_nowait
        # + sleep 轮询（与 voice/kws/vad.py 的 to_thread 思路同源）。
        while not self._stop_event.is_set():
            try:
                data = self._in_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            try:
                await self.send_pcm(data)
            except Exception as exc:  # noqa: BLE001 - 单包上行失败不杀死循环
                logger.warning(f"realtime 上行发送失败（跳过该包）：{exc}")

    def _open_mic(self):
        if self._mic_factory is not None:
            return self._mic_factory()
        stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            # 20ms 采集块（对齐官方 py demo pyaudio MIC_CHUNK=320 帧@16k）：
            # 100ms 大块会让上行数据滞后最多 100ms，把扬声器回声到达服务端的
            # 时间拖出动态判停免疫窗口 → 外放被误判插话掐断（TASK-038 复盘）
            blocksize=max(320, self._sample_rate // 50),
            device=self._device,
            callback=lambda indata, frames, time_info, status: self.feed_pcm(
                bytes(indata)
            ),
        )
        stream.start()
        return stream
