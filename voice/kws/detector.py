"""KWS 唤醒词检测器（TASK-025）。

把 TASK-023 ``demo_kws.py`` 的可复用逻辑抽成模块化类 :class:`KwsWakeDetector`：

    PortAudio 回调线程（只拷贝帧，绝不阻塞）
      -> 有界 PCM 队列（满丢帧记 dropped，回调只 put_nowait）
      -> KWS worker 线程（推理 + 连续命中确认 + 冷却防抖）
      -> loop.call_soon_threadsafe 投递 asyncio 唤醒事件
      -> on_wake 回调在事件循环内执行；动作进行中时新唤醒事件合并

构造参数与 demo 对齐：model_dir / keywords_file / device / sample_rate /
cooldown_sec / confirm_hits / int8 / max_queue；另提供 ``spotter_factory`` /
``stream_factory`` / ``time_fn`` 三个关键字注入点（默认走真实 sherpa-onnx +
sounddevice，测试可替换为假实现）。

- ``async start(on_wake)``：后台启动 worker 线程 + 输入流；PortAudio 打开
  失败时抛 :class:`KwsError`（含麦克风权限/设备提示），不静默崩溃。
- ``async stop()``：置位停止事件 → 关闭流 → 限时 join worker（不阻塞事件循环）
  → 清引用，可安全重复调用。
"""

from __future__ import annotations

import asyncio
import inspect
import queue
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import numpy as np
import sherpa_onnx
import sounddevice as sd

from voice.kws.errors import KwsError

# 与 demo_kws.py 一致的模型文件清单（int8 时按后缀替换）
_BASE_MODEL_FILES = (
    "encoder-epoch-12-avg-2-chunk-16-left-64",
    "decoder-epoch-12-avg-2-chunk-16-left-64",
    "joiner-epoch-12-avg-2-chunk-16-left-64",
)

# on_wake 回调类型：同步或异步皆可（内部 await 兼容）
OnWakeCallback = Optional[Callable[[], Awaitable[None]]]


def build_spotter(
    model_dir: Path, keywords_file: Path, use_int8: bool
) -> sherpa_onnx.KeywordSpotter:
    """构造 sherpa-onnx KeywordSpotter（与 TASK-023 demo 相同参数）。"""
    suffix = ".int8" if use_int8 else ""
    model_dir = Path(model_dir)
    return sherpa_onnx.KeywordSpotter(
        tokens=str(model_dir / "tokens.txt"),
        encoder=str(model_dir / f"encoder-epoch-12-avg-2-chunk-16-left-64{suffix}.onnx"),
        decoder=str(model_dir / f"decoder-epoch-12-avg-2-chunk-16-left-64{suffix}.onnx"),
        joiner=str(model_dir / f"joiner-epoch-12-avg-2-chunk-16-left-64{suffix}.onnx"),
        keywords_file=str(keywords_file),
        num_threads=2,
        sample_rate=16000,
        feature_dim=80,
        max_active_paths=4,
        keywords_score=1.0,
        keywords_threshold=0.25,
        provider="cpu",
    )


def _portaudio_hint(raw: str) -> str:
    """把 PortAudio 打开失败转成用户可读的中文提示。"""
    hint = (
        "打开麦克风失败。macOS 首次使用会弹出麦克风权限框，请点「允许」；"
        "若已拒绝：系统设置 → 隐私与安全性 → 麦克风 → 允许本终端。"
        "设备问题可先运行 voice/kws/demo_kws.py --list-devices 确认输入设备。"
    )
    return f"{raw}。{hint}"


class KwsWakeDetector:
    """本地 KWS 唤醒词检测器（「小奈小奈」等，关键词文件决定）。"""

    def __init__(
        self,
        model_dir,
        keywords_file=None,
        device=None,
        sample_rate: int = 16000,
        cooldown_sec: float = 2.0,
        confirm_hits: int = 1,
        int8: bool = False,
        max_queue: int = 64,
        *,
        spotter_factory: Optional[Callable] = None,
        stream_factory: Optional[Callable] = None,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        """初始化检测器。

        ``spotter_factory`` / ``stream_factory`` / ``time_fn`` 为测试注入点：
        生产环境保持默认（真实 sherpa-onnx + sounddevice + time.monotonic）；
        注入 ``spotter_factory`` 时跳过模型文件存在性校验（由假实现接管）。
        """
        self._model_dir = Path(model_dir)
        self._keywords_file = (
            Path(keywords_file)
            if keywords_file
            else self._model_dir / "keywords_xiaonai.txt"
        )
        self._device = device
        self._sample_rate = int(sample_rate)
        self._cooldown_sec = float(cooldown_sec)
        self._confirm_hits = max(1, int(confirm_hits))
        self._int8 = bool(int8)
        self._max_queue = int(max_queue)
        self._spotter_factory = spotter_factory
        self._stream_factory = stream_factory
        self._time_fn = time_fn or time.monotonic

        if self._spotter_factory is None:
            # 生产路径：模型/关键词文件必须存在，缺失给出可读错误
            if not self._model_dir.is_dir():
                raise KwsError("model_missing", f"KWS 模型目录不存在：{self._model_dir}")
            suffix = ".int8" if self._int8 else ""
            for name in _BASE_MODEL_FILES:
                required = self._model_dir / f"{name}{suffix}.onnx"
                if not required.is_file():
                    raise KwsError("model_missing", f"KWS 模型文件缺失：{required}")
            if not (self._model_dir / "tokens.txt").is_file():
                raise KwsError("model_missing", f"KWS 模型文件缺失：{self._model_dir / 'tokens.txt'}")
            if not self._keywords_file.is_file():
                raise KwsError("keywords_missing", f"关键词文件不存在：{self._keywords_file}")

        # —— 运行时状态（start() 重置，stop() 清引用）——
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_wake: OnWakeCallback = None
        self._stop_event = threading.Event()
        self._queue: Optional[queue.Queue] = queue.Queue(maxsize=self._max_queue)
        self._dropped: int = 0
        self._status_events: int = 0
        self._coalesced_wakes: int = 0
        self._wake_in_progress: bool = False
        self._hit_streak: int = 0
        self._last_trigger: float = 0.0
        self._last_result: Optional[str] = None
        self._spotter = None
        self._spotter_stream = None
        self._worker: Optional[threading.Thread] = None
        self._stream = None
        self._running: bool = False

    # —— 只读指标 ——

    @property
    def running(self) -> bool:
        return self._running

    @property
    def dropped(self) -> int:
        """队列满被丢弃的音频帧数（回调绝不阻塞，只丢帧计数）。"""
        return self._dropped

    @property
    def status_events(self) -> int:
        """PortAudio 回调上报的流状态事件数（仅计数，不在回调内打印）。"""
        return self._status_events

    @property
    def coalesced_wakes(self) -> int:
        """唤醒动作进行中时被合并（丢弃）的新唤醒事件数。"""
        return self._coalesced_wakes

    @property
    def last_result(self) -> Optional[str]:
        return self._last_result

    # —— 生命周期 ——

    async def start(self, on_wake: OnWakeCallback = None) -> None:
        """启动监听：后台 worker 线程 + PortAudio 输入流。

        ``on_wake`` 在事件循环内被调用（唤醒动作进行中时新事件合并）。
        返回 None 表示监听已就绪并持续运行；失败抛 :class:`KwsError`
        （模型缺失 / 关键词缺失 / 麦克风权限或设备问题）。
        """
        if self._running:
            raise KwsError("already_running", "唤醒检测器已在运行。")
        self._loop = asyncio.get_running_loop()
        self._on_wake = on_wake
        self._stop_event = threading.Event()
        self._queue = queue.Queue(maxsize=self._max_queue)
        self._dropped = 0
        self._status_events = 0
        self._coalesced_wakes = 0
        self._wake_in_progress = False
        self._hit_streak = 0
        self._last_trigger = 0.0
        self._last_result = None
        self._running = True
        try:
            # 构建 spotter（加载 ONNX 模型较慢）放线程，避免卡事件循环
            self._spotter = await asyncio.to_thread(self._build_spotter)
            self._spotter_stream = self._spotter.create_stream()
            self._worker = threading.Thread(target=self._kws_worker, daemon=True)
            self._worker.start()
            self._stream = await asyncio.to_thread(self._open_stream)
        except BaseException as exc:
            # 回滚已启动的资源：置位停止 → join worker → 关闭流 → 清引用
            self._running = False
            self._stop_event.set()
            if self._worker is not None:
                worker = self._worker
                self._worker = None
                await asyncio.to_thread(worker.join, 2.0)
            if self._stream is not None:
                try:
                    await asyncio.to_thread(self._stream.close)
                except Exception:  # noqa: BLE001 - 关闭失败不掩盖原始错误
                    pass
                self._stream = None
            self._spotter = None
            self._spotter_stream = None
            if isinstance(exc, sd.PortAudioError):
                raise KwsError("mic_error", _portaudio_hint(str(exc))) from exc
            raise

    async def stop(self) -> None:
        """停止监听并释放资源：置位停止事件 → 关闭流 → 限时 join worker → 清引用。

        join 通过 ``asyncio.to_thread`` 执行，不阻塞事件循环；可安全重复调用。
        """
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            try:
                await asyncio.to_thread(stream.close)
            except Exception:  # noqa: BLE001 - 关闭失败不阻断停止流程
                pass
        self._stop_event.set()
        if self._worker is not None:
            worker = self._worker
            self._worker = None
            try:
                await asyncio.to_thread(worker.join, 2.0)
            except Exception:  # noqa: BLE001
                pass
        self._running = False
        self._loop = None
        self._on_wake = None
        self._queue = None
        self._spotter = None
        self._spotter_stream = None

    # —— 内部实现 ——

    def _build_spotter(self):
        if self._spotter_factory is not None:
            return self._spotter_factory(self._model_dir, self._keywords_file, self._int8)
        return build_spotter(self._model_dir, self._keywords_file, self._int8)

    def _open_stream(self):
        """创建（并启动）PortAudio 输入流；测试可注入 stream_factory。

        ⚠️ 裸构造 ``sd.InputStream(...)`` 不会自动开始采集：sounddevice 只有
        context manager（``with sd.InputStream(...)``）会在 ``__enter__`` 里
        自动 ``start()``。demo_kws.py 用的是 with 写法；集成到本类时改成裸
        构造返回，必须显式 ``stream.start()``，否则音频回调从不被调用、KWS
        worker 永远等不到数据（TASK-025 乖宝端到端验收复现）。
        """
        if self._stream_factory is not None:
            stream = self._stream_factory()
            stream.start()
            return stream
        blocksize = max(160, int(self._sample_rate) // 10)  # 0.1s 一帧
        stream = sd.InputStream(
            samplerate=int(self._sample_rate),
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            device=self._device,
            callback=self._audio_callback,
        )
        stream.start()
        return stream

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """PortAudio 回调线程：只拷贝帧入队，绝不阻塞/推理/打印。"""
        if status:
            self._status_events += 1
        q = self._queue
        if q is None:
            return
        try:
            q.put_nowait(indata.copy())
        except queue.Full:
            self._dropped += 1

    def _kws_worker(self) -> None:
        """KWS worker 线程：取帧 → 推理 → 连续命中确认 → 冷却 → 投递唤醒。"""
        while not self._stop_event.is_set():
            try:
                block = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._feed_block(block)
            except Exception as exc:  # noqa: BLE001 - 单帧推理异常不杀死 worker
                print(f"[kws] 推理异常（已跳过该帧）：{exc}")

    def _feed_block(self, block) -> None:
        """处理一帧音频：归一化 → KWS 推理 → 命中确认 + 冷却防抖。

        独立为方法便于单元测试直接驱动（注入假 spotter / 时钟）。
        """
        spotter = self._spotter
        stream = self._spotter_stream
        if spotter is None or stream is None:
            return
        samples = np.asarray(block).reshape(-1).astype(np.float32) / 32768.0
        stream.accept_waveform(int(self._sample_rate), samples)
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            result = spotter.get_result(stream)
            if result:
                self._hit_streak += 1
                if self._hit_streak >= self._confirm_hits:
                    now = self._time_fn()
                    if now - self._last_trigger >= self._cooldown_sec:
                        self._last_trigger = now
                        self._last_result = result
                        self._signal_wake()
                    self._hit_streak = 0
                spotter.reset_stream(stream)
            else:
                self._hit_streak = 0

    def _signal_wake(self) -> None:
        """worker 线程侧：把唤醒事件安全投递到 asyncio 事件循环。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_wake)
        except RuntimeError:
            pass  # 事件循环已关闭

    def _schedule_wake(self) -> None:
        """事件循环线程内：创建唤醒分发协程任务。"""
        asyncio.create_task(self._dispatch_wake())

    async def _dispatch_wake(self) -> None:
        """在事件循环内执行 on_wake；动作进行中时新唤醒事件合并（防抖）。"""
        if self._wake_in_progress:
            self._coalesced_wakes += 1
            return
        self._wake_in_progress = True
        try:
            cb = self._on_wake
            if cb is not None:
                result = cb()
                if inspect.isawaitable(result):
                    await result
        finally:
            self._wake_in_progress = False
