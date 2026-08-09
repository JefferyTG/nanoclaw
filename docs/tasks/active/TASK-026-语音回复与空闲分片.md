# TASK-026：语音回复 + 空闲自动分片

## 任务卡

- 状态：待开始
- 负责人：小奈
- 执行会话/子 Agent：小奈 + code-master（实现）
- 基线 commit / 分支：main@origin/main（df2002c）
- 依赖任务：TASK-025（入站闭环）

### 目标

完成语音对讲机最后一公里：Agent 回复用甘雨 TTS 合成并**播放到系统默认输出设备**（macOS 自动路由到蓝牙耳机或扬声器，小奈不关心目标设备）；voice 渠道增加**空闲自动分片**——超过阈值（默认 30 分钟）没消息，自动开新会话（seq+1，旧会话保留），并控制会话保留上限（默认最近 50 段，超了清最老）。

### 非目标

- 不做唤醒词优化/训练
- 不做多设备音频路由（系统默认输出即可）
- 不做跨渠道联动（voice 与微信/飞书独立）

### 允许修改

- `channels/voice.py`（TTS 播放 + 空闲分片 + 保留上限）
- `voice/tts/` 复用现有 TTS 服务（甘雨音色，不重写）
- `voice/kws/` 或独立播放模块（sounddevice 播放）
- `config.py` / `config.json`（`voice.idle_ttl_sec` 默认 1800、`voice.max_sessions` 默认 50、`voice.record_sec`）
- `main.py`（voice 渠道注入 tts_service）
- `tests/`（空闲分片/保留上限/播放 mock 测试）
- 任务卡 + PROJECT.md 相关状态

### 禁止修改

- 其他渠道代码
- TTS 服务核心实现（只注入复用）

### 上下文与约束

- 相关代码入口：`voice/tts/service.py`（TextToSpeechService，甘雨 dashscope_realtime 或 edge-tts）、`channels/cli.py` / `channels/feishu.py` 的 `/new` 多会话机制（seq+1 参考）、`session/manager.py`（clear/list_sessions）
- 相关架构/历史决策：
  - 播放到系统默认输出设备（sounddevice.play）；macOS 自动路由蓝牙/扬声器，代码不感知目标设备
  - 空闲分片用 `/new` 机制（seq+1 开新会话，旧会话保留可回查），不是 `/clear`（清空会丢历史）
  - 分片检查是惰性的：每次入站消息时检查距上次消息时间，超阈值自动 seq+1
  - 会话保留上限：超 `max_sessions` 清理最老语音会话（含图片/视频目录）
- 已知风险：TTS 播放延迟（甘雨实时流式已低延迟）；清理老会话是删除操作，需确认范围（只清 voice 渠道）

### 验收标准

- [ ] Agent 回复文字 → 甘雨 TTS → 播放到默认输出（扬声器/蓝牙耳机自动路由）
- [ ] TTS 失败回退：合成/播放失败时回复文字（不静默）
- [ ] 空闲超时自动分片：模拟超过 `idle_ttl_sec` 无消息 → 下次消息进入新会话 `voice:local:<seq+1>`，旧会话保留
- [ ] 会话保留上限生效：超过 `max_sessions` 清最老 voice 会话（仅 voice 渠道，不动其他渠道）
- [ ] 专项测试通过；文档同步

### 必须执行的验证

```bash
.venv/bin/python -m unittest discover -s tests
# 手动端到端：全程语音对话（说话→小奈甘雨声回答）→ 等/模拟 30 分钟 → 再喊 → 确认是新会话
# 检查 voice 会话文件：voice_local_0/1/2... 独立存在，超上限后最老被清
```

## 执行交接

- 状态：待开始
- 实际改动文件：无（待开工）
- 实现摘要：无
- 关键决策与假设：idle 阈值默认 30 分钟（乖宝意向）；保留上限 50 段（可调）；播放走系统默认输出
- 验证命令与结果：未执行
- 未验证项：全部
- 风险与遗留问题：老会话清理是删除操作，需确认只清 voice 渠道；TTS 播放需要音频设备可用
- commit（仅在获授权时）：暂无
- 当前 `git status --short --branch`：main...origin/main（干净）
- 建议下一步：TASK-025 验收后开工

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：待验收
- 证据与备注：
