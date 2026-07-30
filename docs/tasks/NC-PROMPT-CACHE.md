# NC-PROMPT-CACHE：Prompt Cache 命中率优化

## 状态与边界

- 状态：实现与离线验收完成，等待最终 Git 集成交付。
- 基线：`95c187a feat: 支持提醒目标显式换绑`。
- 目标：稳定请求前缀、移除 System 动态时间、统一历史、冻结工具 Schema、增加隐私安全 usage 观测。
- 非目标：不承诺供应商缓存工具或图片；不调用真实付费模型，不连接真实微信/飞书，不 push 或部署。

## 最终设计

1. ContextBuilder 在会话创建时快照 identity、USER、MEMORY 与场景 Agent 摘要；新会话或 `/clear` 是显式刷新边界。Skill 摘要保持启动期快照，修改后需重启。
2. System Prompt 只含稳定规则；时间相关问题使用 `get_current_time`，默认读取实例 `timezone`，可选 IANA 时区由 `zoneinfo` 校验。
3. ToolRegistry 按名称生成确定性 Schema；MCP 尽力连接结束后冻结并计算 hash，成功集合变化构成新边界。
4. 跨轮、落盘和重启历史使用 canonical 结构。assistant 顶层 reasoning 在工具循环中原样保留，`tool_calls` 内只允许标准字段。
5. 每次调用与每个用户回合记录 input/cached/uncached tokens、加权 ratio、System/工具 hash、历史消息数、phase 和工具迭代；daily/历史摘要调用也计入，不记录任何 prompt 内容或参数。
6. MemoryConsolidation 估算多模态 content 与工具 Schema，记录完整工具交换上的稳定压缩边界；摘要失败保留原历史。图片仍保留原字节供主模型理解，但文本摘要只接收省略占位，缓存能力留在供应商边界。

## 验收证据

- fake clock 覆盖默认时区、DST、非法 IANA timezone。
- exact-prefix 覆盖普通相邻回合、工具调用、顶层 reasoning、进程重启和多模态历史。
- fake SDK 覆盖非流式 usage、流式尾 usage、`include_usage`、不支持参数时降级、未知/缺失字段 unavailable。
- 工具注册逆序得到同一定义/hash；MCP 成功集合变化得到不同边界；冻结后拒绝热注册。
- 回合命中率使用 token 总和加权，缺失 cached tokens 不记作 0 命中。
- `uv run python -m unittest discover -s tests`：198 tests passed。
- `uv run python -m compileall -q agent bus channels providers session reminders voice config.py main.py tests`：通过。
- `uv run python -c "import main"`：通过。
- Web UI 内联 JavaScript `node --check -`、`config.example.json` 解析与 `git diff --check`：通过。

## 已知供应商边界

- 冷启动通常没有可复用前缀。
- 部分兼容服务不返回 cached token 明细；此时只报告已知 input，ratio 为 unavailable。
- 不支持 `stream_options.include_usage` 的服务会继续流式响应，但没有缓存明细。
- 工具 Schema 和图片是否参与缓存键由供应商决定。
- 图片文件缺失/变化、上下文显式刷新、MCP 成功集合变化、MemoryConsolidation 摘要替换都会产生合理的缓存断点。
