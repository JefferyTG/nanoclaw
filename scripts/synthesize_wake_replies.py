#!/usr/bin/env python3
"""一次性脚本：用 DashScope 甘雨音色 TTS 合成 10 条唤醒回应短句，输出 WAV。

用法：``uv run python scripts/synthesize_wake_replies.py``

输出：``workspace/voice/wake_replies/wake_01.wav`` ~ ``wake_10.wav``
"""

import asyncio
import os
import sys
import traceback

# 确保项目根目录在 sys.path 上（scripts/ 子目录运行时需要）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import load_config                     # noqa: E402
from voice.tts.dashscope_realtime import (          # noqa: E402
    DashScopeRealtimeTTSProvider,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_LANGUAGE_TYPE,
)

# 10 条唤醒回应文案（小奈原创甘雨风）
WAKE_REPLIES = [
    "哎，我在呢，你说吧",
    "嗯嗯，我听着呢",
    "怎么啦？我在这儿呢",
    "我在呢～有什么事儿吗",
    "嗯？叫我呀～我在的",
    "来了来了，你说吧",
    "在的呢，我一直都在",
    "怎么了呀？想跟我说什么",
    "嗯哼～想聊什么？说吧",
    "听到啦！什么事儿，你说～",
]

OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "workspace", "voice", "wake_replies")


async def main() -> None:
    # ── 1. 加载配置 ──
    config_path = os.path.join(_PROJECT_ROOT, "config.json")
    cfg = load_config(config_path)

    tts = cfg.tts_model
    ds = tts.get("dashscope_realtime", {})

    # 环境变量 DASHSCOPE_API_KEY 最高优先级覆盖（不落盘）
    api_key = os.environ.get("DASHSCOPE_API_KEY") or ds.get("api_key", "")
    voice_id = ds.get("voice_id", "")
    model = ds.get("model", "qwen3-tts-vc-realtime-2026-01-15")
    sample_rate = ds.get("sample_rate", DEFAULT_SAMPLE_RATE)
    instructions = ds.get("instructions", "") or None
    session_ready_timeout_sec = ds.get("session_ready_timeout_sec", 10.0)
    close_grace_sec = ds.get("close_grace_sec", 5.0)
    overall_timeout_sec = ds.get("overall_timeout_sec", 120.0)
    max_audio_bytes = ds.get("max_audio_bytes", 16 * 1024 * 1024)

    if not api_key:
        print("错误：未找到 DashScope API key（config.json tts_model.dashscope_realtime.api_key "
              "或环境变量 DASHSCOPE_API_KEY）", file=sys.stderr)
        sys.exit(1)
    if not voice_id:
        print("错误：未找到 voice_id（config.json tts_model.dashscope_realtime.voice_id）",
              file=sys.stderr)
        sys.exit(1)

    # ── 2. 构造 Provider ──
    provider = DashScopeRealtimeTTSProvider(
        api_key=api_key,
        voice_id=voice_id,
        model=model,
        sample_rate=sample_rate,
        language_type=DEFAULT_LANGUAGE_TYPE,
        instructions=instructions,
        session_ready_timeout_sec=session_ready_timeout_sec,
        close_grace_sec=close_grace_sec,
        overall_timeout_sec=overall_timeout_sec,
        max_audio_bytes=max_audio_bytes,
    )

    print(f"DashScope Realtime TTS 合成脚本")
    print(f"  model       = {model}")
    print(f"  voice_id    = {voice_id}")
    print(f"  sample_rate = {sample_rate} Hz")
    print(f"  instructions= {instructions!r}")
    print(f"  输出目录     = {OUTPUT_DIR}")
    print(f"  文案数       = {len(WAKE_REPLIES)} 条")
    print("-" * 72)

    # ── 3. 创建输出目录 ──
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── 4. 逐条合成并写入 ──
    success = 0
    failures = 0
    for i, text in enumerate(WAKE_REPLIES, start=1):
        filename = f"wake_{i:02d}.wav"
        filepath = os.path.join(OUTPUT_DIR, filename)
        try:
            result = await provider.synthesize(text)
            wav_bytes = result.audio
            with open(filepath, "wb") as f:
                f.write(wav_bytes)
            size_kb = len(wav_bytes) / 1024
            print(f"[{i:2d}/10] ✅ {filename:14s}  {size_kb:7.1f} KB   文案: {text}")
            success += 1
        except Exception as exc:
            print(f"[{i:2d}/10] ❌ {filename:14s}  失败: {exc}")
            traceback.print_exc()
            failures += 1

    print("-" * 72)
    print(f"完成：成功 {success} 条，失败 {failures} 条")
    if failures > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
