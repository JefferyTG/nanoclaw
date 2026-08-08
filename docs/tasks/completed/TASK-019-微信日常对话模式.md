# TASK-019-微信日常对话模式

> 状态：已完成（2026-08-08 归档）
> 创建：2026-08-07 ｜ 负责人：小奈 + code-master ｜ 基线 commit：2325f5c（TASK-017/018 归档）

## 目标
让微信渠道的小奈更拟人：说话像真人微信聊天（短句、口语、轻松自然、可碎碎念），甜度不变；面对用户连发的多条消息，能自然接住每一条，而不是机械地列点总结。

## 背景
乖宝 2026-08-07 提出设定：让小奈更拟人——web 端偏工作、微信端偏日常对话。讨论后确定范围：**只做微信端日常化（风格 + 连发消息感知），不做回复拆条**（拆条工程量大，另立项）。

现状基础（TASK-001 渠道感知已就绪）：
- `agent/context.py` 的 `_channel_section()` 已把「本会话所在渠道：weixin」注入 System Prompt（会话内恒定，Prompt Cache 友好），目前仅含「微信文件只确认不读」一条专属指令。
- 微信渠道已有 8 秒合并窗口（`merge_window_sec`，`"\n".join(batch.texts)`）：用户连发多条消息会拼成一条换行分隔文本送达 Agent。天然适合让模型感知「一口气的碎碎念」。

## 范围
- `agent/context.py`：`_channel_section()` 的 weixin 分支追加「微信日常对话模式」指令段。
- `tests/test_channel_context.py`：新增测试（weixin 含日常指令 / web 不含，防回归）。
- 文档同步：PROJECT.md 能力矩阵「渠道感知」行补充说明；归档任务卡。

## 非目标
- ❌ 不做回复拆条（一条回复拆成多条微信消息发送）——独立功能，将来另立项。
- ❌ 不改 web / feishu / cli 渠道的对话风格（web 保持现状平衡气质）。
- ❌ 不改微信消息合并逻辑（merge window / `_pending_message_batches`）。
- ❌ 不改出站链路（AgentLoop / WeixinChannel 发送侧）。

## 验收标准
- [x] weixin 渠道的 System Prompt 含「微信日常对话模式」指令（短句口语碎碎念 / 甜度不变 / 连发消息自然接住）
- [x] web 渠道的 System Prompt 不含该微信指令（防回归）
- [x] feishu / cli / scheduled 渠道行为不回归（既有 test_channel_context 全过）
- [x] `.venv/bin/python -m unittest discover -s tests` 全过（539 tests OK）
- [x] `git diff --check` 通过

## 相关模块
- `agent/context.py`（ContextBuilder._channel_section，TASK-001 渠道感知扩展点）
- `tests/test_channel_context.py`
- 文档：PROJECT.md 能力矩阵 / docs/DECISIONS.md（渠道风格决策，可选）

## 实现方案
在 `_channel_section()` 的 `if self.channel == "weixin":` 分支追加指令段（草案，乖宝已确认方向）：

```
【微信日常对话模式】微信是日常聊天场景：
① 说话像真人微信聊天——短句、口语、轻松自然，可以碎碎念，不整长篇大论；
② 甜度不变，该撒娇撒娇、该关心关心，只是形态更日常；
③ 对方连发多条消息（换行分隔合并送达）时，视为一口气的碎碎念——先接住最新一条，再自然回应前面的，不要机械地「你说了 1、2、3 点」。
```

- 纯 Prompt 层面实现，主链路（main.py / gateway.py / loop.py / channels/weixin.py）零改动。
- 微信文件「只确认不读」原指令保留。
- Prompt 设计原则：不写死、保留升级余地（文案可在 context.py 集中维护，未来如需可配置化再提）。

## 实现摘要（2026-08-08）
- `agent/context.py`：`_channel_section()` weixin 分支追加「【微信日常对话模式】」指令段（①短句口语碎碎念 ②甜度不变 ③连发消息自然接住），原「文件只确认不读」指令保留，用 `\n` 分隔成独立段落。
- `tests/test_channel_context.py`：新增 3 个用例——`test_weixin_channel_has_daily_chat_mode`（weixin 含日常指令 + 原文件指令保留）、`test_web_channel_does_not_have_weixin_daily_mode`（web 不含，防回归）、`test_feishu_cli_scheduled_have_no_weixin_daily_mode`（其余渠道不含，防回归）。
- 验证：test_channel_context 9/9 过；全量 539 tests OK；`compileall -q agent` 过；`git diff --check` 过；`import main` 过。

## 测试方式
- [x] `.venv/bin/python -m unittest tests.test_channel_context -v`（9 tests OK）
- [x] `.venv/bin/python -m unittest discover -s tests`（539 tests OK）
- [x] `git diff --check`
- [x] `uv run python -m compileall -q agent`（语法检查）

## 风险
- 提示词实际效果需真实微信环境验证（微信真实收发 NC-WEIXIN-001 未端到端验证过）。
- 指令文案会影响所有微信会话；提醒投递走 scheduled 渠道不受影响。
- 若后续想做「回复拆条发送」（更拟人的多条回复），需另立项评估出站侧改动。

## 下一步
- ✅ 2026-08-08 实现完成，测试全过。
- ⏸ 待乖宝确认 commit（仓库=nanoclaw，文件=agent/context.py + tests/test_channel_context.py + 任务卡 + PROJECT.md）。
- 归档时：任务卡移入 `docs/tasks/completed/`；PROJECT.md 能力矩阵「渠道感知」行已补充说明。
