# TASK-025：唤醒→录音→ASR→对话闭环

## 任务卡

- 状态：待开始
- 负责人：小奈
- 执行会话/子 Agent：小奈 + code-master（实现）
- 基线 commit / 分支：main@origin/main（df2002c）
- 依赖任务：TASK-023（KWS 可行）+ TASK-024（voice 渠道骨架）

### 目标

打通「喊「小奈小奈」→ 自动录音 → ASR 转文字 → 进 Agent 对话」的入站闭环。这是语音对讲机的「耳朵」：唤醒事件接进 voice 渠道，唤醒后自动录一段音频，复用现有 ASR 服务转写，作为 InboundMessage 进入 Agent。

### 非目标

- 不做语音回复/TTS 播放（TASK-026）
- 不做空闲自动分片（TASK-026）
- 不做唤醒词训练/优化（TASK-023 已定词）
- 不做蓝牙专用适配（输入用系统默认麦克风，蓝牙尽力而为）

### 允许修改

- `voice/kws/`（KWS worker 模块：回调→有界队列→worker→asyncio 唤醒事件，按 docs/VOICE_WAKE_KWS.md 架构）
- `channels/voice.py`（接 KWS 唤醒事件 + 录音 + ASR）
- `voice/asr/` 复用现有 ASR 服务（不重写）
- `main.py` / `config.py`（voice 渠道注入 asr_service）
- `tests/`（KWS 队列/冷却/唤醒事件测试 + voice 渠道音频测试）
- 任务卡 + PROJECT.md 相关状态

### 禁止修改

- 其他渠道代码
- ASR/TTS 服务核心实现（只注入复用，不改造）

### 上下文与约束

- 相关代码入口：`voice/asr/service.py`（AudioTranscriptionService，飞书语音入站同款）、`channels/feishu.py` 语音入站处理（参考）、`docs/VOICE_WAKE_KWS.md`（KWS 架构冻结候选）
- 相关架构/历史决策：
  - KWS 架构：PortAudio 回调只拷贝帧→有界队列（满丢帧记 metric）→worker 重采样+KWS 推理+连续命中确认+冷却→`loop.call_soon_threadsafe` 投递 asyncio 唤醒事件；唤醒期间新事件合并
  - 唤醒后动作：自动录音固定时长（如 5~10 秒，可配置）→ ASR 转写
  - 音频只在内存，默认不落盘（隐私铁律）
- 已知风险：录音起点可能截掉头字（唤醒后立即录音有延迟）→ 考虑唤醒词后预录音缓冲（先简单实现，标注遗留）；蓝牙麦克风延迟

### 验收标准

- [ ] 唤醒事件能接进 voice 渠道（不再需要 CLI 模拟）
- [ ] 唤醒后自动录音 → ASR 转写成功（说话内容进 Agent）
- [ ] Agent 能收到语音转写的内容并回复（文字）
- [ ] 音频不落盘（内存流转，临时文件即用即删）
- [ ] KWS 队列满丢帧不阻塞回调、冷却防抖生效（测试覆盖）
- [ ] 专项测试通过；文档同步

### 必须执行的验证

```bash
.venv/bin/python -m unittest discover -s tests
# 手动端到端：启动 → 对着麦克风喊「小奈小奈」→ 说「今天天气怎么样」→ 观察 Agent 收到并回复
```

## 执行交接

- 状态：待开始
- 实际改动文件：无（待开工）
- 实现摘要：无
- 关键决策与假设：录音时长可配置（默认 8s）；先内置麦克风验证
- 验证命令与结果：未执行
- 未验证项：全部
- 风险与遗留问题：录音截头字、蓝牙延迟
- commit（仅在获授权时）：暂无
- 当前 `git status --short --branch`：main...origin/main（干净）
- 建议下一步：TASK-023/024 验收后开工

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：待验收
- 证据与备注：
