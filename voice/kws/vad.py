"""流式静音检测录音（VAD）模块（TASK-027 第一步）。

在 :mod:`voice.kws.recorder` 的固定时长录音之外，新增带静音检测的流式录音：
说话人说完话停顿 ``silence_end_sec`` 即提前结束录音、立即转写，不用干等满
``max_duration_sec``；全程没有检测到有效人声时返回 ``is_silent=True``，
调用方据此退出连续对话。

实现要点：

- ``sd.InputStream``（callback 模式）按块采集 int16 mono PCM，块大小默认约
  50ms；callback 把原始字节拷入线程安全 ``queue.Queue``，消费者循环读帧计算
  RMS（``np.sqrt(np.mean(x**2))``）并运行「安静/有声」状态机。
- 状态机：安静态下任何一帧 RMS >= ``energy_threshold`` 即进入「有声」态并开始
  累积人声时长；有声态下连续静音帧累计达 ``silence_end_sec`` → 提前结束。
  在从未检测到人声帧之前**不会**因静音提前结束，会一直录满 ``max_duration_sec``
  ——保证「全程静默」时调用方仍能拿到足够帧来判定静默并退出连续对话。
- ``is_silent`` 判定：全程累积人声时长 < ``min_voice_sec`` → True。这同时覆盖
  完全没人说话，以及只有短促噪音/咳嗽被误触发（人声总时长不足）两种情况。
- InputStream 是阻塞 API：整个采集+处理循环放进 ``asyncio.to_thread`` 执行，
  不在事件循环里裸调用（参考 recorder.py 的 to_thread 用法）；对外接口仍是
  async。PortAudioError / 设备不可用统一转 :class:`KwsError`（mic_error）。
- 音频只在内存流转、不落盘；``_wrap_wav`` 复用 recorder 的 WAV 封装。
"""

from __future__ import annotations

import asyncio
import queue
import time

import numpy as np
import sounddevice as sd

from voice.kws.errors import KwsError
from voice.kws.recorder import _wrap_wav

_DEFAULT_SILENCE_END_SEC = 1.2
_DEFAULT_ENERGY_THRESHOLD = 400.0
_DEFAULT_MIN_VOICE_SEC = 0.3
_DEFAULT_BLOCK_SEC = 0.05
_BLOCK_SEC_MIN = 0.01
_BLOCK_SEC_MAX = 1.0


def _normalize_positive(value, default: float) -> float:
    """参数归一化为正数；None / 非数字 / NaN / <=0 一律回退默认，保证不崩。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v <= 0:  # NaN 或非正
        return default
    return v


def _normalize_non_negative(value, default: float) -> float:
    """同 :func:`_normalize_positive`，但允许 0（min_voice_sec / energy_threshold 用）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v < 0:
        return default
    return v


def _rms(pcm_bytes: bytes) -> float:
    """int16 PCM 字节的 RMS 能量（root mean square，0 表示完全静音）。"""
    arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


def _capture_audio(
    max_duration_sec: float,
    sample_rate: int,
    device,
    silence_end_sec: float,
    energy_threshold: float,
    min_voice_sec: float,
    block_sec: float,
) -> tuple[bytes | None, bool]:
    """同步采集 + 状态机主循环（在 ``asyncio.to_thread`` 中执行，勿在事件循环里裸调用）。

    返回 ``(pcm_bytes, is_silent)``；pcm_bytes 为 int16 mono PCM（未封装 WAV），
    全程无任何帧时返回 None。参数已在 async 包装层做过归一化校验。
    """
    block_size = max(1, int(block_sec * sample_rate))
    frames_total = int(max_duration_sec * sample_rate)
    q: "queue.Queue[bytes]" = queue.Queue()
    collected = bytearray()

    def callback(indata, frames, time_info, status) -> None:
        # indata: (blocksize, 1) int16；PortAudio 会复用缓冲区，必须立刻拷贝为 bytes
        q.put(bytes(indata))

    voice_time = 0.0  # 全程累积人声时长（秒）
    silent_run = 0.0  # 有声态下连续静音时长（秒）
    in_voice = False
    started_at = time.monotonic()

    try:
        with sd.InputStream(
            samplerate=sample_rate,
            blocksize=block_size,
            channels=1,
            dtype="int16",
            device=device,
            callback=callback,
        ):
            while (len(collected) // 2) < frames_total:
                # 兜底：设备没按预期出帧时也按墙钟退出，避免无限等待
                if time.monotonic() - started_at >= max_duration_sec:
                    break
                try:
                    data = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                frame_count = len(data) // 2
                if frame_count <= 0:
                    continue
                collected.extend(data)
                dur = frame_count / sample_rate
                if _rms(data) >= energy_threshold:
                    voice_time += dur
                    silent_run = 0.0
                    in_voice = True
                elif in_voice:
                    silent_run += dur
                    if silent_run >= silence_end_sec:
                        break  # 说完话停顿够久 → 提前结束
    except sd.PortAudioError as exc:
        raise KwsError(
            "mic_error",
            f"录音失败（请检查麦克风权限/设备）：{exc}",
        ) from exc

    if not collected:
        return None, True
    # 到 max_duration_sec 时可能多出半个块，截断到目标采样数
    pcm = bytes(collected[: frames_total * 2])
    return pcm, voice_time < min_voice_sec


async def record_audio_vad(
    max_duration_sec: float,
    *,
    sample_rate: int = 16000,
    device=None,
    silence_end_sec: float = 1.2,
    energy_threshold: float = 400.0,
    min_voice_sec: float = 0.3,
    block_sec: float = 0.05,
) -> tuple[bytes | None, bool]:
    """流式静音检测录音。

    参数
    ----
    max_duration_sec:
        最长录音时长（秒）。``record_sec`` 变**最长上限**：检测到静默可提前结束；
        到顶仍无静默则正常返回已录内容。
    sample_rate: 采样率（Hz），默认 16000。
    device: sounddevice 输入设备索引；None 表示系统默认输入。
    silence_end_sec:
        有声状态下连续静音达到该时长 → 提前结束录音（默认 1.2s）。
    energy_threshold:
        人声帧的 RMS 能量阈值（默认 400.0）。帧 RMS >= 阈值视为人声。
    min_voice_sec:
        判定「非静默」所需的最短累积人声时长（默认 0.3s）。全程人声时长不足 →
        ``is_silent=True``（防突发噪音误判）。
    block_sec:
        单帧块时长（默认 0.05s ≈ 50ms），即状态机检测粒度。

    返回
    ----
    ``(wav_bytes, is_silent)``

    - ``wav_bytes``: int16 mono PCM 的 WAV bytes。只要采集到任何帧就返回对应的
      WAV（**即使 ``is_silent=True`` 也返回**，便于调用方做存证/后续分析）；
      仅在极端情况下（没有任何帧入队）为 None。
    - ``is_silent``: 全程累积人声时长 < ``min_voice_sec`` → True。True 表示本次
      录音没有有效人声（完全静默，或只有短促噪音/咳嗽被误触发），调用方应据此
      退出连续对话；False 表示检测到有效人声。

    提前结束只发生在「已进入有声状态之后」：从开始到第一个有效人声帧之间的静音
    不会触发提前结束，录音会一直持续到 ``max_duration_sec``，从而保证全程静默
    时能录到足够的帧供调用方判断。

    异常
    ----
    麦克风不可用 / 无权限（PortAudioError）→ :class:`KwsError`（mic_error）；
    ``max_duration_sec`` 非正或采样点数不足 → :class:`KwsError`（invalid_duration）。
    """
    if max_duration_sec <= 0:
        raise KwsError("invalid_duration", "录音时长必须为正数。")
    sr = int(sample_rate)
    if sr <= 0:
        raise KwsError("invalid_duration", "采样率必须为正数。")
    if int(float(max_duration_sec) * sr) <= 0:
        raise KwsError("invalid_duration", f"录音时长过短：{max_duration_sec}s。")

    # 非法参数回退默认，保证不崩
    silence_end_sec = _normalize_positive(silence_end_sec, _DEFAULT_SILENCE_END_SEC)
    energy_threshold = _normalize_non_negative(
        energy_threshold, _DEFAULT_ENERGY_THRESHOLD
    )
    min_voice_sec = _normalize_non_negative(min_voice_sec, _DEFAULT_MIN_VOICE_SEC)
    block_sec = min(
        max(_normalize_positive(block_sec, _DEFAULT_BLOCK_SEC), _BLOCK_SEC_MIN),
        _BLOCK_SEC_MAX,
    )

    pcm, is_silent = await asyncio.to_thread(
        _capture_audio,
        float(max_duration_sec),
        sr,
        device,
        silence_end_sec,
        energy_threshold,
        min_voice_sec,
        block_sec,
    )
    if pcm is None:
        return None, True
    return _wrap_wav(pcm, sr), is_silent
