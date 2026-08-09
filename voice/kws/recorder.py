"""唤醒后录音模块（TASK-025）。

``record_audio`` 录制固定时长 int16 mono PCM 并封装为 WAV bytes，全程内存
流转、不落盘；``sd.rec`` 是阻塞调用，统一放 ``asyncio.to_thread`` 执行，
PortAudioError 转成 :class:`KwsError` 友好错误抛出。

已知遗留：唤醒后立即录音可能截掉头字（唤醒词结束到录音启动之间有延迟）；
蓝牙单流麦克风与 KWS 输入流并存时尽力而为（macOS 默认设备通常支持多流）。
"""

from __future__ import annotations

import asyncio
import io
import wave

import numpy as np
import sounddevice as sd

from voice.kws.errors import KwsError


def _wrap_wav(pcm: bytes, sample_rate: int) -> bytes:
    """把 int16 PCM 字节封装为单声道 WAV bytes（标准库 wave 写头）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    return buffer.getvalue()


async def record_audio(
    duration_sec: float, sample_rate: int = 16000, device=None
) -> bytes:
    """录制 ``duration_sec`` 秒音频并返回 WAV bytes。

    - ``device`` 为 sounddevice 输入设备索引；None 表示系统默认输入。
    - 麦克风不可用/无权限时抛 :class:`KwsError`（含可读提示）。
    - 音频只在内存，不落盘。
    """
    if duration_sec <= 0:
        raise KwsError("invalid_duration", "录音时长必须为正数。")
    frames = int(float(duration_sec) * int(sample_rate))
    if frames <= 0:
        raise KwsError("invalid_duration", f"录音时长过短：{duration_sec}s。")
    try:
        recording = await asyncio.to_thread(
            sd.rec,
            frames,
            samplerate=int(sample_rate),
            channels=1,
            dtype="int16",
            device=device,
        )
        await asyncio.to_thread(sd.wait)
    except sd.PortAudioError as exc:
        raise KwsError(
            "mic_error",
            f"录音失败（请检查麦克风权限/设备）：{exc}",
        ) from exc
    pcm = np.asarray(recording).reshape(-1).astype("<i2").tobytes()
    return _wrap_wav(pcm, int(sample_rate))
