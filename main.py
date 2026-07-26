"""NanoClaw 入口文件。

通过消息总线（MessageBus）+ 渠道（Channel）+ 网关（Gateway）驱动一个
基于 ReAct 循环的本地 Agent：读取配置 → 创建模型 Provider、工具注册表、
上下文构建器 → 交给 Gateway 按会话调度 → 从 CLI / 飞书等渠道收发消息。

用法：
    export NANOCLAW_API_KEY="sk-xxx"   # 或用 config.json 配置
    uv run python main.py

交互命令（在 CLI 渠道内输入）：
    /exit   退出
    /clear  清空当前会话历史
    /tools  查看已注册工具
"""

import asyncio
import os
import sys

from config import load_config
from providers.openai_compat import OpenAICompatProvider
from agent.tools.registry import ToolRegistry
from agent.tools.mcp import MCPClientManager
from agent.tools.filesystem import ReadFileTool, WriteFileTool, ListDirTool
from agent.tools.shell import ExecTool
from agent.tools.web_search import WebSearchTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.skills_tools import ListSkillsTool, LoadSkillTool
from agent.tools.spawn import SpawnSubagentTool
from agent.skills import SkillsLoader
from agent.context import ContextBuilder
from agent.loop import AgentLoop
from session.manager import SessionManager
from agent.memory import MemoryConsolidation
from agent.daily import DailyMemory, summarize_messages_to_daily
from agent.search import MemorySearcher
from agent.tools.search import MemorySearchTool

from bus.queue import MessageBus
from gateway import Gateway
from channels.cli import CLIChannel
from channels.feishu import FeishuChannel
from channels.web import WebChannel


# 配置文件路径（网页渠道配置页也写回同一文件）
CONFIG_PATH = "config.json"


def build_shared() -> dict:
    """创建跨会话共享的组件，返回供 agent_factory 复用的配置字典。

    这些组件（Provider / 工具 / 上下文 / 会话管理器 / 压缩器）与具体
    会话无关，所有会话共用同一份，避免重复创建开销。
    """
    config = load_config(CONFIG_PATH)

    # 1) 缺少 API Key 直接退出，避免后续调用必然失败
    if not config.api_key:
        print("错误：未配置 API Key。")
        print("请二选一：")
        print("  - 在 config.json 中填入 api_key；或")
        print("  - 设置环境变量：export NANOCLAW_API_KEY='你的key'")
        sys.exit(1)

    # 2) 创建模型 Provider（OpenAI 兼容，默认硅基流动）
    provider = OpenAICompatProvider(config.api_key, config.base_url, config.model)

    # 3) 技能加载器（扫描 <workspace>/skills 下的 SKILL.md，供摘要注入与技能工具共用）
    skills_dir = os.path.join(config.workspace, "skills")
    skills_loader = SkillsLoader(skills_dir)

    # 4) 注册工具（全部以 workspace 为边界，防止越权访问）
    tools = ToolRegistry()
    tools.register(ReadFileTool(config.workspace))
    tools.register(WriteFileTool(config.workspace))
    tools.register(ListDirTool(config.workspace))
    tools.register(ExecTool(config.workspace))
    tools.register(WebSearchTool())
    tools.register(WebFetchTool())
    # 技能工具：让模型能主动枚举与读取技能正文
    tools.register(ListSkillsTool(skills_loader))
    tools.register(LoadSkillTool(skills_loader))
    # 子 Agent 衍生工具：允许主 Agent 把复杂任务派给独立子 Agent 处理。
    # provider_factory 闭包接收可选 model，省略时回退到 config.model；
    # 子 Agent 的默认模型可由 config.subagent_model 另行指定。
    def provider_factory(model=None):
        return OpenAICompatProvider(
            config.api_key, config.base_url, model or config.model
        )
    tools.register(
        SpawnSubagentTool(
            provider_factory=provider_factory,
            tools_registry=tools,
            workspace=config.workspace,
            config=config,
        )
    )

    # 5) 生成技能摘要并注入 System Prompt
    skills_summary = skills_loader.build_skills_summary()
    if skills_summary:
        # 按摘要中以 "- " 开头的技能行统计数量
        skill_count = sum(
            1 for line in skills_summary.splitlines() if line.strip().startswith("- ")
        )
        print(f"已加载技能：{skill_count} 个")

    # 6) 上下文构建器（人设 + 时间 + 工作区 + 长期记忆 + 技能摘要）
    context = ContextBuilder(
        config.workspace, config.identity_file, skills_summary=skills_summary
    )

    # 7) 会话持久化管理器（跨进程保存对话历史，数据落在 workspace/ 下，不进项目根）
    WORKSPACE = config.workspace
    sessions_dir = os.path.join(WORKSPACE, "workspace", "sessions")
    session_manager = SessionManager(sessions_dir)

    # 7.5) 记忆检索：SQLite + LIKE 索引 USER/MEMORY/daily/sessions，
    #      启动时全量重建。注册 memory_search 工具（唯一新增工具——检索是
    #      read_file 做不到的真新能力，符合大道至简的例外）。
    searcher = MemorySearcher(
        os.path.join(WORKSPACE, "workspace", "memory"),
        session_manager=session_manager,
    )
    indexed = searcher.rebuild_all()
    print(f"已索引记忆与会话文档：{indexed} 条")
    tools.register(MemorySearchTool(searcher))

    # 8) 每日记忆：按天把重要事件追加到 workspace/memory/daily/YYYY-MM-DD.md
    #    供 /clear 与压缩前两个触发点写入；不暴露为工具。
    daily_memory = DailyMemory(os.path.join(WORKSPACE, "workspace", "memory"))

    # 9) 会话压缩器（上下文超预算时把旧消息压成摘要，落到 workspace/memory/HISTORY.md）
    #    token_budget 按 192k 估算 token 计；与 sessions 同级的 workspace/memory 目录。
    #    压缩前先把旧消息里的重要事件落 daily，避免关键事实随压缩丢失。
    memory = MemoryConsolidation(
        provider, os.path.join(WORKSPACE, "workspace"),
        token_budget=192_000, daily_memory=daily_memory,
    )

    return {
        "config": config,
        "provider": provider,
        "tools": tools,
        "context": context,
        "session_manager": session_manager,
        "memory": memory,
        "daily_memory": daily_memory,
        "searcher": searcher,
        "skills_summary": skills_summary,
    }


def make_agent_factory(shared: dict, registry: dict) -> callable:
    """构造 agent_factory：按 session_key 惰性创建并缓存 AgentLoop。

    Gateway 在一次会话的首条消息时调用本工厂创建 Agent，并缓存到
    ``registry``（供 /clear 回调查找当前会话的 Agent）。同一 session_key
    后续消息直接复用缓存实例。

    每次新建会话都按「当前」配置重建 Provider 与 ContextBuilder，使网页配置页
    改动的 model / identity / api_key 等对**新会话**即时生效（已在进行的会话
    保持原状，符合预期）。
    """

    def factory(session_key: str) -> AgentLoop:
        cfg = shared["config"]
        provider = OpenAICompatProvider(cfg.api_key, cfg.base_url, cfg.model)
        context = ContextBuilder(
            cfg.workspace, cfg.identity_file, skills_summary=shared["skills_summary"]
        )
        agent = AgentLoop(
            provider,
            shared["tools"],
            context,
            shared["session_manager"],
            session_key=session_key,
            model=cfg.model,
            max_iterations=cfg.max_iterations,
            memory=shared["memory"],
            turn_timeout=cfg.turn_timeout_sec,
        )
        registry[session_key] = agent
        return agent

    return factory


async def amain() -> None:
    """装配总线、渠道与网关，并驱动运行。"""
    shared = build_shared()
    tools = shared["tools"]

    print("已注册工具：", ", ".join(tools.list_tools()))

    # MCP 接入：按配置启动外部 MCP Server，把它们的工具注入同一注册表。
    # connect_all 是「尽力而为」的——连不上的 Server 会被跳过并打印告警，
    # 不会影响内置工具与已连上的 Server。mcp_manager 在退出时统一 shutdown。
    mcp_manager = MCPClientManager(shared["config"].mcp_servers)
    try:
        await mcp_manager.connect_all()
        for mt in mcp_manager.get_tools():
            tools.register(mt)
        if mcp_manager.get_tools():
            print("已加载 MCP 工具：", ", ".join(t.name for t in mcp_manager.get_tools()))
    except Exception as exc:  # noqa: BLE001 - MCP 连接失败不应阻断整体启动
        print(f"[!] MCP 初始化异常，已跳过所有 MCP 工具：{exc}")
    shared["mcp_manager"] = mcp_manager

    # 消息总线：渠道与 Gateway 之间的运行时解耦层
    bus = MessageBus()

    # CLI 渠道：从终端读写；工具列表与清空回调由下方注入
    cli_channel = CLIChannel(bus)
    cli_channel.tool_names = tools.list_tools()

    # 会话缓存：factory 与 /clear 回调共享。CLI 的 session_key 由
    # Gateway 推导为 f"{channel}:{sender_id}"；多会话下 sender_id 形如
    # "local{n}"，对应 session_key "cli:local{n}"。
    agents_registry: dict = {}
    factory = make_agent_factory(shared, agents_registry)

    def clear_callback(session_key: str) -> None:
        """/clear 命令回调：按完整 session_key 清空对应会话历史。

        同时供 CLI（传入 ``cli:local{n}``）与飞书（传入
        ``feishu:<chat_id>:<n>``）复用，避免重复定义。

        清空前先拷贝历史，启动后台任务把重要事件总结写入当天 daily
        （不阻塞 clear，daily 是 nice-to-have，失败忽略）。
        """
        agent = agents_registry.get(session_key)
        if agent is not None:
            # 先拷贝历史供后台总结（clear_history 会清空 _session_history）
            history_snapshot = list(agent._session_history)
            agent.clear_history()
            # 后台总结写 daily；无历史或未启用 daily 时跳过
            daily = shared.get("daily_memory")
            if history_snapshot and daily is not None:
                try:
                    asyncio.create_task(summarize_messages_to_daily(
                        shared["provider"], daily, history_snapshot,
                        category="会话总结",
                    ))
                except Exception:  # noqa: BLE001 - daily 失败不影响 clear
                    pass

    cli_channel._clear_callback = clear_callback

    # 组装所有启用的渠道。多实例由「不同文件夹 + 不同配置/端口」实现，
    # 本函数只负责按当前配置启用对应渠道。
    channels: list = []
    cfg = shared["config"]

    # CLI 仅在交互终端下启用；无终端（如纯网页部署）则跳过，由网页渠道独立运行
    cli_task_index = None
    if sys.stdin.isatty():
        cli_channel.tool_names = tools.list_tools()
        cli_channel._clear_callback = clear_callback
        channels.append(cli_channel)
        cli_task_index = len(channels) - 1

    # 飞书渠道：配置了凭证时启用
    if cfg.feishu_app_id and cfg.feishu_app_secret:
        feishu_channel = FeishuChannel(
            "feishu", bus, cfg.feishu_app_id, cfg.feishu_app_secret
        )
        feishu_channel._clear_callback = clear_callback  # 复用同一清空回调
        channels.append(feishu_channel)
        print("（飞书渠道：已启用·常开）")
    else:
        print("（飞书渠道：未配置 App ID/Secret，未启用）")

    # 网页渠道：配置了端口且 >0 时启用（同局域网内网页访问）
    if cfg.web_port and cfg.web_port > 0:
        web_channel = WebChannel(
            "web", bus, cfg.web_host, cfg.web_port, cfg, CONFIG_PATH,
            session_manager=shared["session_manager"],  # 侧边栏读写历史会话
        )
        web_channel._clear_callback = clear_callback  # 复用同一清空回调
        channels.append(web_channel)
        print(f"（网页渠道：已启用·监听 http://{cfg.web_host}:{cfg.web_port}）")
    else:
        print("（网页渠道：未配置 web_port 或未启用）")

    if not channels:
        print("错误：没有任何启用渠道（CLI 需终端，网页需 web_port>0，飞书需凭证），退出。")
        return

    gateway = Gateway(bus, channels, factory)

    # 并发启动各渠道的 start() + 入站消费 + 出站分发协程。
    # CLI 的 start() 是长循环（/exit 时返回）；飞书/网页的 start() 仅拉起
    # 后台守护线程并立即返回。CLI 退出即视为整体退出；无 CLI 时永久运行。
    start_tasks = [asyncio.create_task(ch.start()) for ch in channels]
    inbound_task = asyncio.create_task(gateway._process_inbound())
    outbound_task = asyncio.create_task(gateway._dispatch_outbound())
    stream_task = asyncio.create_task(gateway._dispatch_stream())

    if cli_task_index is not None:
        await start_tasks[cli_task_index]   # 阻塞至终端 /exit
        for i, t in enumerate(start_tasks):
            if i != cli_task_index:
                t.cancel()
    else:
        # 无 CLI（纯网页/飞书部署）：永久运行，直到 Ctrl-C
        await inbound_task

    inbound_task.cancel()     # 结束网关入站/出站循环
    outbound_task.cancel()
    stream_task.cancel()
    await asyncio.gather(
        *start_tasks, inbound_task, outbound_task, stream_task, return_exceptions=True
    )
    await gateway.shutdown()

    # 关闭所有 MCP Server 连接，回收子进程
    mcp_manager = shared.get("mcp_manager")
    if mcp_manager is not None:
        await mcp_manager.shutdown()


def main() -> None:
    """程序入口。"""
    print("=" * 48)
    print("   NanoClaw —— 本地 ReAct Agent（Gateway 驱动）")
    print("=" * 48)

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n再见。")


if __name__ == "__main__":
    main()
