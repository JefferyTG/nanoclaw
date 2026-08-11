#!/usr/bin/env python3
"""TTS 首包延迟（TTFB）对比 demo：阿里 qwen3-tts-vc-realtime vs 豆包 seed-tts-2.0

流程：
  1. 用 config.json 里的 LLM（deepseek-v4-flash）生成一个很长的笑话
  2. 同一个笑话文本，分别喂给两家流式 TTS，各跑 N 轮
  3. 记录两个指标：
     - ttfb  ：从「完整文本提交给服务端」到「收到第一包音频」的时长
     - total ：从「函数开始（含建连/鉴权）」到「收到第一包音频」的时长

用法（项目根目录执行）：
  .venv/bin/python tests/benchmarks/tts_ttfb_demo/bench_tts_ttfb.py [--rounds 3] [--chars 500]

豆包凭据（环境变量，二选一）：
  VOLC_API_KEY=xxx                       # 新版控制台 API Key（推荐）
  VOLC_APP_ID=xxx VOLC_ACCESS_KEY=xxx    # 旧版控制台
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# 项目根目录（config.json 所在处）
ROOT = Path(__file__).resolve().parents[3]  # tests/benchmarks/tts_ttfb_demo → 项目根

# ---------------- 1. LLM 生成笑话 ----------------

def _load_llm_config() -> dict[str, str]:
    """从 config.json 读取主模型（中转站/deepseek）配置。"""
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return {
        "base_url": cfg["base_url"].rstrip("/"),
        "api_key": cfg["api_key"],
        "model": cfg["model"],
    }


def generate_joke(chars: int = 800) -> str:
    """让便宜的 deepseek-v4-flash 讲一个很长很长的笑话。"""
    import httpx

    cfg = _load_llm_config()
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "你是幽默大师，擅长讲离谱又好笑的长笑话。"},
            {
                "role": "user",
                "content": (
                    f"请讲一个很长很长的笑话，正文不少于 {chars} 字，"
                    "情节越离谱越好笑越好，可以有转折和包袱。只输出笑话正文，不要任何前缀解释。"
                ),
            },
        ],
        "temperature": 0.9,
        "max_tokens": 3000,
    }
    resp = httpx.post(
        f"{cfg['base_url']}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=180,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    # 推理模型偶发返回空 content：重试一次
    if not text:
        resp = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=180,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
    if not text:
        raise RuntimeError("LLM 生成笑话两次都为空，请检查模型配置")
    # 阿里 qwen 单段限 2000 字符，按「1 汉字 = 2 字符」计：
    # 超限时在句号处安全截断（留 200 余量）
    def _est_chars(s: str) -> int:
        return sum(2 if ord(ch) > 127 else 1 for ch in s)

    if _est_chars(text) > 1800:
        cut_len = 900  # 900 汉字 ≈ 1800 字符
        cut = text[:cut_len]
        idx = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("."))
        text = cut[: idx + 1] if idx > 0 else cut
        while _est_chars(text) > 1800 and len(text) > 100:  # 极端情况再收一档
            text = text[: len(text) - 50]
    print(f"  🤣 笑话已生成：{len(text)} 字（模型 {cfg['model']}）")
    print(f"  开头：{text[:60]}…")
    return text


def stream_llm_first_sentence(
    chars: int = 500,
) -> tuple[float, str, str]:
    """LLM 流式生成笑话，返回 (首 token 到达时刻, 第一句完整句子, 完整文本)。

    计时约定：LLM 开始流式回复（第一个 content token 到达）即开始计时，
    首句拿去喂 TTS，TTS 出第一个字停表——测「边说边播」端到端首字延迟。
    """
    import httpx

    cfg = _load_llm_config()
    payload = {
        "model": cfg["model"],
        "stream": True,
        "messages": [
            {"role": "system", "content": "你是幽默大师，擅长讲离谱又好笑的长笑话。"},
            {
                "role": "user",
                "content": (
                    f"请讲一个很长很长的笑话，正文不少于 {chars} 字，"
                    "情节越离谱越好笑越好，可以有转折和包袱。只输出笑话正文，不要任何前缀解释。"
                ),
            },
        ],
        "temperature": 0.9,
        "max_tokens": 3000,
    }
    t_first: float | None = None
    t_first_sentence: float | None = None
    sentence_buffer = ""
    full_text = ""
    with httpx.stream(
        "POST",
        f"{cfg['base_url']}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=180,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            piece = delta.get("content") or ""
            if not piece:
                continue
            now = time.perf_counter()
            if t_first is None:
                t_first = now  # LLM 开始流式回复 = 计时起点
            full_text += piece
            if sentence_buffer or piece:
                sentence_buffer += piece
                if t_first_sentence is None and any(
                    ch in piece for ch in "。！？!?\n"
                ):
                    t_first_sentence = now
                    # 句子边界可能在 chunk 中间：取当前缓冲的整句
            # 句子已完整（≥1 个结束符）就收手
            if t_first_sentence is not None and len(sentence_buffer) >= 4:
                break
    if t_first is None:
        raise RuntimeError("LLM 流式未返回任何 content")
    # 收完整句：从缓冲里截到第一个句子结束符
    first_sentence = sentence_buffer
    for idx, ch in enumerate(sentence_buffer):
        if ch in "。！？!?\n" and idx >= 1:
            first_sentence = sentence_buffer[: idx + 1]
            break
    first_sentence = first_sentence.strip()
    if not first_sentence:
        first_sentence = sentence_buffer[:80]  # 兜底：没句号就取前 80 字
    print(f"  🤣 LLM 已流式回复（计时起点），首句「{first_sentence[:40]}…」")
    print(f"  完整笑话累计 {len(full_text)} 字（模型 {cfg['model']}）")
    return t_first, first_sentence, full_text


# ---------------- 2. 阿里 qwen3-tts-vc-realtime ----------------

def _load_qwen_tts_config() -> dict[str, str]:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    d = cfg["tts_model"]["dashscope_realtime"]
    return {"api_key": d["api_key"], "model": d["model"], "voice_id": d["voice_id"]}


def _qwen_realtime_factory(*, model: str, callback: Any) -> Any:
    from dashscope.audio.qwen_tts_realtime.qwen_tts_realtime import QwenTtsRealtime

    return QwenTtsRealtime(model=model, callback=callback)


async def bench_qwen_ttfb(text: str, t_start: float | None = None) -> tuple[float, float, float]:
    """返回 (ttfb, total, end_to_end)，单位秒。

    - ttfb      ：TTS 文本提交 → 首包
    - total     ：函数开始（建连）→ 首包
    - end_to_end：t_start（LLM 首 token）→ 首包；t_start 为 None 时等同 total
    """
    import dashscope

    cfg = _load_qwen_tts_config()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    holder: dict[str, float] = {"t_send": None}
    session_ready = threading.Event()

    class Callback:
        """桥接 SDK 后台线程事件到 asyncio 队列（线程安全：必须 call_soon_threadsafe）。"""

        def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
            self._loop = loop
            self._queue = queue

        def _push(self, item) -> None:
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
            except RuntimeError:
                pass  # 事件循环已关闭

        def on_open(self) -> None:
            pass

        def on_close(self, close_status_code, close_msg) -> None:
            self._push(("close", None))

        def on_event(self, message) -> None:
            try:
                if isinstance(message, str):
                    message = json.loads(message)
                if not isinstance(message, dict):
                    return
                mtype = message.get("type")
                if mtype in ("session.created", "session.updated"):
                    session_ready.set()
                elif mtype == "response.audio.delta":
                    self._push(("data", message.get("delta")))
                elif mtype in ("response.done", "response.completed"):
                    self._push(("response_done", None))
                elif mtype in ("session.finished",):
                    self._push(("finished", None))
                elif mtype == "error":
                    self._push(("error", str(message)[:200]))
            except Exception:
                pass

    callback = Callback(loop, queue)
    t0 = time.perf_counter()  # total 起点（函数开始）

    def run_sync() -> None:
        try:
            dashscope.api_key = cfg["api_key"]
            client = _qwen_realtime_factory(model=cfg["model"], callback=callback)
            holder["client"] = client
            client.connect()
            client.update_session(
                voice=cfg["voice_id"],
                mode="commit",
                sample_rate=24000,
                language_type="Chinese",
            )
            if not session_ready.wait(timeout=10.0):
                raise TimeoutError("服务端未确认会话配置")
            client.append_text(text)
            client.commit()  # commit 完成 = 文本已提交
            holder["t_send"] = time.perf_counter()  # ttfb 起点
        except Exception as exc:
            queue.put_nowait(("error", str(exc)[:300]))

    worker = asyncio.create_task(asyncio.to_thread(run_sync))
    ttfb = None
    first_data_time = None
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                raise TimeoutError("等待首包音频超时（60s）")
            if kind == "data" and payload:
                if first_data_time is None:
                    first_data_time = time.perf_counter()
                    if holder["t_send"] is not None:
                        ttfb = first_data_time - holder["t_send"]
                    break
            elif kind == "error":
                raise RuntimeError(payload)
            elif kind == "close":
                raise RuntimeError("连接提前关闭")
    finally:
        worker.cancel()
        client = holder.get("client")
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    if ttfb is None:
        raise RuntimeError("未测到首包时间")
    end_to_end = first_data_time - (t_start if t_start is not None else t0)
    return ttfb, first_data_time - t0, end_to_end


# ---------------- 3. 豆包 seed-tts-2.0（V3 双向流式 WebSocket） ----------------

# 事件编号（openspeech v3 框架）
EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_START_SESSION = 100
EV_FINISH_SESSION = 102
EV_CANCEL_SESSION = 103
EV_TASK_REQUEST = 200
EV_CONNECTION_STARTED = 50
EV_SESSION_STARTED = 150
EV_SESSION_CANCELED = 151
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153
EV_TTS_SENTENCE_START = 350
EV_TTS_SENTENCE_END = 351
EV_TTS_RESPONSE = 352

# 文本事件名（新版文档写法）
EV_NAME_CONNECTION_STARTED = "ConnectionStarted"
EV_NAME_SESSION_STARTED = "SessionStarted"
EV_NAME_SESSION_FINISHED = "SessionFinished"
EV_NAME_SESSION_FAILED = "SessionFailed"

DOUBAO_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
DOUBAO_RESOURCE_ID = "seed-tts-2.0"
# seed-tts-2.0 音色（官方示例音色，可在火山引擎控制台音色库替换）
DOUBAO_SPEAKER = os.environ.get("VOLC_SPEAKER", "zh_female_cancan_mars_bigtts")


def _doubao_headers() -> dict[str, str]:
    """新版控制台 X-Api-Key 或旧版 AppId+AccessKey，二选一。"""
    headers = {"X-Api-Resource-Id": DOUBAO_RESOURCE_ID}
    api_key = os.environ.get("VOLC_API_KEY", "").strip()
    app_id = os.environ.get("VOLC_APP_ID", "").strip()
    access_key = os.environ.get("VOLC_ACCESS_KEY", "").strip()
    if api_key:
        headers["X-Api-Key"] = api_key
    elif app_id and access_key:
        headers["X-Api-App-Id"] = app_id
        headers["X-Api-Access-Key"] = access_key
    else:
        raise RuntimeError(
            "缺少豆包凭据：请设置环境变量 VOLC_API_KEY（新版）或 VOLC_APP_ID+VOLC_ACCESS_KEY（旧版）"
        )
    return headers


def _ev_num(msg: dict) -> int | None:
    """兼容文本事件名与数字事件号。"""
    ev = msg.get("event")
    if isinstance(ev, int):
        return ev
    if isinstance(ev, str):
        name_map = {
            EV_NAME_CONNECTION_STARTED: EV_CONNECTION_STARTED,
            EV_NAME_SESSION_STARTED: EV_SESSION_STARTED,
            EV_NAME_SESSION_FINISHED: EV_SESSION_FINISHED,
            EV_NAME_SESSION_FAILED: EV_SESSION_FAILED,
        }
        return name_map.get(ev)
    return None


def _extract_audio_base64(msg: dict) -> str | None:
    """从 TTSResponse(352) 帧里尝试提取音频 base64（兼容多种嵌套结构）。"""
    data = msg.get("data")
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, str):
            return inner
        if isinstance(inner, dict):
            audio = inner.get("audio") or data.get("audio")
            if isinstance(audio, str):
                return audio
        audio = data.get("audio")
        if isinstance(audio, str):
            return audio
    elif isinstance(data, str):
        return data
    return None


async def bench_doubao_ttfb(text: str, t_start: float | None = None) -> tuple[float, float, float]:
    """返回 (ttfb, total, end_to_end)，单位秒。"""
    import websockets

    headers = _doubao_headers()
    t0 = time.perf_counter()  # total 起点
    connect_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex

    async with websockets.connect(
        DOUBAO_WS_URL, additional_headers=headers, open_timeout=15, max_size=16 * 1024 * 1024
    ) as ws:
        # 1) 建立连接
        await ws.send(json.dumps({"event": EV_START_CONNECTION, "data": {"connect_id": connect_id}}))
        started = await asyncio.wait_for(ws.recv(), timeout=15)
        started = json.loads(started)
        if _ev_num(started) != EV_CONNECTION_STARTED:
            raise RuntimeError(f"连接未建立：{started}")

        # 2) 创建会话
        await ws.send(
            json.dumps(
                {
                    "event": EV_START_SESSION,
                    "data": {
                        "session_id": session_id,
                        "req_params": {
                            "speaker": DOUBAO_SPEAKER,
                            "audio_params": {"format": "pcm", "sample_rate": 24000},
                        },
                    },
                }
            )
        )
        sess_started = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if _ev_num(sess_started) != EV_SESSION_STARTED:
            raise RuntimeError(f"会话未建立：{sess_started}")

        # 3) 发送完整文本（ttfb 起点）
        await ws.send(
            json.dumps({"event": EV_TASK_REQUEST, "data": {"session_id": session_id, "text": text}})
        )
        t_send = time.perf_counter()

        # 4) 收帧直到第一包音频
        ttfb = None
        while True:
            raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            ev = _ev_num(raw)
            if ev == EV_TTS_RESPONSE or ev == 352 or raw.get("event") == "data":
                audio_b64 = _extract_audio_base64(raw)
                if audio_b64:
                    ttfb = time.perf_counter() - t_send
                    break
                # 帧格式与预期不符：打印原文辅助调试
                print(f"  [豆包] 数据帧未解析出音频：{json.dumps(raw, ensure_ascii=False)[:200]}")
            elif ev == EV_SESSION_FAILED:
                raise RuntimeError(f"豆包会话失败：{raw}")
            elif ev == EV_SESSION_FINISHED:
                raise RuntimeError("会话提前结束，未收到音频")

        # 5) 礼貌收尾（best-effort）
        try:
            await ws.send(json.dumps({"event": EV_FINISH_SESSION, "data": {"session_id": session_id}}))
            await asyncio.wait_for(ws.recv(), timeout=5)
            await ws.send(json.dumps({"event": EV_FINISH_CONNECTION, "data": {"connect_id": connect_id}}))
        except Exception:
            pass

    end_to_end = time.perf_counter() - (t_start if t_start is not None else t0)
    return ttfb, time.perf_counter() - t0, end_to_end


# ---------------- 4. 主流程 ----------------

async def main() -> int:
    parser = argparse.ArgumentParser(description="TTS 首包延迟对比 demo")
    parser.add_argument("--rounds", type=int, default=3, help="每家测几轮（默认 3）")
    parser.add_argument("--chars", type=int, default=500, help="笑话目标字数（默认 500；阿里单段限 2000 字符）")
    parser.add_argument("--text", type=str, default="", help="直接指定测试文本，跳过 LLM 生成")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds 至少为 1")

    print("=" * 60)
    print("TTS 首包延迟对比：阿里 qwen3-tts-vc-realtime vs 豆包 seed-tts-2.0")
    print("=" * 60)

    if args.text:
        text = args.text
        print(f"  📝 使用指定文本：{len(text)} 字（纯 TTS 模式，无 LLM 环节）")
        rounds = args.rounds
    else:
        print("  🧠 端到端模式：每轮重新让 deepseek-v4-flash 流式讲笑话…")
        print("     计时起点 = LLM 第一个字蹦出来，终点 = TTS 放出第一个字")
        rounds = args.rounds

    results: dict[str, list[tuple[float, float, float]]] = {"qwen": [], "doubao": []}

    async def _run_round(name: str, bench, results_key: str) -> None:
        for i in range(rounds):
            try:
                if args.text:
                    ttfb, total, e2e = await bench(text)
                else:
                    t_llm_first, first_sentence, _ = stream_llm_first_sentence(args.chars)
                    ttfb, total, e2e = await bench(first_sentence, t_start=t_llm_first)
                results[results_key].append((ttfb, total, e2e))
                print(
                    f"  第 {i + 1} 轮：端到端 {e2e * 1000:7.1f} ms"
                    f" | TTS TTFB {ttfb * 1000:7.1f} ms"
                    f" | 含建连 {total * 1000:7.1f} ms"
                )
            except Exception as exc:
                print(f"  第 {i + 1} 轮失败：{exc}")

    # 阿里
    print("\n[1/2] 阿里 qwen3-tts-vc-realtime（甘雨音色）")
    await _run_round("qwen", bench_qwen_ttfb, "qwen")

    # 豆包
    print("\n[2/2] 豆包 seed-tts-2.0（音色 " + DOUBAO_SPEAKER + "）")
    try:
        _doubao_headers()  # 提前校验凭据
        has_key = True
    except RuntimeError as exc:
        has_key = False
        print(f"  ⚠️ {exc}")
        print("  💡 拿到火山引擎 API Key 后，设置环境变量再跑：")
        print("     VOLC_API_KEY=xxx .venv/bin/python tests/benchmarks/tts_ttfb_demo/bench_tts_ttfb.py")
    if has_key:
        await _run_round("doubao", bench_doubao_ttfb, "doubao")

    # 汇总
    print("\n" + "=" * 60)
    print("📊 汇总（单位 ms）")
    print("=" * 60)
    name_map = {"qwen": "阿里 qwen3-tts-vc-realtime", "doubao": "豆包 seed-tts-2.0"}
    for key in ("qwen", "doubao"):
        rows = results[key]
        if not rows:
            print(f"  {name_map[key]:<28} （无有效数据）")
            continue
        e2e_list = [r[2] * 1000 for r in rows]
        ttfb_list = [r[0] * 1000 for r in rows]
        total_list = [r[1] * 1000 for r in rows]
        print(f"  {name_map[key]:<28}  n={len(rows)}")
        print(f"      端到端  平均 {statistics.mean(e2e_list):7.1f} | 最小 {min(e2e_list):7.1f} | 最大 {max(e2e_list):7.1f}")
        print(f"      TTS TTFB 平均 {statistics.mean(ttfb_list):7.1f} | 最小 {min(ttfb_list):7.1f} | 最大 {max(ttfb_list):7.1f}")
        print(f"      total   平均 {statistics.mean(total_list):7.1f} | 最小 {min(total_list):7.1f} | 最大 {max(total_list):7.1f}")

    # 对比结论（端到端首字延迟）
    if results["qwen"] and results["doubao"]:
        q = statistics.mean([r[2] for r in results["qwen"]]) * 1000
        d = statistics.mean([r[2] for r in results["doubao"]]) * 1000
        faster = "阿里" if q < d else "豆包"
        diff = abs(q - d)
        print(f"\n  🏆 结论（端到端首字延迟）：{faster} 平均快 {diff:.1f} ms（阿里 {q:.1f} vs 豆包 {d:.1f}）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
