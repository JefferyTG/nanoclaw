# TASK-032：voice 渠道真·流式播放（LLM 流式 token → 边攒边切边播）

## 任务卡

- 状态：✅ 已完成（2026-08-09 验收归档）
- 负责人：小奈
- 执行会话/子 Agent：code-master
- 基线 commit / 分支：9285152（main，TASK-030 已提交+push，工作区干净）
- 依赖任务：TASK-030（分段播放，✅已提交 9285152）、TASK-017（DashScope 甘雨 TTS ✅）、TASK-027/028（连续对讲/防炸麦 ✅）

### 目标

voice 渠道从「等 LLM 全文生成完 → 才切句分段播放」升级为「LLM 流式 token → 边攒边切边合成边播放」——第 1 句在 LLM 还在生成后续内容时就开始出声，体验对齐 web 端浏览器 `collectTtsToken → chooseTtsCut → startTtsSynthesis → playNextTts` 的真·流式链路。

用户可观察结果：喊「小奈小奈」说完话后，**2-3 秒内第 1 句就出声**（不再等 LLM 全文生成完的 5-15 秒），后续句子连续念完。

### 验收标准（逐项检查）

- [x] voice 渠道挂载 stream_sink 后，AgentLoop 对 voice 走流式路径（`_run_streamed`）— gateway.py 已改，voice 渠道挂 `make_token_sink()`
- [x] LLM 流式 token 到达 voice 渠道后，攒够一句即开始送 TTS 合成——不等后续 token — `IncrementalSegmenter.feed()` 增量切句
- [x] 第 1 句合成完成立即播放（play_audio），同时后台继续攒+合成后续句 — 首段即开播设计（`_add_segment(idx=0)` 时立即 `create_task(_play_all())`）
- [x] 连续多句按序播放，段间无明显停顿（预合成并发上限 2） — `asyncio.Semaphore(2)`
- [x] 每段过 TASK-028 DSP（normalize_playback_pcm） — `play_audio` 透传 `playback_params`
- [x] 乖宝实测：说完话后第 1 句出声 — 乖宝 2026-08-09 23:44 确认听到回复；观察到长回复时 `🎀 小奈说（第1段）` 先于灰色全文打印出现（流式生效证据）；短回复时灰色先出（正常，LLM 快速生成完）
- [x] [END] 标记在流式 token 中被检测并正确处理（剥离标记+退出连续对讲） — `_on_token` 中 `_full_text` 拼接实时检测 + 每段 `_strip_end_marker` 双保险
- [x] 工具调用回合：文本部分正常流式播放；工具结果后继续播放（或合理降级） — sink 忽略 tool_call/tool_result 事件，文本 token 正常流式播放
- [x] 合成/播放失败降级不静默不崩溃 — 单段合成失败 `_emit` 文字；播放失败当前及后续段全部 `_emit`
- [x] 其他渠道（web/feishu/weixin/cli）行为完全不变 — 仅 `msg.channel == "voice"` 分支新增，其他分支不变
- [x] 专项测试 + 全量测试通过 — 37 条新增专项 + 全量 815 tests 全过
- [x] 文档同步 — 任务卡归档 + PROJECT.md 能力矩阵更新 + ARCHITECTURE.md 流式描述 + MEMORY 指针

### 实现摘要

**改动文件**（4 源文件 + 3 新测试 + 1 测试调整 + 1 任务卡）：

1. **`gateway.py`**（+15 行）：`_handle_one` 中 voice 渠道分支——从 `_channel_map` 取 voice 实例，调 `make_token_sink()` 获取 sink，不走 bus.stream_queue
2. **`channels/voice.py`**（+257 行）：新增 `_StreamingVoiceSink` 类（回合级隔离状态机）+ `make_token_sink()` 方法 + `send()` 流式收尾适配（`streamed=True` 时 return）
   - `_on_token`：追加 [END] 检测缓冲 → `IncrementalSegmenter.feed()` 增量切句 → 切出段入队+启动合成 → 首段到达即开播
   - `_on_done`：flush 切句器 → 等播放完毕 → `_post_playback()` 处理 [END]/续听
   - `_play_all`：按序播放，等合成完→播→预合成下下段（Semaphore(2)），失败降级 `_emit`
   - `_post_playback`：`_end_detected` → `_exit_continuous()`；否则 `_continuous` → `_schedule_next_listen()`
3. **`voice/segments.py`**（+65 行）：新增 `IncrementalSegmenter` 类——`feed(text_part) → list[str]` 增量切句 + `flush() → list[str]` 强制切完，复用现有 `_choose_cut` 规则，不丢字保证
4. **`tests/test_voice_channel.py`**（+10 行调整）：回归适配
5. **`tests/test_voice_incremental_segments.py`**（新）：15 条增量切句器单测
6. **`tests/test_voice_streaming.py`**（新）：17 条 voice 流式集成测试
7. **`tests/test_gateway_voice_stream.py`**（新）：5 条 gateway 挂 sink 测试

**关键设计决策**：
1. 首段即开播：`_add_segment(idx=0)` 时立即 `asyncio.create_task(self._play_all())`，不等 done 事件 → 实现 2-3 秒首句出声
2. 并发预合成上限 2：`asyncio.Semaphore(2)` 限制同时 2 个 synthesize 在途，对齐 web 端
3. 回合级隔离：每条入站消息 → `make_token_sink()` 创建新 sink 实例，不跨回合复用
4. `send()` 双角色：`streamed=True` 是 no-op（流式已处理）；`streamed=False` 走全文切句（兜底/取消场景）
5. 禁止修改的文件全部未动：`agent/loop.py`、`agent/providers/`、`channels/web.py`、`channels/feishu.py`、`channels/weixin.py`、`channels/cli.py`、`config.json`、`voice/kws/*` 均未修改

**验证结果**：
- `git diff --check`：✅ 无空白错误
- `.venv/bin/python -m unittest discover -s tests`：✅ 815 tests OK
- `compileall`：✅ 编译通过
- `import main`：✅ 冒烟通过

### 遗留问题

1. **首句出声延迟体验调优**：乖宝确认流式生效（长回复时首段先于全文打印出现），但体验「不是特别快」——后续可优化方向：首段切句阈值调低（当前首段 5 字起切）/ TTS provider 级流式合成（provider 暴露流式 PCM → player 边收边播，进一步砍等待，见 followups「TTS 流式播放」）
2. **工具调用回合复杂场景**：LLM 先输出文本再调工具时，文本部分正常流式播放；工具结果后的新文本继续流式播——但工具执行期间可能有间隔，复杂多工具场景未端到端验证
3. **max_voice_chars 流式判定**：当前未在流式路径强制 max_voice_chars 限制（分段天然控制单段长度，全文超长时分段继续播）——如需限制可改为累计已播放字数超限则停
4. **日志系统**（乖宝 2026-08-09 提出）：程序只靠 print 打印调试，已记入 followups 待后续加日志系统
