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
import signal
import sys
from datetime import timedelta

from config import load_config
from providers.openai_compat import OpenAICompatProvider
from voice.asr.openai_compat import OpenAICompatibleASRProvider
from voice.asr.service import AudioTranscriptionService
from voice.tts.edge import EdgeTTSProvider
from voice.tts.service import TextToSpeechService
from agent.tools.registry import ToolRegistry
from agent.tools.mcp import MCPClientManager
from agent.tools.filesystem import ReadFileTool, WriteFileTool, ListDirTool
from agent.tools.shell import ExecTool
from agent.tools.web_search import WebSearchTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.skills_tools import ListSkillsTool, LoadSkillTool, ReadSkillResourceTool
from agent.tools.spawn import SpawnSubagentTool
from agent.tools.reminders import (
    CancelReminderTool,
    CreateReminderTool,
    ListRemindersTool,
)
from agent.tools.agent_profiles import (
    CreateAgentPrivateTool,
    CreateAgentSkillTool,
    CreateAgentTool,
    ListAgentAssetsTool,
    ListAgentsTool,
    UpdateAgentPrivateTool,
    UpdateAgentSkillTool,
)
from agent.skills import SkillsLoader
from agent.profiles import AgentProfileLoader
from agent.scene_assets import SceneSkillAssets, SceneToolAssets
from agent.tool_factories import ToolFactoryRegistry
from agent.context import ContextBuilder
from agent.identity import IdentityBootstrapper
from agent.loop import AgentLoop
from session.manager import SessionManager
from agent.memory import MemoryConsolidation
from agent.daily import DailyMemory, summarize_messages_to_daily
from agent.search import MemorySearcher
from agent.tools.search import MemorySearchTool
from agent.tools.vision import AskImageTool
from agent.tools.imagegen import GenerateImageTool
from agent.imagestore import ImageStore

from bus.queue import MessageBus, OutboundMessage
from gateway import Gateway
from channels.cli import CLIChannel
from channels.feishu import FeishuChannel
from channels.weixin import WeixinChannel
from channels.web import WebChannel
from reminders.models import DeliveryResult
from reminders.repository import ReminderRepository
from reminders.scheduler import ReminderScheduler, SystemClock
from reminders.service import AsyncReminderRepository, ReminderService


# 配置文件路径（网页渠道配置页也写回同一文件）
CONFIG_PATH = "config.json"


def build_asr_service(config):
    """按启动期配置创建渠道无关的 ASR 服务；未启用或无效时返回 None。"""

    settings = config.asr_model if isinstance(config.asr_model, dict) else {}
    if not settings.get("enabled", False):
        return None
    if settings.get("provider", "openai_compatible") != "openai_compatible":
        print("[!] ASR 未启用：当前仅支持 provider=openai_compatible")
        return None

    api_key = str(settings.get("api_key") or "").strip()
    base_url = str(settings.get("base_url") or "").strip()
    model = str(settings.get("model") or "").strip()
    if not api_key or not base_url or not model:
        print("[!] ASR 未启用：请配置 asr_model 的 api_key/base_url/model（密钥可用 ASR_API_KEY）")
        return None

    try:
        provider = OpenAICompatibleASRProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_sec=float(settings.get("timeout_sec", 90)),
            max_retries=int(settings.get("max_retries", 1)),
        )
        service = AudioTranscriptionService(
            provider=provider,
            max_audio_bytes=int(settings.get("max_audio_bytes", 10 * 1024 * 1024)),
            max_duration_sec=float(settings.get("max_duration_sec", 120)),
            max_concurrency=int(settings.get("max_concurrency", 2)),
            ffmpeg_path=str(settings.get("ffmpeg_path") or "ffmpeg"),
            ffprobe_path=str(settings.get("ffprobe_path") or "ffprobe"),
            language=str(settings.get("language") or "").strip() or None,
            prompt=str(settings.get("prompt") or "").strip() or None,
        )
    except (TypeError, ValueError) as exc:
        print(f"[!] ASR 未启用：配置值无效（{exc}）")
        return None

    print(f"（网页语音识别：已启用·模型 {model}）")
    return service


def build_tts_service(config):
    """按启动期配置创建渠道无关的 TTS 服务；无效配置只禁用朗读。"""

    settings = config.tts_model if isinstance(config.tts_model, dict) else {}
    if not settings.get("enabled", True):
        return None
    if settings.get("provider", "edge_tts") != "edge_tts":
        print("[!] TTS 未启用：当前仅支持 provider=edge_tts")
        return None

    voice = str(settings.get("voice") or "").strip()
    rate = str(settings.get("rate") or "").strip()
    if not voice or not rate:
        print("[!] TTS 未启用：请配置 tts_model.voice/rate")
        return None

    try:
        provider = EdgeTTSProvider(
            voice=voice,
            rate=rate,
            connect_timeout_sec=int(settings.get("connect_timeout_sec", 10)),
            receive_timeout_sec=int(settings.get("receive_timeout_sec", 60)),
            max_audio_bytes=int(settings.get("max_audio_bytes", 16 * 1024 * 1024)),
        )
        service = TextToSpeechService(
            provider,
            max_text_chars=int(settings.get("max_text_chars", 4000)),
            max_audio_bytes=int(settings.get("max_audio_bytes", 16 * 1024 * 1024)),
            max_concurrency=int(settings.get("max_concurrency", 2)),
            timeout_sec=float(settings.get("timeout_sec", 60)),
        )
    except (TypeError, ValueError) as exc:
        print(f"[!] TTS 未启用：配置值无效（{exc}）")
        return None

    print(f"（网页文字朗读：服务已就绪·音色 {voice}·页面默认关闭）")
    return service


def build_reminder_service(config):
    """Create the dedicated reminder store and application service when enabled."""

    settings = config.reminders if isinstance(config.reminders, dict) else {}
    if not settings.get("enabled", True):
        return None, None
    database_path = os.fspath(settings.get("database_path") or "workspace/reminders.db")
    if not os.path.isabs(database_path):
        database_path = os.path.join(config.workspace, database_path)
    repository = ReminderRepository(database_path)
    return repository, ReminderService(repository)


def build_weixin_channel(config, bus, image_store):
    """Build the optional Weixin process adapter from startup-only settings."""

    settings = config.weixin if isinstance(config.weixin, dict) else {}
    if not settings.get("enabled", False):
        return None

    command = settings.get("bridge_command")
    if (
        not isinstance(command, (list, tuple))
        or not command
        or not all(isinstance(part, str) and part.strip() for part in command)
    ):
        raise ValueError("weixin.bridge_command must be a non-empty argv list")

    allowed = settings.get("allowed_user_ids", [])
    if not isinstance(allowed, (list, tuple)) or not all(
        isinstance(user_id, str) and user_id for user_id in allowed
    ):
        raise ValueError("weixin.allowed_user_ids must be a list of user IDs")

    state_dir = os.fspath(settings.get("state_dir") or "workspace/weixin")
    if not os.path.isabs(state_dir):
        state_dir = os.path.join(config.workspace, state_dir)
    state_dir = os.path.realpath(state_dir)
    runtime_root = os.path.realpath(os.path.join(config.workspace, "workspace"))
    if os.path.commonpath((runtime_root, state_dir)) != runtime_root:
        raise ValueError("weixin.state_dir must stay under <workspace>/workspace")

    return WeixinChannel(
        "weixin",
        bus,
        bridge_command=command,
        state_dir=state_dir,
        allowed_user_ids=allowed,
        image_store=image_store,
        image_merge_window_sec=float(
            settings.get("image_merge_window_sec", 10)
        ),
        request_timeout_sec=float(settings.get("request_timeout_sec", 30)),
        login_timeout_sec=float(settings.get("login_timeout_sec", 480)),
        inbound_ack_timeout_sec=float(
            settings.get("inbound_ack_timeout_sec", 30)
        ),
        stop_timeout_sec=float(settings.get("stop_timeout_sec", 10)),
        max_ipc_line_bytes=int(settings.get("max_ipc_line_bytes", 1024 * 1024)),
        max_inbound_image_bytes=int(
            settings.get("max_inbound_image_bytes", 20 * 1024 * 1024)
        ),
        max_outbound_image_bytes=int(
            settings.get("max_outbound_image_bytes", 20 * 1024 * 1024)
        ),
        auto_login=True,
    )


async def watch_channel_start_failures(tasks, channels, *, ignore_indices=()):
    """Surface background-style channel startup failures without treating a
    successful, short ``start()`` as application shutdown.

    CLI is excluded because its normal completion is already the explicit
    application-exit signal.  Web/Feishu/Weixin ``start()`` may return after
    installing their background listener; those successful returns are ignored.
    """

    monitored = {
        task: channels[index].name
        for index, task in enumerate(tasks)
        if index not in set(ignore_indices)
    }
    while monitored:
        done, _ = await asyncio.wait(
            monitored, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            channel_name = monitored.pop(task)
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                raise RuntimeError(
                    f"channel '{channel_name}' failed to start: {error}"
                ) from error
    await asyncio.Future()


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
    asr_service = build_asr_service(config)
    tts_service = build_tts_service(config)

    # 3) 技能加载器（扫描 <workspace>/skills 下的 SKILL.md，供摘要注入与技能工具共用）
    skills_dir = os.path.join(config.workspace, "skills")
    skills_loader = SkillsLoader(skills_dir)
    profile_loader = AgentProfileLoader(
        os.path.join(config.workspace, "workspace", "agents")
    )
    scene_skill_assets = SceneSkillAssets(config.workspace)
    scene_tool_assets = SceneToolAssets(config.workspace)
    tool_factories = ToolFactoryRegistry()

    # 4) 注册工具（全部以 workspace 为边界，防止越权访问）
    tools = ToolRegistry()
    tools.register(ReadFileTool(config.workspace))
    tools.register(WriteFileTool(config.workspace))
    tools.register(ListDirTool(config.workspace))
    tools.register(ExecTool(config.workspace))
    tools.register(WebSearchTool())
    tools.register(WebFetchTool())
    reminder_repository, reminder_service = build_reminder_service(config)
    if reminder_service is not None:
        tools.register(CreateReminderTool(reminder_service))
        tools.register(ListRemindersTool(reminder_service))
        tools.register(CancelReminderTool(reminder_service))
    # 技能工具：让模型能主动枚举与读取技能正文
    tools.register(ListSkillsTool(skills_loader))
    tools.register(LoadSkillTool(skills_loader))
    tools.register(ReadSkillResourceTool(skills_loader))
    # 场景 Agent 管理工具。创建时按当前完整 registry / Skill 清单校验白名单；
    # registry 是共享对象，后续注册的内置/MCP 工具也会在执行时可见。
    tools.register(
        CreateAgentTool(
            profile_loader=profile_loader,
            tools_registry=tools,
            skills_loader=skills_loader,
        )
    )
    tools.register(ListAgentsTool(profile_loader))
    tools.register(
        CreateAgentSkillTool(profile_loader, scene_skill_assets, skills_loader)
    )
    tools.register(
        UpdateAgentSkillTool(profile_loader, scene_skill_assets, skills_loader)
    )
    tools.register(
        CreateAgentPrivateTool(
            profile_loader, scene_tool_assets, tool_factories, tools_registry=tools
        )
    )
    tools.register(
        UpdateAgentPrivateTool(
            profile_loader, scene_tool_assets, tool_factories, tools_registry=tools
        )
    )
    tools.register(ListAgentAssetsTool(profile_loader, tool_factories))

    # 5) 生成技能摘要并注入 System Prompt
    skills_summary = skills_loader.build_skills_summary()
    if skills_summary:
        # 按摘要中以 "- " 开头的技能行统计数量
        skill_count = sum(
            1 for line in skills_summary.splitlines() if line.strip().startswith("- ")
        )
        print(f"已加载技能：{skill_count} 个")

    # 6) 上下文构建器（人设 + 时间 + 工作区 + 长期记忆 + 技能摘要）
    agents_summary = profile_loader.build_summary()
    context = ContextBuilder(
        config.workspace,
        config.identity_file,
        skills_summary=skills_summary,
        agents_summary=agents_summary,
        agents_summary_provider=profile_loader.build_summary,
    )
    identity_bootstrapper = IdentityBootstrapper(
        config.workspace, config.identity_file
    )

    # 7) 会话持久化管理器（跨进程保存对话历史，数据落在 workspace/ 下，不进项目根）
    WORKSPACE = config.workspace
    sessions_dir = os.path.join(WORKSPACE, "workspace", "sessions")
    session_manager = SessionManager(sessions_dir)

    # 7.2) 图片存储：与 sessions 同目录，按会话落盘到 <safe_key>_images/，
    #      现有 .jsonl 结构零改动；供 ask_image 工具与多模态直传使用。
    image_store = ImageStore(sessions_dir)

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

    # 视觉工具 ask_image：基础模型为纯文本时注册（无论 multimodal_model 是否配置；
    # 未配置时工具会返回"看不见图片"，由基础模型继续回答文字部分）。基础模型本身
    # 多模态时不注册，图片直接以多模态 content 透传基础模型。
    if not config.base_model_multimodal:
        tools.register(AskImageTool(image_store, config))

    # 生图工具 generate_image：始终注册（与 base_model_multimodal 无关）。
    # 未配置 image_gen_model 时仍注册，工具内部返回友好提示，由主模型用文字继续。
    tools.register(GenerateImageTool(image_store, config))

    # 子 Agent 工具最后注册，确保 Profile 创建/派遣时可见完整的内置工具集合。
    # MCP 工具稍后仍会注入同一个 registry，因此执行时同样可被校验和选用。
    def provider_factory(model=None):
        return OpenAICompatProvider(
            config.api_key, config.base_url, model or config.model
        )

    tools.register(
        SpawnSubagentTool(
            provider_factory=provider_factory,
            tools_registry=tools,
            skills_loader=skills_loader,
            profile_loader=profile_loader,
            scene_skill_assets=scene_skill_assets,
            scene_tool_assets=scene_tool_assets,
            tool_factories=tool_factories,
            workspace=config.workspace,
            config=config,
        )
    )

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
        "asr_service": asr_service,
        "tts_service": tts_service,
        "tools": tools,
        "context": context,
        "identity_bootstrapper": identity_bootstrapper,
        "session_manager": session_manager,
        "image_store": image_store,
        "memory": memory,
        "daily_memory": daily_memory,
        "searcher": searcher,
        "skills_summary": skills_summary,
        "profile_loader": profile_loader,
        "scene_skill_assets": scene_skill_assets,
        "scene_tool_assets": scene_tool_assets,
        "tool_factories": tool_factories,
        "agents_summary": agents_summary,
        "reminder_repository": reminder_repository,
        "reminder_service": reminder_service,
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
            cfg.workspace,
            cfg.identity_file,
            skills_summary=shared["skills_summary"],
            agents_summary=shared["profile_loader"].build_summary(),
            agents_summary_provider=shared["profile_loader"].build_summary,
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
            image_store=shared["image_store"],
            base_model_multimodal=cfg.base_model_multimodal,
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

    reminder_scheduler = None
    reminder_scheduler_task = None
    reminder_service = shared.get("reminder_service")
    reminder_repository = shared.get("reminder_repository")

    if reminder_service is not None and reminder_repository is not None:
        reminder_settings = shared["config"].reminders
        reminder_clock = SystemClock()
        async_repository = AsyncReminderRepository(
            reminder_repository,
            clock=reminder_clock.now,
            once_grace=timedelta(
                seconds=float(reminder_settings.get("once_grace_seconds", 3600))
            ),
        )

        async def clear_scheduled_session(session_key: str) -> None:
            agents_registry.pop(session_key, None)
            await asyncio.to_thread(shared["session_manager"].clear, session_key)
            await asyncio.to_thread(shared["image_store"].clear, session_key)

        async def scheduled_agent_runner(prompt: str, session_key: str) -> str:
            # A scheduled execution is deliberately isolated from daily chat and
            # from a previous crashed attempt. SQLite retains its durable output.
            await clear_scheduled_session(session_key)
            agent = factory(session_key)
            try:
                return await agent.run(prompt)
            finally:
                await clear_scheduled_session(session_key)

        async def cleanup_scheduled_task(task_id: int) -> None:
            execution_ids = await asyncio.to_thread(
                reminder_repository.list_execution_ids, task_id
            )
            for execution_id in execution_ids:
                await clear_scheduled_session(
                    f"scheduled:{task_id}:{execution_id}"
                )

        async def deliver_reminder(execution, output: str) -> DeliveryResult:
            target = await asyncio.to_thread(reminder_repository.get_active_target)
            if target is None or target.target_id != execution.target_id:
                result = DeliveryResult(
                    success=False,
                    retryable=True,
                    code="target_unbound",
                    message="飞书提醒目标已解绑；同一用户重新绑定后可继续发送。",
                )
                async_repository.remember_delivery_result(execution.id, result)
                return result
            delivery_future = asyncio.get_running_loop().create_future()
            await bus.publish_outbound(
                OutboundMessage(
                    channel="feishu",
                    chat_id=target.chat_id,
                    content=output,
                    delivery_future=delivery_future,
                )
            )
            result = await delivery_future
            async_repository.remember_delivery_result(execution.id, result)
            return result

        reminder_scheduler = ReminderScheduler(
            async_repository,
            scheduled_agent_runner,
            deliver_reminder,
            clock=reminder_clock,
            lease_duration=timedelta(
                seconds=float(reminder_settings.get("lease_seconds", 900))
            ),
            max_sleep=timedelta(
                seconds=float(reminder_settings.get("max_sleep_seconds", 3600))
            ),
            delivery_timeout=float(
                reminder_settings.get("delivery_timeout_sec", 30)
            ),
            max_delivery_attempts=int(
                reminder_settings.get("max_delivery_attempts", 3)
            ),
            max_agent_attempts=int(reminder_settings.get("max_agent_attempts", 3)),
        )
        reminder_service.attach_scheduler(reminder_scheduler)
        reminder_service.attach_cleanup(cleanup_scheduled_task)

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
        else:
            # Agent 尚未创建（如进程重启后直接 /clear）：直接清落盘的会话历史，
            # 否则 jsonl 会残留、下次创建 Agent 时旧历史又被读回来
            shared["session_manager"].clear(session_key)
        # 清除该会话落盘的图片目录，避免无限堆积。
        # 注意必须放在 agent 判断之外：无论 Agent 是否已创建都要清图，
        # 否则重启后直接 /clear 会漏掉图片目录。
        image_store = shared.get("image_store")
        if image_store is not None:
            image_store.clear(session_key)

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
            "feishu",
            bus,
            cfg.feishu_app_id,
            cfg.feishu_app_secret,
            image_store=shared["image_store"],
            image_merge_window_sec=cfg.feishu_image_merge_window_sec,
            bind_callback=(
                reminder_service.bind_feishu if reminder_service is not None else None
            ),
            unbind_callback=(
                reminder_service.unbind_feishu if reminder_service is not None else None
            ),
        )
        feishu_channel._clear_callback = clear_callback  # 复用同一清空回调
        channels.append(feishu_channel)
        print("（飞书渠道：已启用·常开）")
    else:
        print("（飞书渠道：未配置 App ID/Secret，未启用）")

    # 微信渠道：启用后由 Python 维护一个薄 Node Bridge 子进程。Bridge 独占
    # 登录凭据、cursor 和 context token；普通消息只携带稳定账号/用户 target。
    try:
        weixin_channel = build_weixin_channel(cfg, bus, shared["image_store"])
    except (TypeError, ValueError) as exc:
        print(f"[!] 微信渠道未启用：配置值无效（{exc}）")
        weixin_channel = None
    if weixin_channel is not None:
        channels.append(weixin_channel)
        if not weixin_channel.allowed_user_ids:
            print("[!] 微信渠道 allowlist 为空：将拒绝所有入站与出站用户")
        print("（微信渠道：已启用·Node Bridge·仅私聊）")
    else:
        print("（微信渠道：未启用）")

    # 网页渠道：配置了端口且 >0 时启用（同局域网内网页访问）
    if cfg.web_port and cfg.web_port > 0:
        web_channel = WebChannel(
            "web", bus, cfg.web_host, cfg.web_port, cfg, CONFIG_PATH,
            session_manager=shared["session_manager"],  # 侧边栏读写历史会话
            image_store=shared["image_store"],            # 图片上传落盘
            asr_service=shared["asr_service"],             # Web 录音即时转写；音频不进入 Bus
            tts_service=shared["tts_service"],             # Web 新回复按需朗读；不进入会话历史
        )
        web_channel._clear_callback = clear_callback  # 复用同一清空回调
        channels.append(web_channel)
        print(f"（网页渠道：已启用·监听 http://{cfg.web_host}:{cfg.web_port}）")
    else:
        print("（网页渠道：未配置 web_port 或未启用）")

    if not channels:
        print("错误：没有任何启用渠道（CLI 需终端，网页/飞书/微信需显式配置），退出。")
        return

    gateway = Gateway(
        bus,
        channels,
        factory,
        identity_bootstrapper=shared["identity_bootstrapper"],
    )

    # 并发启动各渠道的 start() + 入站消费 + 出站分发协程。
    # CLI 的 start() 是长循环（/exit 时返回）；飞书/网页的 start() 仅拉起
    # 后台守护线程并立即返回。CLI 退出即视为整体退出；无 CLI 时永久运行。
    start_tasks = [asyncio.create_task(ch.start()) for ch in channels]
    start_failure_task = asyncio.create_task(
        watch_channel_start_failures(
            start_tasks,
            channels,
            ignore_indices=(() if cli_task_index is None else (cli_task_index,)),
        )
    )
    inbound_task = asyncio.create_task(gateway._process_inbound())
    outbound_task = asyncio.create_task(gateway._dispatch_outbound())
    stream_task = asyncio.create_task(gateway._dispatch_stream())
    if reminder_scheduler is not None:
        reminder_scheduler_task = reminder_scheduler.start()

    # Linux 管理脚本使用 SIGTERM 停止进程；显式转成 asyncio 事件后，渠道、
    # Agent 与 MCP 都会走下方的统一清理流程，而不是被操作系统直接截断。
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    watched = {inbound_task, shutdown_waiter, start_failure_task}
    if reminder_scheduler_task is not None:
        watched.add(reminder_scheduler_task)
    if cli_task_index is not None:
        watched.add(start_tasks[cli_task_index])
    await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)

    if shutdown_event.is_set():
        print("\n收到停止信号，正在释放资源……")
    elif start_failure_task.done() and not start_failure_task.cancelled():
        try:
            start_failure_task.result()
        except Exception as exc:  # noqa: BLE001 - already normalized above
            print(f"[!] 渠道启动失败，正在停止实例：{exc}")
    for i, task in enumerate(start_tasks):
        if cli_task_index is None or i != cli_task_index or not task.done():
            task.cancel()
    shutdown_waiter.cancel()
    start_failure_task.cancel()

    if reminder_scheduler is not None:
        await reminder_scheduler.stop()

    inbound_task.cancel()     # 结束网关入站/出站循环
    outbound_task.cancel()
    stream_task.cancel()
    await asyncio.gather(
        *start_tasks,
        inbound_task,
        outbound_task,
        stream_task,
        shutdown_waiter,
        start_failure_task,
        *([reminder_scheduler_task] if reminder_scheduler_task is not None else []),
        return_exceptions=True,
    )
    for sig in installed_signals:
        loop.remove_signal_handler(sig)
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
