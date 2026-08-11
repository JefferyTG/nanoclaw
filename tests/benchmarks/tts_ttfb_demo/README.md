# TTS 首包延迟（TTFB）对比 demo

> ⚠️ 这是**手动基准测试**（benchmark），不是单元测试！
> 不会被 `unittest discover` 收集（文件不以 `test` 开头）。
> 全量测试只跑 `tests/` 下 `test_*.py`，与本目录互不干扰。

对比两个流式 TTS 方案的**首字出音速度**：
- **阿里**：`qwen3-tts-vc-realtime-2026-01-15`（WebSocket 流式，甘雨复刻音色）
- **豆包**：`seed-tts-2.0`（火山引擎 V3 WebSocket 双向流式）

## 原理（端到端首字延迟）

贴近真实「边说边播」场景，按用户设定：

1. 用 `config.json` 里的 LLM（`deepseek-v4-flash`）**流式**讲一个超长笑话
2. **LLM 开始流式回复（第一个 content token 到达）→ 开始计时**
3. 收到第一句完整句子 → 立刻喂给 TTS（阿里 commit / 豆包 TaskRequest）
4. **TTS 放出第一包音频 → 停表**

三个指标：
- **端到端**：LLM 首 token → TTS 首包（用户感知的核心指标）
- **TTS TTFB**：文本提交给 TTS → TTS 首包（TTS 单环节能力）
- **total**：函数开始（含建连/鉴权）→ TTS 首包

## 用法

```bash
# 在项目根目录执行
.venv/bin/python tests/benchmarks/tts_ttfb_demo/bench_tts_ttfb.py                 # 端到端模式，默认 3 轮
.venv/bin/python tests/benchmarks/tts_ttfb_demo/bench_tts_ttfb.py --rounds 5      # 5 轮更稳
.venv/bin/python tests/benchmarks/tts_ttfb_demo/bench_tts_ttfb.py --chars 400     # 控制笑话长度
.venv/bin/python tests/benchmarks/tts_ttfb_demo/bench_tts_ttfb.py --text "文本"    # 纯 TTS 模式：跳过 LLM，直接测 TTS
```

## 豆包凭据（环境变量，二选一）

```bash
# 新版控制台（推荐）：https://console.volcengine.com/speech/new
export VOLC_API_KEY=你的APIKey

# 或旧版控制台：AppId + AccessKey
export VOLC_APP_ID=你的AppId
export VOLC_ACCESS_KEY=你的AccessKey

# 可选：切换音色（默认 zh_female_cancan_mars_bigtts，官方示例音色）
export VOLC_SPEAKER=音色ID
```

豆包音色从[控制台 > 音色库](https://console.volcengine.com/speech/new/voices)获取；
`seed-tts-2.0` 对应的音色列表见火山引擎[大模型音色列表](https://www.volcengine.com/docs/6561/1257544)。

## 已知约束

- **阿里单段限 2000 字符（按 1 汉字 = 2 字符计）**：长文本需在句号处截断（端到端模式只发首句，天然安全）
- **阿里 commit 模式**：`response.created → audio.delta → done`，长文本服务端自动分多个 response
- **豆包 V3 协议**：`StartConnection(1) → ConnectionStarted(50) → StartSession(100) → SessionStarted(150) → TaskRequest(200) → 音频帧(352) → FinishSession(102) → SessionFinished(152)`

## 踩过的坑（已修复）

1. **asyncio 队列线程安全**：SDK 回调在后台线程，`queue.put_nowait` 必须经
   `loop.call_soon_threadsafe` 桥接，否则事件会延迟/丢失（首包被误测成 60s）。
   与 `voice/tts/dashscope_realtime.py` 中 `_DashScopeRealtimeCallback._push` 一致。
2. **LLM 偶发空 content**：deepseek-v4-flash 是推理模型，偶发把回答写进
   `reasoning_content` 而 content 为空，流式模式已过滤（只取 content，忽略 reasoning）。
3. **豆包侧若首次运行格式不符**：脚本会打印原始帧，把报错和原始输出发给小奈即可调试。
