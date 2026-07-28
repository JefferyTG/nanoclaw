# NanoClaw 项目文档

本目录是跨会话协作的稳定事实源。审计基线为 2026-07-26、提交 `0daefc4`。

| 文档 | 用途 | 何时阅读 |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 技术栈、目录、模块依赖、运行与数据流 | 接手项目或修改跨模块链路前 |
| [DECISIONS.md](DECISIONS.md) | 历史决策、约束、已解决事项和遗留问题 | 做设计选择或排查历史 Bug 前 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 任务拆解、会话分工、验证矩阵、完成标准 | 每个开发任务开始和交接时 |
| [TTS.md](TTS.md) | 网页自动朗读的范围、增量链路、故障边界与验收项 | 修改 TTS Provider 或网页朗读流程前 |
| [tasks/TEMPLATE.md](tasks/TEMPLATE.md) | 长任务的任务卡与交接模板 | 需要跨会话或多阶段协作时 |

根目录 [AGENTS.md](../AGENTS.md) 是所有会话自动适用的精简规则。`.workbuddy/` 是本机历史证据，不是可移植的唯一规范；其中仍有效的长期约定已经提炼到上述文档。

事实冲突时使用这一优先级：当前代码与配置契约 > 当前 Git 状态 > 本目录文档 > `.workbuddy/` 日志 > 旧方案草稿。
