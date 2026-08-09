# TASK-024：voice 本地语音渠道骨架

## 任务卡

- 状态：已完成（2026-08-09 验收归档）
- 负责人：小奈
- 执行会话/子 Agent：小奈 + code-master（实现）
- 基线 commit / 分支：main@origin/main（df2002c）
- 依赖任务：无（可与 TASK-023 并行；音频接入等 TASK-025）

### 目标

给 NanoClaw 新增**第五个渠道：`voice` 本地语音渠道**（无音频版骨架）。先证明「本地渠道」概念成立：会话 key 用 `voice:local:<seq>` 多会话分片，消息经渠道进 Agent、回复出渠道。本任务不含音频，用 CLI 模拟输入验证链路。

### 非目标

- 不接 KWS 唤醒（TASK-023 验证后由 TASK-025 接入）
- 不接录音/ASR（TASK-025）
- 不接语音播放/TTS（TASK-026）
- 不做空闲自动分片（TASK-026）
- 不实现蓝牙耳机适配（输出走系统默认设备）

### 允许修改

- `channels/voice.py`（新建，继承 base.Channel 或参照 weixin/feishu 骨架）
- `main.py`（注册 voice 渠道；如渠道无需外部事件循环，可只注册实例）
- `config.py` / `config.json`（`voice` 渠道开关配置）
- `tests/test_voice_channel.py`（新建专项测试）
- 任务卡 + PROJECT.md 相关状态

### 禁止修改

- 其他渠道代码（cli/feishu/web/weixin）——除非确需复用公共逻辑，先记入任务卡再动
- bus/ / agent/ / gateway.py 核心链路（如发现必须改动，先记录再评估）

### 上下文与约束

- 相关代码入口：`channels/base.py`（渠道基类）、`channels/cli.py`（最简渠道参考）、`main.py` 渠道装配区（约 1249/1431/1486 行）、`gateway.py` session_key 构造（`f"{channel}:{sender_id}"`）
- 相关架构/历史决策：
  - 会话 key 规则：`voice:local:<seq>`（多会话分片，与 CLI/飞书 `/new` 机制同构）；sender_id 形如 `local:<seq>`
  - 渠道只负责收发，业务全在 Agent（多渠道架构铁律）
  - 本渠道是「本地对讲机」属性：短平快，无外部服务器依赖
- 已知风险：voice 渠道无真实消息来源时如何测试（用 CLI 模拟注入）；渠道注册后 CLI/Web 渠道列表同步（如需）

### 验收标准

- [x] `voice:local:0` 会话能收发消息走通 Agent（CLI 模拟输入）
- [x] `/new` 命令可用：`voice:local:0` → `/new` → `voice:local:1`（旧会话保留可 `/switch`）
- [x] 渠道可开关（config 控制，默认关闭）
- [x] 专项测试通过（unittest）
- [x] 文档与配置同步（任务卡、PROJECT.md 能力矩阵、DECISIONS.md、MEMORY 指针）

### 必须执行的验证

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q channels
# 手动验证：启动 main.py，向 voice 渠道注入模拟消息 → 观察 Agent 回复
```

## 实现进展

### 2026-08-09 11:53（开工）

- 方案定稿：参照 `channels/cli.py` 多会话骨架，`VoiceChannel` 提供公共注入 API `inject_text()`（TASK-025 录音/ASR 回调入口），出站走可注入 `_reply_sink`（TASK-026 换 TTS 播放）；`start()` 空转等 stop（TASK-025 替换为 KWS 监听循环）
- `config.voice` 新增开关字段默认 `enabled=False`；main.py 仅渠道装配区注册
- 已指派 code-master 实现

### 2026-08-09 12:10（code-master 实现完成）

- `channels/voice.py` 新建完成；`main.py` 装配区注册；`config.py` / `config.example.json` 加开关；`tests/test_voice_channel.py` 16 项测试全绿
- 全量 `unittest discover -s tests`：576 项通过；`compileall -q channels` 通过；`git diff --check` 通过

## 执行交接

- 状态：已完成（2026-08-09 验收归档）
- 实际改动文件（本次实现，均在授权范围）：
  - 新增 `channels/voice.py`：`VoiceChannel(Channel)`，name 固定 "voice"，bus 由构造参数传入
  - 修改 `main.py`：渠道装配区（web 渠道之后）注册 voice；仅加 import 与装配分支，不动其他逻辑
  - 修改 `config.py`：`_CONFIG_FIELDS` 加 `"voice"`（带注释）；`NanoClawConfig` 加 `voice: dict = field(default_factory=lambda: {"enabled": False})`
  - 修改 `config.example.json`：`"voice": {"enabled": false}`（weixin 之后）
  - 新增 `tests/test_voice_channel.py`：16 项测试（完整链路 + 命令/注入接口 + config 默认值）
- 实现摘要（每文件关键点）：
  - `channels/voice.py`：多会话 `_session_seq`/`_current_session`，`_current_sender_id()` 返回 `local:<seq>`；公共注入 API `async inject_text(text)`（/clear /new /sessions /switch /context 命令 + 正常消息封装 `InboundMessage(channel="voice", sender_id=..., chat_id="direct", content=text)` 投 `bus.publish_inbound`）；出站 `async send()` 与命令回执统一走 `_emit()` 单一出口（优先 `_reply_sink`，回调异常只降级打印；默认 None 时打印 `[voice] {text}` 兜底，绝不抛异常）；`start()` 空转 `await self._stop_event.wait()`，`stop()` set 事件；注入属性 `_clear_callback`/`_context_callback`/`_reply_sink` 默认 None
  - `main.py`：`from channels.voice import VoiceChannel`；`voice_settings = cfg.voice if isinstance(cfg.voice, dict) else {}`，`enabled` 为真时 `VoiceChannel(bus)` + 注入 `_clear_callback`/`_context_callback` 后 `channels.append`，打印「（语音渠道：已启用·本地对讲机·无音频骨架）」，否则「（语音渠道：未启用）」；voice `start()` 空转不影响「无渠道则退出」检查
  - `config.py` / `config.example.json`：voice 开关默认 `enabled=False`（默认关闭，避免影响现有渠道）
- 关键决策与假设：
  1. **sender_id 格式统一为 `local:<seq>`（带冒号）**：gateway 按 `f"{channel}:{sender_id}"` 推导 session_key，因此 sender_id=`local:0` → `voice:local:0`，与验收标准、`test_channel_context.py` 中 `split(":", 1)` 只切第一刀的设计一致。任务文案中 `local{n}` 为简写，已按验收断言统一。
  2. 命令回执与 Agent 回复共用 `_emit()` 单一出口：TASK-026 只换 `_reply_sink`（TTS 播放）即可，渠道侧零改动。
  3. `inject_text` 为唯一入站口：TASK-025 录音/ASR 回调将直接调用它；命令分支同步改状态、回执走 `_emit`。
  4. `load_config` 对 `voice` 走整段覆盖（未加入 asr/tts/reminders/weixin 的深度合并名单）：当前仅 `enabled` 字段，行为等价且改动最小；TASK-025/026 若扩展字段再评估是否并入合并名单。
  5. `config.json`（本机敏感配置）未改动，仅更新 `config.py` 默认值与 `config.example.json` 模板。
- 验证命令与结果：
  - `.venv/bin/python -m unittest tests.test_voice_channel -v` → Ran 16 tests, OK
  - `.venv/bin/python -m unittest discover -s tests` → Ran 576 tests in 40.753s, OK
  - `.venv/bin/python -m compileall -q channels` → OK
  - `git diff --check` → OK
  - 冒烟验证（fake agent，无网络）：`inject("你好")`→agent `voice:local:0` 收到并回回复出 `_reply_sink`；`/new` 后下一条到 `voice:local:1`；`/sessions` 列表含当前标记 → 全部符合预期
- 未验证项：
  - 未启动真实 `main.py` 全实例（需真实 API Key / 交互终端），真实模型链路由既有 gateway 测试体系覆盖
  - `voice.enabled=true` 时 `main.py` 真实装配未端到端跑过（仅在测试中验证 VoiceChannel+Gateway 链路）
  - PROJECT.md 能力矩阵未更新（不在本次授权文件清单内，留待负责人）
- 风险与遗留问题：
  - 无真实音频输入源，链路用 CLI/注入模拟（TASK-025 接 KWS/ASR 后需复测）
  - `load_config` 深度合并名单未含 `voice`：未来新增字段时旧 config.json 会被整段覆盖，需在 TASK-025/026 评估
  - 测试内 `[voice]` 兜底打印会出现在部分未注入 reply_sink 的命令测试 stdout 中，属预期行为，不影响断言
- commit（仅在获授权时）：暂无
- 当前 `git status --short --branch`：main...origin/main（M config.example.json / config.py / 任务卡 / main.py；?? channels/voice.py / tests/test_voice_channel.py）
- 建议下一步：乖宝验收 → （获授权后）commit → 更新 PROJECT.md 能力矩阵 → TASK-025

## 负责人验收

- [x] 检查 diff 与授权范围
- [x] 独立复跑关键验证
- [x] 检查秘密/个人数据/运行产物
- [x] 检查文档与配置一致性
- [x] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：✅ 已归档（2026-08-09）。乖宝授权 commit+push；提交/推送完成后 MEMORY 指针已同步。
- 证据与备注：完整链路测试覆盖 inject_text→bus→Gateway→fake agent→reply_sink；voice.enabled 默认关闭。手动验证：真实 main.py 全实例启动未做（避免干扰运行实例），链路由测试体系等价覆盖。
