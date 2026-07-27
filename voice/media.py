"""Private, short-lived media normalization for ASR requests."""

import asyncio
import json
import math
from pathlib import Path
from typing import Sequence

from voice.asr.base import ASRAudio


class MediaError(Exception):
    """A validation or conversion error that must not expose command output."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    allowed = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".opus", ".wav", ".webm"}
    return suffix if suffix in allowed else ".bin"


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


async def _run(
    args: Sequence[str], *, timeout_sec: float, failure_category: str
) -> tuple[bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaError("media_unavailable", "本机未安装音频处理工具。") from exc
    except OSError as exc:
        raise MediaError("media_unavailable", "无法启动音频处理工具。") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    except asyncio.TimeoutError as exc:
        await _stop_process(process)
        raise MediaError("media_timeout", "音频处理超时。") from exc

    if process.returncode != 0:
        raise MediaError(failure_category, "音频文件无法处理。")
    return stdout, stderr


async def normalize_to_pcm_wav(
    audio: ASRAudio,
    *,
    directory: str,
    ffmpeg_path: str,
    ffprobe_path: str,
    timeout_sec: float,
    max_duration_sec: float,
) -> ASRAudio:
    """Probe duration and produce a 16 kHz mono signed-16-bit WAV in ``directory``."""

    if not audio.data:
        raise MediaError("input_invalid", "音频内容为空。")

    workdir = Path(directory)
    source = workdir / f"input{_safe_suffix(audio.filename)}"
    output = workdir / "normalized.wav"
    source.write_bytes(audio.data)

    stdout, _ = await _run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index:format=duration",
            "-of",
            "json",
            str(source),
        ],
        timeout_sec=timeout_sec,
        failure_category="media_invalid",
    )
    try:
        probe = json.loads(stdout.decode("utf-8", errors="strict"))
        streams = probe.get("streams") if isinstance(probe, dict) else None
        if not streams:
            raise ValueError("no audio stream")
        duration = float((probe.get("format") or {}).get("duration"))
    except (AttributeError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError("media_invalid", "无法读取音频时长。") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("media_invalid", "音频时长无效。")
    if duration > max_duration_sec:
        raise MediaError("input_too_long", "音频时长超过允许上限。")

    await _run(
        [
            ffmpeg_path,
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
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        timeout_sec=timeout_sec,
        failure_category="media_invalid",
    )
    try:
        normalized = output.read_bytes()
    except OSError as exc:
        raise MediaError("media_invalid", "音频转换未生成有效输出。") from exc
    if not normalized:
        raise MediaError("media_invalid", "音频转换未生成有效输出。")
    return ASRAudio(normalized, "audio.wav", "audio/wav")
