# NanoClaw 项目文档

本目录是跨会话协作的稳定事实源。审计基线：2026-08-04，提交 `0cd50de`。

| 文档 | 用途 | 何时阅读 |
|---|---|---|
| [../PROJECT.md](../PROJECT.md) | 项目总览入口：定位、能力矩阵、模块、命令速查 | 任何新会话的第一份文档 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 技术栈、目录、模块依赖、运行与数据流 | 接手项目或修改跨模块链路前 |
| [DECISIONS.md](DECISIONS.md) | 历史决策、约束、已解决事项和遗留问题（NC-*） | 做设计选择或排查历史 Bug 前 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 任务拆解、会话分工、验证矩阵、完成标准 | 每个开发任务开始和交接时 |
| [TTS.md](TTS.md) | 网页自动朗读的范围、增量链路、故障边界与验收项 | 修改 TTS Provider 或网页朗读流程前 |
| [ASR.md](ASR.md) | 网页语音输入的链路、Provider 抽象与故障边界 | 修改 ASR 相关代码前 |
| [VOICE_WAKE_KWS.md](VOICE_WAKE_KWS.md) | 语音唤醒/关键词检测的调研与边界 | 涉及唤醒功能前 |
| [SCENE_AGENTS.md](SCENE_AGENTS.md) | 场景 Agent 的完整契约（Profile/私有 Skill/受控工具） | 修改场景 Agent 相关代码前 |
| [tasks/TEMPLATE.md](tasks/TEMPLATE.md) | 任务卡模板 | 创建新任务时 |
| [tasks/active/](tasks/active/) | 进行中的任务卡（TASK-xxx） | 恢复/继续任务时 |
| [tasks/completed/](tasks/completed/) | 已完成并归档的任务卡 | 查阅历史任务时 |
| [decisions/](decisions/) | 单条架构/技术决策记录（ADR 风格，可选） | 记录重大决策时；总表仍在 DECISIONS.md |

根目录 [AGENTS.md](../AGENTS.md) 是所有会话自动适用的精简规则。事实冲突时使用这一优先级：当前代码与配置契约 > 当前 Git 状态 > 本目录文档 > 旧方案草稿。
