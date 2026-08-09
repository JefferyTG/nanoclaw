# TASK-024：voice 本地语音渠道骨架

## 任务卡

- 状态：待开始
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

- [ ] `voice:local:0` 会话能收发消息走通 Agent（CLI 模拟输入）
- [ ] `/new` 命令可用：`voice:local:0` → `/new` → `voice:local:1`（旧会话保留可 `/switch`）
- [ ] 渠道可开关（config 控制，默认关闭）
- [ ] 专项测试通过（unittest）
- [ ] 文档与配置同步（任务卡、PROJECT.md 能力矩阵）

### 必须执行的验证

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q channels
# 手动验证：启动 main.py，向 voice 渠道注入模拟消息 → 观察 Agent 回复
```

## 执行交接

- 状态：待开始
- 实际改动文件：无（待开工）
- 实现摘要：无
- 关键决策与假设：渠道默认关闭（config `voice.enabled=false`），避免影响现有渠道
- 验证命令与结果：未执行
- 未验证项：全部
- 风险与遗留问题：无真实音频输入源，链路用 CLI 模拟
- commit（仅在获授权时）：暂无
- 当前 `git status --short --branch`：main...origin/main（干净）
- 建议下一步：乖宝说「开始」后开工；可与 TASK-023 并行

## 负责人验收

- [ ] 检查 diff 与授权范围
- [ ] 独立复跑关键验证
- [ ] 检查秘密/个人数据/运行产物
- [ ] 检查文档与配置一致性
- [ ] 更新 `docs/DECISIONS.md` 中相关状态
- 验收结论：待验收
- 证据与备注：
