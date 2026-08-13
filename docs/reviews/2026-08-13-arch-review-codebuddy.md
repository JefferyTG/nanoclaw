# 架构审查报告（2026-08-13，codebuddy）

> **执行方**：CodeBuddy CLI（hy3 模型，plan 只读模式，未改任何文件）
> **审查对象**：NanoClaw 全项目结构
> **触发背景**：乖宝 2026-08-13 提出「项目应该好好查一遍代码结构」，codebuddy 首单正式任务
> **状态**：已存档供后续排期修复（2026-08-13）
> **相关**：文档漂移项已于 2026-08-13 当日修复并核销（README/ARCHITECTURE/PROJECT/DECISIONS）；P0/P1/P2 代码改进项待排期

---

全部关键结论已通过直接核查确认（`session/manager.py:20`、`channels/web.py:28/208`、`agent/tools/mcp.py:182` 顺序循环、`config.py:237` 默认 512k）。下面是结构化审查报告。

---

# NanoClaw 架构审查报告（只读分析）

## ① 目录总览（规模与职责）

项目为**扁平化 Python 包**（顶层导入，`uv run python main.py` 启动），源码包直接位于仓库根目录。

| 路径 | 规模 | 职责 |
|---|---|---|
| `main.py` | ~1900 行 | 唯一组合根（composition root）：`build_shared()` 构造跨会话共享对象、`make_agent_factory()` 懒构造每会话 `AgentLoop`、`amain()` 编排启动/关闭。还内嵌 Dream 子系统（~300 行）与若干 channel builder |
| `gateway.py` | 根目录 | 运行时枢纽：bus↔agent 桥接，按 `session_key` 管理 `AgentLoop` 实例与并发锁 |
| `config.py` | — | `NanoClawConfig` dataclass + `load_config()`（默认值 < config.json < 环境变量） |
| `bus/queue.py` | 167 行 | 最低层基础设施：3 个 `asyncio.Queue`（inbound/outbound/stream）+ DTO |
| `channels/` | base 52；feishu 1179；weixin 1534；voice 1121；web 993；cli/realtime | 传输适配层，把平台事件转 `InboundMessage`/消费 `OutboundMessage`，理论上是"纯消息搬运" |
| `agent/` | loop 1179；memory 968；tools/*（~16 模块）；另含 context/history/identity/profiles/skills/scene_*/filestore/imagestore/videostore/cache_observability/memory_sync/scene_policy | ReAct 编排 + 工具 + 记忆 + 上下文组装，全部业务逻辑所在 |
| `providers/` | base 145；openai_compat 347；usage | LLM 抽象（ABC），与具体厂商解耦 |
| `session/manager.py` | 264 行 | 每会话历史 JSONL 持久化 |
| `reminders/` | scheduler 323 + models/repository/service/schedule | 自包含提醒领域（RRULE + SQLite + 调度），声明"不含调度循环与通道集成" |
| `voice/` | asr/tts/kws/realtime_s2s 等 | 音频能力：ASR/TTS/唤醒词/实时双工 |
| `webui/` | 不存在 | 文档称有 `webui/index.html`，实际 web 通道是 `channels/web.py`，无独立 webui 包 |
| `integrations/weixin_bridge/` | 不存在 | 文档列此目录，实际微信桥为 `channels/weixin.py`（Node 桥在别处） |
| `skills/` | 运行时目录（gitignored） | 运行时技能目录，默认即 `<workspace>/skills/` |
| `tests/` | 92 个 `test_*.py`，900+ 用例 | unittest，目录镜像生产包结构，`benchmarks/` 隔离 |
| `docs/` `scripts/` `bin/` | — | 文档、工具脚本、`nanoclawctl` 启停 |

**规模结论**：整体分层意图清晰（通道/总线/网关/智能体/提供方/能力），但少数文件过大、`main.py` 承载过多。

---

## ② 核心模块职责与依赖关系

### 依赖边（运行时，内部）
```
bus.queue        → 仅 stdlib（TYPE_CHECKING 下引用 reminders.models）
providers.*      → providers.usage（完全隔离，不依赖 agent/channels/bus/session）
voice.*          → voice.* 内部（完全隔离）
reminders.*      → reminders.* 内部（完全隔离，靠注入回调）
channels.*       → bus.queue；→ voice.*（feishu/voice/web/realtime 直接 import voice.asr/tts/media）
channels/web     → agent.filestore        ★ 反向边
session.*        → agent.history          ★ 反向边
agent.*          → session.manager（loop/search/spawn/filesystem）
agent.tools.spawn→ agent.loop（子代理依赖父编排器）
gateway          → bus.queue, channels.base, agent.identity, agent.loop
main             → 几乎所有模块（唯一知道全部具体类之处）
bus/channels.base ──TYPE_CHECKING──► reminders.models（DeliveryResult 跨层共享 DTO）
```

### 高内聚中枢（高扇入）
- `bus.queue.MessageBus` / `InboundMessage` / `OutboundMessage`：被 ~10 个生产模块引用，总线脊柱。
- `agent.tools.base.Tool`：被 ~16 个工具模块引用，扇入最高的类。
- `reminders.models.DeliveryResult`：跨 bus/gateway/各 channel/reminders/main 约 40 处引用，共享横切 DTO。
- `agent.loop.AgentLoop`、`session.manager.SessionManager`：扇入较高。

### 端到端消息流（以飞书为例，已核对）
1. `FeishuChannel.start()`（`channels/feishu.py:153`）起 WS 守护线程 → `_on_message`（`:192`）。
2. `_publish_text_inbound`（`:462/470`）`bus.publish_inbound(InboundMessage(...))`。
3. `Gateway._process_inbound`（`gateway.py:111/118`）消费，按 `f"{channel}:{sender_id}"` 加 `asyncio.Lock`（`:119/127`）串行化同会话。
4. `_handle_one` → `agent.run(...)`（`gateway.py:196`）。
5. `AgentLoop.run`（`agent/loop.py:299`）→ `_run`（`:692`）ReAct 循环，返回文本。
6. `Gateway.outbound_safe`（`:280/290`）`publish_outbound`。
7. `_dispatch_outbound`（`:328/348`）→ `FeishuChannel.send`（`:825`）分块/发图/可选语音气泡。
8. Web 走 `stream_queue` 增量推送，无需等最终消息。

**结论**：通道/总线/网关/智能体边界在调用层基本成立，总线解耦设计是该项目最健康的架构决策。

---

## ③ 文档–代码一致性核对结果

| # | 文档 | 声称 | 代码事实 | 判定 |
|---|---|---|---|---|
| 1 | `README.md:262` | MCP"多 Server **并行**连接" | `agent/tools/mcp.py:182` 顺序 `for...items()`，无 `asyncio.gather` | **漂移**（已在 DECISIONS NC-DOC-001 记录） |
| 2 | `README.md:286` | "约 **192k** 预算"触发压缩 | `config.py:237` 默认 `524288`（512k）；ARCHITECTURE 正确写 512k | **漂移** |
| 3 | `ARCHITECTURE.md:121` + `DECISIONS.md:189` | `agent/skills/` 历史副本待清理 | `agent/skills/` **不存在**（仅 `agent/skills.py` 加载器） | **陈旧**（清理已完成但未更新文档） |
| 4 | `ARCHITECTURE.md §4` / `PROJECT.md` 表 | 模块/边界图 | 遗漏 `agent/{scene_policy,cache_observability,history,videostore,memory_sync,filestore}.py` 与 `providers/usage.py` | **不完整** |
| 5 | `README.md:330` | `compileall` 列表 | 漏 `reminders`、`voice`（相对 `PROJECT.md:82`） | 次要 |
| 6 | `README.md:181` | TTS"默认关闭" | `config.py:316` `tts_model.enabled=True`（UI 喇叭开关默认关，provider 已就绪） | 歧义 |
| 7 | `ARCHITECTURE.md:7` vs `DECISIONS.md:140` | 基线提交哈希 | `0cd50de` vs `0daefc4` | 设计内指针陈旧（文档已声明 git 为准） |
| — | `PROJECT.md` 模块表 | 是否遗漏 providers/reminders/session/voice/integrations | **均已在表中**（`gateway.py` 确实存在） | 一致 |
| — | 通道默认行为、`web_host`/`max_iterations`/`model`/`turn_timeout_sec`/`base_url` 默认值、unittest 框架、benchmark 隔离、离线/mock 测试设计 | — | 与代码一致 | 一致 |

**一致性总评**：核心架构描述与代码基本吻合；主要问题是 README 两处事实漂移 + 架构模块图遗漏 + `agent/skills` 陈旧条目。NC-TEST-001（无 CI/lint/类型基线）经 `pyproject.toml` 确认属实。

---

## ④ 发现的问题（按 高/中/低 分级，附 file:line）

### 🔴 高
1. **`session` ↔ `agent` 循环依赖**
   `session/manager.py:20` `from agent.history import canonicalize_history...`，同时 `agent/loop.py:48`、`agent/search.py:29`、`agent/tools/spawn.py:38`、`agent/tools/filesystem.py:135` 均 `import session.manager`。当前因 `agent.history` 仅依赖 stdlib 未崩溃，但两包架构性循环耦合；一旦 `agent.history` 反向 import `session.manager` 将致启动崩溃，且 `session` 无法独立测试。
2. **`channels/web.py` 反向依赖 `agent.filestore`（分层倒置）**
   `channels/web.py:28` `from agent.filestore import MAX_FILE_BYTES, FileTooLargeError`；`:208` `from agent.filestore import FileStore`。而 `channels/base.py` 自身的文档约定通道应是"纯粹的消息搬运角色"、业务逻辑应注入。此处通道直接触达 agent 内部存储，破坏该约定。
3. **安全边界弱点（文档已知但未修复）**
   `PROJECT.md` 已记录 `NC-BUG-001`（P0：web `web_host 0.0.0.0` + 无鉴权外泄）与 `NC-SEC-001`（`workspace: "."` 非 OS 沙箱）。属结构性/安全风险，不应仅停留在文档。

### 🟡 中
4. **`bus`/`channels.base` 以 `TYPE_CHECKING` 依赖 `reminders.models`**
   `bus/queue.py:17-18`、`channels/base.py:20-21` 引用 `DeliveryResult`——最低层基础设施类型依赖领域 DTO，属分层倒置（仅类型级，无运行时耦合，但说明 `DeliveryResult` 是缺位共享类型）。
5. **`channels/*` → `voice.*` 传输层依赖能力层**
   如 `channels/feishu.py:48` import `voice.media.encode_to_opus`，把通道绑定到 voice 内部。
6. **大文件混合职责**
   - `channels/feishu.py`（1179 行）：WS 生命周期 + 图像批处理定时器 + 命令处理（`_try_handle_command:706`）+ 提醒绑定回调 + 音频转 opus + 语音回覆 TTS 同处一文件。
   - `agent/loop.py`（1179 行）：编排 + PromptCache 计费（`_build_usage_event:431`/`_emit_usage_event:422`）+ 记忆快照（`_sync_memory_patch:542`/`_apply_memory_patch:629`）+ 磁盘持久化（`_persist:1229`/`_save_to_history:1254`）高度聚集。
7. **重复代码**
   - `_delivery_result(...)` 工厂在 `gateway.py:33-50` 与 `channels/feishu.py:1126` 各定义一份同形实现。
   - `from reminders.models import DeliveryResult` 懒导入散落 6+ 处（`feishu.py:52/1134`、`weixin.py:426/465/554`、`bus/queue.py:18`、`gateway.py:30/42`、`channels/base.py:21`）。
   - 飞书 Lark 上传 async/sync 成对封装（`_upload_audio:1032` vs `_upload_audio_sync:1064`；`_upload_image:931` vs `_upload_image_sync:1144`）。
8. **`agent/tools/spawn.py` 高扇出 + 子代理依赖父编排器**
   约 14 处内部 import（含 `agent.loop:39`），使"生成子代理"工具反向耦合顶层编排器，不利复用与测试。
9. **文档漂移**（见 §3 表 1/2/3/4）：README MCP 并行、192k 预算、agent/skills 陈旧、架构模块图遗漏。

### 🟢 低
10. **测试依赖私有内部**：`tests/channels/test_web_files.py:19` import `_human_file_size`；`tests/channels/voice/test_voice_streaming.py:24` import `_StreamingVoiceSink`；`tests/tools/test_video_tool.py:23` import `_get_field`。重构私有名会破测试。
11. **慢/集成测试仅靠目录隔离**：无 `skipIf`/marker（`Grep skipIf` 无结果），默认 `unittest discover` 混合音频/语音/微信等重测试，`DEVELOPMENT.md` 只能靠"分模块运行"规避。
12. **`voice/{asr,tts}/__init__.py` 纯 re-export 外观**：无逻辑，无害但属"过薄抽象"。
13. **README `compileall` 列表遗漏 `reminders`/`voice`**（相对 `PROJECT.md`）；TTS"默认关闭"措辞歧义（见 §3 表 5/6）。

---

## ⑤ 改进建议（按优先级，注明收益与成本）

### P0 — 解除循环依赖 + 安全加固
- **A. 拆 `session`↔`agent` 循环**：把 `canonicalize_history*` 这类纯函数从 `agent/history.py` 移到 `session/` 或独立的 `history` 包，使 `SessionManager` 不再 import `agent`。
  - 收益：消除潜在启动崩溃、可独立单测 `session`、降低耦合。成本：中（移动函数 + 改 import，影响面小）。
- **B. 加固 web 通道安全边界**：web 绑定 `127.0.0.1` 或加令牌鉴权；在 `config.py`/`main.py` 启动期对 `web_host=0.0.0.0` 无鉴权显式告警。`workspace` 沙箱边界按 AGENTS.md 重新评估，不依赖注释假设。
  - 收益：消 P0 外泄风险（NC-BUG-001）。成本：中（鉴权或绑定改动 + 文档）。

### P1 — 恢复分层 + 减重复 + 文档对齐
- **C. 解除 `channels/web.py` → `agent.filestore`**：文件存储经 Gateway/回调注入，或通过 `OutboundMessage` 传递文件引用，保持通道纯传输。
  - 收益：恢复 `channels/base.py` 约定、降低耦合。成本：中。
- **D. 拆分 `main.py`**：把 channel builders、Dream 子系统抽到 `builders.py`/`dream.py`，`amain` 只编排。
  - 收益：可维护性显著提升，契合"只改授权范围"的协作约定。成本：中。
- **E. 收敛 `DeliveryResult`**：在 `reminders.models` 提供共享构造器，删 `gateway.py`/`feishu.py` 重复实现，统一懒导入点。
  - 收益：减重复、单点修正。成本：低。
- **F. 文档对齐（低成本高收益）**：README MCP"并行"→"顺序"、192k→512k；ARCHITECTURE §4 与 PROJECT.md 表补遗漏模块；清理 `agent/skills` 陈旧条目；README `compileall` 补 `reminders`/`voice`。
  - 收益：文档可信度回升。成本：低（纯文档编辑）。

### P2 — 可维护性深化
- **G. 拆分大文件**：`channels/feishu.py` 抽 `media`/`command` 子模块；`agent/loop.py` 抽 `memory_sync`/`persist`/`cache` 子模块。
  - 收益：单文件可维护、易评审。成本：中高（需保行为不变 + 回归测试）。
- **H. 测试加固**：对重/集成测试加 marker 或 `skipIf` 自动排除；将私有内部改为稳定公开 API 再测试。
  - 收益：默认套件可控、重构安全。成本：中。
- **I. 建立 lint/类型/CI 基线**（落实 NC-TEST-001）：引入 ruff/mypy + 最小 CI。
  - 收益：防止漂移复发。成本：中。

---

### 总体结论
NanoClaw 的**分层意图与总线解耦设计是健康的主干**（`bus`/`providers`/`voice`/`reminders` 均干净隔离）。主要风险集中在三处：**一个真实循环依赖（session↔agent）**、**一处分层倒置（web→filestore）**、以及 **`main.py`/`feishu.py`/`loop.py` 等巨型文件承载过多职责**；文档整体可靠，但 README 有两处事实漂移、架构模块图不完整。建议按 P0→P1→P2 顺序推进，其中 P0-A（循环依赖）与 P0-B（web 安全）收益最高、应优先处理。

> 全部为只读分析，未修改任何文件。