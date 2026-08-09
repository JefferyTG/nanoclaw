"""唤醒确认回应播放模块（TASK-025 方案 B）。

把 TTS 合成的一段音频（WAV / MP3 等）用 ffmpeg 解码为 24kHz 单声道 int16
PCM，再经 sounddevice 播放到输出设备；**播完才返回**，保证「播完再开始录音」
的时序（用户听到甘雨回应就知道小奈在，且录音不截用户的话）。

- 纯内存流转：ffmpeg 临时文件放在 ``TemporaryDirectory`` 内，即用即删不落盘；
- 两个阻塞调用（``sd.play`` / ``sd.wait``）都放 ``asyncio.to_thread``，不阻塞
  事件循环；
- 输出失败（``sd.PortAudioError``）与解码失败（ffmpeg）统一转
  :class:`KwsError`（含可读提示），由调用方降级为跳过回应继续录音。

TASK-028 播放防炸麦：解码出 int16 PCM 后、``sd.play`` 前先过
``voice/kws/normalize.normalize_playback_pcm`` 做音量归一化 + 限幅——
把满幅/近满幅音频压回目标峰值（默认 0.89 ≈ -1dBFS）内，消除削波爆音；
低响度音频默认只压不抬（不放大底噪）。参数经 ``playback_params`` 传入
（键名与 normalize 关键字参数一致，None 用默认值），向后兼容既有调用。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import numpy as np
import sounddevice as sd

from voice.kws.errors import KwsError
from voice.kws.normalize import normalize_playback_pcm

# 统一播放采样率：甘雨音色原生 24k（dashscope_realtime 输出 audio/wav），
# edge-tts（audio/mpeg）也统一重采样到 24k，保证保真与一致性（TASK-024 非
# 目标：不做蓝牙专用适配，输出走系统默认设备）。
DEFAULT_PLAYBACK_SAMPLE_RATE = 24000

# 已合成音频（TTS 输出）的常见 MIME → 扩展名映射，供 ffmpeg 输入文件命名使用。
_MEDIA_TYPE_SUFFIX = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
}

_FFMPEG_DECODE_TIMEOUT_SEC = 30.0

# 播放缓冲延迟（秒）：sounddevice 默认用设备 low latency（~12ms），在 KWS/
# ASR/TTS 同机负载下容易缓冲区欠载（underrun）导致偶发「噼里啪啦」爆音
# （TASK-028 实测：网页端正常、voice 播放噼啪 → 源音频没问题，问题在播放
# 链路缓冲）。显式加大缓冲（0.15s）扛住 CPU 抖动；对讲机场景播完才继续，
# 延迟无感。
_PLAYBACK_LATENCY_SEC = 0.15


async def _stop_process(process) -> None:
    """Best-effort stop and reap for timeout/cancellation paths."""

    if getattr(process, "returncode", None) is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), 1.0)
        return
    except (AttributeError, ProcessLookupError, asyncio.TimeoutError):
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


async def _decode_to_pcm_s16le(
    audio: bytes,
    media_type: str,
    *,
    directory: str,
    sample_rate: int,
) -> bytes:
    """把一段 TTS 音频解码为 ``sample_rate`` Hz 单声道 int16 PCM 字节。

    参照 ``voice/media.py`` 的 ffmpeg 调用范式：临时文件即用即删、内存流转不
    落盘；失败抛 :class:`KwsError`（含可读提示），不暴露原始命令输出。
    """
    if not audio:
        raise KwsError("audio_empty", "回应音频为空。")
    declared = (media_type or "").split(";", 1)[0].strip().lower()
    suffix = _MEDIA_TYPE_SUFFIX.get(declared, ".bin")
    workdir = Path(directory)
    source = workdir / f"input{suffix}"
    output = workdir / "output.pcm"
    source.write_bytes(audio)

    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(int(sample_rate)),
            "-f",
            "s16le",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise KwsError(
            "media_unavailable", "本机未安装 ffmpeg，无法播放回应音频。"
        ) from exc
    except OSError as exc:
        raise KwsError(
            "media_unavailable", "无法启动 ffmpeg，无法播放回应音频。"
        ) from exc

    try:
        await asyncio.wait_for(
            process.communicate(), _FFMPEG_DECODE_TIMEOUT_SEC
        )
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    except asyncio.TimeoutError as exc:
        await _stop_process(process)
        raise KwsError("media_timeout", "回应音频解码超时。") from exc

    if process.returncode != 0:
        raise KwsError("audio_invalid", "回应音频无法解码。")
    try:
        pcm = output.read_bytes()
    except OSError as exc:
        raise KwsError("audio_invalid", "回应音频解码未生成有效输出。") from exc
    if not pcm:
        raise KwsError("audio_invalid", "回应音频解码未生成有效输出。")
    return pcm


async def play_audio(
    audio_bytes: bytes,
    media_type: str,
    sample_rate: int = DEFAULT_PLAYBACK_SAMPLE_RATE,
    device=None,
    playback_params: dict | None = None,
) -> None:
    """把一段已合成音频解码后播放到输出设备，**播完才返回**。

    - ``audio_bytes``：TTS 合成音频字节（如 TTSResult.audio）；
    - ``media_type``：音频 MIME（如 ``audio/wav`` / ``audio/mpeg``），决定
      ffmpeg 输入文件扩展名；
    - ``sample_rate``：统一输出采样率（默认 24000，甘雨原生 24k 保真）；
    - ``device``：sounddevice 输出设备索引；None 用系统默认输出设备；
    - ``playback_params``：播放前 DSP 覆盖参数（键名与
      ``voice/kws/normalize.normalize_playback_pcm`` 的关键字参数一致：
      ``target_peak`` / ``max_gain_db`` / ``soft_clip``）；None（默认）用
      DSP 内置默认（target_peak=0.89 只压不抬 + soft_clip=True）。
    - 播放失败抛 :class:`KwsError`（含可读提示），由调用方降级。
    """
    if int(sample_rate) <= 0:
        raise KwsError("invalid_rate", "播放采样率必须为正数。")
    with tempfile.TemporaryDirectory() as tmp:
        pcm = await _decode_to_pcm_s16le(
            audio_bytes,
            media_type,
            directory=tmp,
            sample_rate=int(sample_rate),
        )
        # bytes → int16 mono numpy 数组（sd.play 需要 array_like，不接受 raw bytes）
        data = np.frombuffer(pcm, dtype="<i2")
        if data.size == 0:
            raise KwsError("audio_invalid", "回应音频解码为空。")
        # TASK-028 播放防炸麦：解码出的 int16 PCM 在 sd.play 前先过
        # 音量归一化 + 软限幅（满幅音频压回目标峰值内；低响度只压不抬，
        # 不放大底噪）。normalize 返回原数组副本或新数组，sd.play 接受
        # array_like，直接传即可。playback_params=None 用 DSP 默认值。
        data = normalize_playback_pcm(data, **(playback_params or {}))
        try:
            await asyncio.to_thread(
                sd.play,
                data,
                samplerate=int(sample_rate),
                device=device,
                latency=_PLAYBACK_LATENCY_SEC,
            )
            await asyncio.to_thread(sd.wait)
        except sd.PortAudioError as exc:
            raise KwsError(
                "output_error",
                f"回应播放失败（请检查输出设备/音量）：{exc}",
            ) from exc
