# TASK-032：voice 渠道真·流式播放（LLM 流式 token → 边攒边切边播）

## 任务卡

- 状态：待开始
- 负责人：小奈
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：f3b688f（main，含 TASK-030 未提交改动）
- 依赖任务：TASK-030（分段播放，已实现待验收）、TASK-017（DashScope 甘雨 TTS）、TASK-027/028（连续对讲/防炸麦）

### 目标

voice 渠道从「等 LLM 全文生成完 → 才切句分段播放」升级为「LLM 流式 token → 边攒边切边合成边播放」——第 1 句在 LLM 还在生成后续内容时就开始出声，体验对齐 web 端浏览器 `collectTtsToken → chooseTtsCut → startTtsSynthesis → playNextTts` 的真·流式链路。

用户可观察结果：喊「小奈小奈」说完话后，**2-3 秒内第 1 句就出声**（不再等 LLM 全文生成完的 5-15 秒），后续句子连续念完。

### 背景

- **当前 voice 渠道是非流式的**：`gateway.py:180` 只给 web 渠道挂 `stream_sink`，voice 为 None → AgentLoop 走离散路径（`_emit_discrete`）等全文 → `OutboundMessage` 出站 → `send()` 收到全文才切句
- **web 端已经是真·流式**：LLM token 逐个推 `stream_sink` → bus.stream_queue → WebSocket → 浏览器 `collectTtsToken` 攒缓冲 → `chooseTtsCut` 切句 → `startTtsSynthesis` 逐段 /api/tts → `playNextTts` 按序播放+预合成
- **AgentLoop 流式能力已就绪**：`_run_streamed` 逐 token 消费 `provider.chat_stream`，`stream_sink` 非 None 即走流式路径——代码现成，voice 渠道只是没接入
- **TASK-030 已做的切句器**（`voice/segments.py`，移植自 `chooseTtsCut`）可直接复用——从「全文切」改为「增量切」
- **乖宝 2026-08-09 验收 TASK-030 时发现**：分段播放虽然砍了「等全文合成」的等待，但没砍「等 LLM 全文生成」的等待（5-15 秒）——这才是"先看到全文才出声"的真正根因
- TASK-030 任务卡明确将此列為非目标（「不改 AgentLoop 流式链路」= 后置里程碑），本任务即是那个后置里程碑

### 非目标

- **不改 AgentLoop 核心逻辑**（`_run_streamed` 已就绪，不重写）
- **不改 LLM provider**（`chat_stream` 已就绪）
- **不改 web 渠道**（web 流式链路正常运行）
- **不改 feishu/weixin/cli 渠道**（它们不需要流式，保持离散路径）
- **不做 ASR 流式**（录音→转写仍等全文，独立问题）
- **不做全链路三段流水线**（ASR 流式→LLM→TTS，后置）

### 允许修改

- `gateway.py`：voice 渠道也挂载 stream_sink（实现不同于 web）
- `channels/voice.py`：新增 token 缓冲 + 增量切句 + 边合成边播放管线；`send()` 改为流式收尾/兜底路径
- `voice/segments.py`：如需从「全文切」适配为「增量切」（攒够一句就返回），可修改
- `bus/queue.py`：如需新事件类型或 sink 签略，可修改
- 测试文件（新增/修改）
- 文档（任务卡 / PROJECT.md / ARCHITECTURE.md / DECISIONS.md）

### 禁止修改

- `agent/loop.py`（`_run_streamed` 已就绪，不改核心循环）
- `agent/providers/`（LLM provider 不改）
- `channels/web.py`（web 流式链路不改）
- `channels/feishu.py` / `channels/weixin.py` / `channels/cli.py`（不改）
- `config.json` / `identity*.md` / `workspace/`
- `voice/kws/`（KWS/录音/VAD/播放器不改，但 player.py 可按需适配连续播放）

### 上下文与约束

- **相关代码入口**：
  - `gateway.py:175-200`：`stream_sink = ... if msg.channel == "web" else None` ← 关键改动点
  - `agent/loop.py:793` / `941-980`：`_run_streamed` 逐 token 推 `stream_sink`
  - `bus/queue.py:136-166`：`stream_queue` / `publish_stream` / `consume_stream`
  - `channels/web.py:662`：`stream_event` 推 WebSocket
  - `webui/index.html:475-478`：`collectTtsToken` 攒缓冲 + `flushTtsSegments`
  - `webui/index.html:429-472`：`chooseTtsCut` 切句算法
  - `voice/segments.py`：Python 版切句器（TASK-030，移植自 `chooseTtsCut`）
  - `channels/voice.py:send()` / `_play_segments()`：当前全文切句分段播放
  - `voice/tts/dashscope_realtime.py:205`：`synthesize` async（WebSocket 流式，不阻塞事件循环）
  - `agent/context.py:104-120`：voice 渠道专属 Prompt

- **相关架构/历史决策**：
  - ARCHITECTURE.md:184：「Web 是唯一启用细粒度流事件的渠道」← 本任务扩展为 voice 也启用
  - ARCHITECTURE.md:146：web TTS 链路描述（浏览器端攒+切+合成+播放）
  - ARCHITECTURE.md:164-168：Gateway → AgentLoop → stream_sink → OutboundMessage 数据流

- **已知风险**：
  1. **stream_sink 的实现差异**：web 的 sink 推 bus.stream_queue → WebSocket；voice 的 sink 需要直接在服务端攒+切+合成+播放，不走 bus.stream_queue（或走但消费端不同）
  2. **连续对讲状态机交互**：当前 `send()` 负责播完→调度下一轮录音；流式后"播完"的判定点变化（最后一段播完才算完，不是 `send()` 收到全文时）
  3. **[END] 标记**：LLM 流式输出中 [END] 可能出现在最后一个 token 里，需要实时检测
  4. **max_voice_chars**：全文长度在流式开始时未知，超长判定需改为增量累计或放弃限制
  5. **工具调用回合**：LLM 可能先输出文本再调工具（流式 token → tool_call 事件），voice 渠道需要处理"先播文本部分，工具执行后再播结果"
  6. **并发安全**：token 事件在 AgentLoop 事件循环中产生，TTS 合成+播放在 voice 渠道——需确认同一事件循环还是跨循环
  7. **错误恢复**：某段合成/播放失败 → 降级文字，但流式下"文字"怎么输出？打印还是跳过继续下一段？

### 验收标准

- [ ] voice 渠道挂载 stream_sink 后，AgentLoop 对 voice 走流式路径（`_run_streamed`）
- [ ] LLM 流式 token 到达 voice 渠道后，攒够一句（标点/字数阈值）即开始送 TTS 合成——不等后续 token
- [ ] 第 1 句合成完成立即播放（play_audio），同时后台继续攒+合成后续句
- [ ] 连续多句按序播放，段间无明显停顿（预合成并发，对齐 web 端上限 2）
- [ ] 每段过 TASK-028 DSP（normalize_playback_pcm）
- [ ] 乖宝实测：说完话后 2-3 秒内第 1 句出声（不再等 LLM 全文 5-15 秒）
- [ ] [END] 标记在流式 token 中被检测并正确处理（剥离标记+退出连续对讲）
- [ ] 工具调用回合：LLM 先输出文本再调工具时，文本部分正常流式播放；工具结果后继续播放（或合理降级）
- [ ] 合成/播放失败降级不静默不崩溃（对齐现有降级语义）
- [ ] 其他渠道（web/feishu/weixin/cli）行为完全不变
- [ ] 专项测试 + 全量测试通过
- [ ] 文档同步（任务卡 / PROJECT.md 能力矩阵 / ARCHITECTURE.md 流式描述 / DECISIONS / MEMORY 指针）

### 必须执行的验证

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests
# 按 docs/DEVELOPMENT.md 验证矩阵：涉及 gateway / bus / channels 改动需检查关联层
```

## 实现方案

### 核心思路

web 端流式链路：`LLM token → stream_sink → bus.stream_queue → WebSocket → 浏览器攒+切+合成+播放`

voice 端复用前半段，改后半段：`LLM token → stream_sink(新) → voice 渠道内部攒+切+合成+播放`

AgentLoop 的 `_run_streamed` 逐 token yield 给 `stream_sink`——这部分**不改**。stream_sink 是一个 `async def sink(event: dict)` 回调，voice 渠道提供一个**服务端版** sink：

### 改动点 1：gateway.py — voice 也挂 stream_sink

```python
# 现在
stream_sink = self._make_stream_sink(msg) if msg.channel == "web" else None

# 改为
if msg.channel == "web":
    stream_sink = self._make_stream_sink(msg)
elif msg.channel == "voice":
    stream_sink = voice_channel.make_token_sink()  # 新方法
else:
    stream_sink = None
```

voice 的 sink 不走 bus.stream_queue（不推 WebSocket），而是直接喂给 voice 渠道内部的 token 管冲。

### 改动点 2：channels/voice.py — token 缓冲 + 增量切句 + 合成播放管线

新增 `make_token_sink()` 返回一个 async callback，内部维护：
- `tts_buffer: str`：攒 token 文本
- 增量切句器：每次 token 到达时尝试切（攒够一句就切出去）
- 合成+播放调度：切出的段立即送 TTS 合成，合成完播放，播放期间后台继续攒+合成下一段

参照 web 端 `collectTtsToken → flushTtsSegments → chooseTtsCut → startTtsSynthesis → playNextTts` 的状态机，但全部在 Python 服务端实现。

`send()` 改为：
- 流式路径已覆盖大部分文本 → `send()` 主要负责收尾（最后一段 flush + [END] 处理 + 连续对讲续听调度）
- 若 stream_sink 未挂载（降级/兜底）→ 走现有 TASK-030 全文切句路径

### 改动点 3：voice/segments.py — 增量切句适配

当前 `segment_text(text)` 是全文一次性切。新增（或适配）增量接口：
- `feed(text_part) → list[str]`：每次喂入 token 文本，返回已切出的完整段（可能 0 段、1 段或多段），剩余留在内部缓冲
- `flush() → list[str]`：force 刷出所有剩余文本（对应 web 端 `flushTtsSegments(force=True)`）
- 或者直接在 voice.py 内维护缓冲 + 调用现有 `segment_text` 的切句规则（`_choose_cut`）

### 改动点 4：连续对讲状态机适配

当前 `send()` 播完全文后调度下一轮录音。流式后：
- "播完"的判定点 = 最后一段播放完成（不是 `send()` 收到全文时）
- [END] 检测从"全文检查"改为"token 流中实时检测"
- 静默退出/连续对讲续听的触发时序适配

### 测试策略

- 增量切句器单测：模拟逐 token 喂入，验证切出的段正确、不丢字、flush 刷完
- voice 渠道集成测试：mock stream_sink 逐 token 推入，mock tts_service + play_audio，验证首段先播、并发预合成、失败降级
- gateway 测试：验证 voice 渠道挂载 sink、其他渠道不受影响
- 全量回归

## 风险与遗留问题

1. **stream_sink 跨事件循环**：gateway 主循环与 voice 渠道可能跑在不同事件循环——需确认 sink callback 的线程安全
2. **工具调用回合的流式**：LLM 输出文本 token → 突然 tool_call 事件 → 文本已部分播放 → 工具执行 → 继续输出 → 复杂场景需设计降级策略（如工具调用前已播的文本不回滚，工具结果后的新文本继续流式播）
3. **max_voice_chars 增量判定**：流式开始时全文长度未知，可改为累计已播放字数超限则停（或放弃限制，分段天然控制单段长度）
4. **DashScope TTS 并发上限**：Semaphore(2) 已限制，但流式下合成请求更密集——需验证 DashScope WebSocket 并发不报错
5. **当前 TASK-030 未提交**：TASK-032 基线含 TASK-030 改动，需先验收/提交 TASK-030 或在同一批次提交

## 下一步

- 乖宝说「开始」→ 先确认 TASK-030 验收状态（是否一并提交还是先验收 TASK-030）
- 派 code-master：先读 web 端 `collectTtsToken`/`chooseTtsCut`/`flushTtsSegments`/`pumpTts`/`playNextTts` 全链路 + voice 现有 `_play_segments`，再逐改动点实现
- 注意：**开工前先读 `webui/index.html` TTS 模块代码** + `agent/loop.py` `_run_streamed`，对照移植，不凭记忆重写
