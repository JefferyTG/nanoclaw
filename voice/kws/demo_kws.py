#!/usr/bin/env python3
"""KWS 唤醒词验证 Demo（TASK-023）

监听麦克风中的「小奈小奈」唤醒词，命中时打印 🔥 唤醒事件。

架构（对齐 docs/VOICE_WAKE_KWS.md 线程模型）：
  PortAudio 回调线程 -> 有界 PCM 队列 -> KWS worker 线程（推理 + 连续命中确认 + 冷却防抖）

用法：
  python voice/kws/demo_kws.py \
    --model-dir voice/kws/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01

依赖安装（可复现）：
  uv add sherpa-onnx sounddevice onnxruntime==1.27.0 sentencepiece pypinyin
  # macOS arm64 需补 onnxruntime dylib 软链（sherpa-onnx 1.13.4 wheel 打包问题）：
  ln -sf ../../onnxruntime/capi/libonnxruntime.1.27.0.dylib \
    .venv/lib/python3.13/site-packages/sherpa_onnx/lib/libonnxruntime.1.27.0.dylib
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sherpa_onnx
import sounddevice as sd


def build_spotter(model_dir: Path, keywords_file: Path, use_int8: bool) -> sherpa_onnx.KeywordSpotter:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="KWS 唤醒词验证 Demo")
    parser.add_argument("--model-dir", type=Path, default=None,
                        help="sherpa-onnx KWS 模型目录（含 tokens.txt / encoder / decoder / joiner）")
    parser.add_argument("--keywords-file", type=Path, default=None,
                        help="关键词文件（默认 <model-dir>/keywords_xiaonai.txt）")
    parser.add_argument("--device", type=int, default=None,
                        help="sounddevice 输入设备索引（默认系统默认输入设备）")
    parser.add_argument("--sample-rate", type=int, default=16000, help="输入采样率（默认 16000）")
    parser.add_argument("--cooldown", type=float, default=2.0,
                        help="触发冷却秒数，冷却期内重复喊只触发一次（默认 2.0）")
    parser.add_argument("--confirm-hits", type=int, default=1,
                        help="连续命中确认次数，防误触发（默认 1，调高更稳）")
    parser.add_argument("--int8", action="store_true", help="使用 int8 量化模型（更快）")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        print("\n默认输入设备:", sd.default.device)
        return 0

    if args.model_dir is None:
        print("❌ 缺少 --model-dir", file=sys.stderr)
        return 1
    model_dir = Path(args.model_dir)
    keywords_file = args.keywords_file or (model_dir / "keywords_xiaonai.txt")
    if not keywords_file.exists():
        print(f"❌ 关键词文件不存在: {keywords_file}", file=sys.stderr)
        return 1
    for required in ("tokens.txt", "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                     "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                     "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"):
        if not (model_dir / required).exists():
            print(f"❌ 模型文件缺失: {model_dir / required}", file=sys.stderr)
            return 1

    print(f"📋 关键词文件: {keywords_file}")
    for line in keywords_file.read_text(encoding="utf-8").strip().splitlines():
        if line.strip():
            print(f"   🎯 {line.split('@')[-1]}")
    print(f"🔧 冷却 {args.cooldown}s / 连续命中确认 x{args.confirm_hits} / int8={'是' if args.int8 else '否'}")
    print("🚀 初始化 KWS 模型…")

    spotter = build_spotter(model_dir, keywords_file, args.int8)
    stream = spotter.create_stream()
    print("✅ 模型就绪，开始监听麦克风（说「小奈小奈」试试）…")
    print("   按 Ctrl+C 退出\n")

    # 有界 PCM 队列：回调线程只入队，绝不阻塞/推理
    q: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
    dropped = 0
    stop_event = threading.Event()

    def audio_callback(indata, frames, time_info, status):
        nonlocal dropped
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        try:
            q.put_nowait(indata.copy())
        except queue.Full:
            dropped += 1

    def kws_worker():
        nonlocal dropped
        hit_streak = 0
        last_trigger = 0.0
        while not stop_event.is_set():
            try:
                block = q.get(timeout=0.5)
            except queue.Empty:
                continue
            samples = block.reshape(-1).astype(np.float32) / 32768.0
            stream.accept_waveform(args.sample_rate, samples)
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                result = spotter.get_result(stream)
                if result:
                    hit_streak += 1
                    if hit_streak >= args.confirm_hits:
                        now = time.monotonic()
                        if now - last_trigger >= args.cooldown:
                            last_trigger = now
                            ts = time.strftime("%H:%M:%S")
                            print(f"🔥 {ts} 唤醒事件: {result}")
                        hit_streak = 0
                    spotter.reset_stream(stream)
                else:
                    hit_streak = 0

    worker = threading.Thread(target=kws_worker, daemon=True)
    worker.start()

    blocksize = max(160, args.sample_rate // 10)  # 0.1s 一帧
    try:
        with sd.InputStream(samplerate=args.sample_rate, channels=1,
                            dtype="int16", blocksize=blocksize,
                            device=args.device, callback=audio_callback):
            while not stop_event.is_set():
                time.sleep(0.2)
    except sd.PortAudioError as e:
        print(f"\n❌ 麦克风打开失败: {e}", file=sys.stderr)
        print("   macOS 首次使用会弹出麦克风权限框，请点「允许」。", file=sys.stderr)
        print("   若已拒绝：系统设置 → 隐私与安全性 → 麦克风 → 允许本终端。", file=sys.stderr)
        print("   设备问题可先跑 --list-devices 确认输入设备。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        worker.join(timeout=2.0)
        print(f"\n👋 退出（队列丢弃帧数: {dropped}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
