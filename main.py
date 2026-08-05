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
    /context  查看当前会话上下文占用
    /tools  查看已注册工具
"""

import asyncio
import json
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
from agent.tools.current_time import CurrentTimeTool
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
from agent.memory import ContextCompactor
from agent.memory_sync import is_patch_message, is_snapshot_message
from agent.daily import (
    DailyMemory,
    dream_consolidate,
    summarize_messages_to_daily,
)
from agent.search import MemorySearcher
from agent.tools.search import MemorySearchTool
from agent.tools.vision import AskImageTool
from agent.tools.imagegen import GenerateImageTool
from agent.filestore import FileStore
from agent.imagestore import ImageStore
from agent.tools.video import CreateVideoTool, QueryVideoTool
from agent.videostore import VideoStore

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


async def publish_reminder_delivery(
    execution,
    output: str,
    *,
    repository,
    service: ReminderService,
    async_repository: AsyncReminderRepository,
    bus: MessageBus,
) -> DeliveryResult:
    """Resolve one persisted target and await its channel acknowledgement."""

    target = await asyncio.to_thread(
        repository.get_target_by_public_id,
        execution.target_id,
        active_only=True,
    )
    if target is None:
        result = DeliveryResult(
            success=False,
            retryable=False,
            code="target_unbound",
            message="提醒目标已暂停；同一渠道和用户重新绑定后可继续发送。",
        )
        async_repository.remember_delivery_result(execution.id, result)
        return result

    delivery_future = asyncio.get_running_loop().create_future()
    await bus.publish_outbound(
        OutboundMessage(
            channel=target.channel,
            chat_id=target.recipient_id,
            content=output,
            delivery_future=delivery_future,
            correlation_id=f"reminder:{execution.id}",
        )
    )
    result = await delivery_future
    if (
        target.channel == "weixin"
        and result.code in {"context_missing", "session_expired"}
    ):
        await service.suspend_target_id(
            target.target_id,
            expected_binding_revision=target.binding_revision,
        )
    async_repository.remember_delivery_result(execution.id, result)
    return result


def build_weixin_channel(
    config,
    bus,
    image_store,
    file_store=None,
    *,
    bind_callback=None,
    unbind_callback=None,
    suspend_callback=None,
    context_callback=None,
):
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
        file_store=file_store,
        bind_callback=bind_callback,
        unbind_callback=unbind_callback,
        suspend_callback=suspend_callback,
        context_callback=context_callback,
        # 默认值与 config.py / WeixinChannel 构造器一致（8.0 / 10），且不在组合根
        # 强转：非法值（如 "abc"）由构造器的 try/except 兜底，而不是在此抛 ValueError。
        image_merge_window_sec=settings.get("image_merge_window_sec", 8.0),
        merge_max_messages=settings.get("merge_max_messages", 10),
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


# ===== 每日做梦整理（TASK-011 第二阶段：定时调度 + 启动补做）=====
# 与 reminders/scheduler.py 的 ReminderScheduler 同理：独立 asyncio 后台
# task、注入时钟、动态等待；本模块不重构 reminders，二者互不影响。
DEFAULT_DREAM_TIME = "02:00"


def _as_aware(now: datetime) -> datetime:
    """把时钟返回的时刻规范化为 aware（naive 按 UTC 解释，与 CurrentTimeTool 一致）。"""
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _parse_dream_time(value) -> tuple:
    """解析 ``"HH:MM"`` → (hour, minute)；非法值回退默认 02:00（与 config 同款容错）。"""
    try:
        hour, minute = str(value).strip().split(":", 1)
        hour, minute = int(hour), int(minute)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return 2, 0


def _next_dream_run(now: datetime, dream_time: str, timezone: str) -> datetime:
    """给定当前时刻，返回下一次做梦时刻（dream_time HH:MM，timezone 时区，严格晚于 now）。"""
    hour, minute = _parse_dream_time(dream_time)
    local_now = _as_aware(now).astimezone(ZoneInfo(timezone))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def _today_date(timezone: str) -> datetime.date:
    """实例时区下的「今天」日期（与 daily 按本地日期命名一致）。"""
    return datetime.now(ZoneInfo(timezone)).date()


async def _dream_default_wait(event: asyncio.Event, timeout: float) -> None:
    """默认等待：睡到 timeout（秒）或被事件唤醒（stop/wake）。"""
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        pass


class DreamScheduler:
    """每日定时做梦整理调度器（TASK-011）。

    仿照 ReminderScheduler 的「动态等待 + 可注入时钟」循环：独立 asyncio
    后台 task，每个本地日到 ``dream_time``（HH:MM，实例时区）到点执行一次
    ``consolidate_today()``（整理当天内容）；进程晚启动（已过到点）则启动后
    立即补跑当天一次。``consolidate_today`` 内部失败静默（不抛异常），故调度
    器不会阻塞聊天或启动。

    可测性：注入 ``clock``（``now() -> datetime``）与 ``wait``（event, timeout），
    测试用假时钟推进 + 假 wait 直接跳到到点时刻，无需真实 sleep。
    """

    def __init__(
        self,
        consolidate_today,
        *,
        dream_time: str = DEFAULT_DREAM_TIME,
        timezone: str = "Asia/Shanghai",
        clock=None,
        wait=None,
    ):
        self.consolidate_today = consolidate_today
        self.dream_time = dream_time
        self.timezone = timezone
        self.clock = clock or SystemClock()
        self._wait = wait or _dream_default_wait
        self._wake_event = asyncio.Event()
        self._stopped = False
        self._task = None

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stopped = False
            self._wake_event.clear()  # 清除上次 stop 遗留的唤醒信号
            self._task = asyncio.create_task(self.run(), name="dream-scheduler")
        return self._task

    async def stop(self) -> None:
        self._stopped = True
        self._wake_event.set()
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        """调度主循环：每个本地日到点最多执行一次；晚启动立即补跑当天。

        循环结构仿照 ReminderScheduler：注入时钟 + 可中断的 wait，便于单测
        用假时钟推进，无需真实 sleep。
        """
        last_date = None
        while not self._stopped:
            now = _as_aware(self.clock.now())
            local_today = now.astimezone(ZoneInfo(self.timezone)).date()
            if last_date != local_today:
                # 今天还没执行过：到点时刻在今天则睡到到点（已到则 delay=0）；
                # 到点已过（进程晚启动）则立即补跑今天。
                next_run = _next_dream_run(now, self.dream_time, self.timezone)
                if next_run.date() == local_today:
                    delay = max(0.0, (next_run - now).total_seconds())
                    self._wake_event.clear()
                    await self._wait(self._wake_event, delay)
                    if self._stopped:
                        return
                await self._safe_consolidate()
                last_date = local_today
            else:
                # 今天已执行过：睡到下一次（明天）到点
                next_run = _next_dream_run(now, self.dream_time, self.timezone)
                delay = max(0.0, (next_run - now).total_seconds())
                self._wake_event.clear()
                await self._wait(self._wake_event, delay)
                if self._stopped:
                    return

    async def _safe_consolidate(self) -> None:
        """执行一次整理；任何异常（含整理函数自身未吞的）都不让调度循环退出。"""
        try:
            await self.consolidate_today()
        except Exception:  # noqa: BLE001 - 做梦失败绝不影响调度循环
            pass


class DreamState:
    """维护做梦状态文件 ``<memory_dir>/dream_state.json``。

    记录 ``{"last_dream_date": "YYYY-MM-DD"}``：最近一次完成做梦整理的日期。
    - 启动补做：``last_dream_date < 昨天`` → 补整理前一天并更新本文件；
    - 每日定时到点：整理当天后同步更新（避免下次启动重复补做昨天）。
    文件读写均为尽力而为：缺失/损坏按「无记录」处理，写失败静默忽略
    （运行时产物，绝不阻断启动或聊天）。
    """

    _FILENAME = "dream_state.json"

    def __init__(self, memory_dir: str):
        self.path = os.path.join(memory_dir, self._FILENAME)

    def read_last_dream_date(self) -> str | None:
        """读取 last_dream_date；无文件/损坏/非法返回 None。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("last_dream_date")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    def write_last_dream_date(self, date_str: str) -> None:
        """记录 last_dream_date（**只前进不后退**；失败静默，不阻断调用方）。

        单调前进：若已有更晚的整理记录（如定时到点已写今天、启动补做随后写
        昨天），则跳过写入——避免并发路径把状态从「今天」回退到「昨天」。
        """
        try:
            current = self.read_last_dream_date()
            if current is not None and current >= date_str:
                return
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {"last_dream_date": date_str}, f, ensure_ascii=False, indent=2
                )
        except OSError:
            pass


def should_catch_up(last_dream_date: str | None, today: datetime.date) -> str | None:
    """启动补做目标：昨天未整理则返回昨天（YYYY-MM-DD），否则返回 None。

    - 无状态文件（首次启动）视为「昨天未整理」，补做一次昨天；
    - ``last_dream_date < 昨天`` 时只补最近 1 天（昨天），超期不回溯；
    - ``last_dream_date`` 已覆盖昨天（== 或晚于）则无需补做。
    """
    yesterday = today - timedelta(days=1)
    yesterday_str = yesterday.isoformat()
    if last_dream_date is None or last_dream_date < yesterday_str:
        return yesterday_str
    return None


def collect_messages_for_date(session_manager, date_str: str) -> list:
    """收集指定日期（YYYY-MM-DD）各会话的关键消息，供做梦整理。

    - 枚举所有会话 JSONL，取 ``timestamp`` 命中该日期的消息（保留原始消息，
      供模型提取事件）；
    - 过滤掉系统内部的记忆补丁/快照消息（``<memory_patch>/<memory_snapshot>``，
      非用户事件，避免污染整理输入）；
    - 单会话读取失败静默跳过（不影响其他会话，也不阻断启动）。
    """
    messages: list = []
    for stem in session_manager.list_sessions():
        key = stem.replace("_", ":")
        try:
            records = session_manager.get_session_messages(key)
        except Exception:  # noqa: BLE001 - 单会话失败静默跳过
            continue
        for msg in records:
            timestamp = msg.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp.startswith(date_str):
                continue
            if is_patch_message(msg) or is_snapshot_message(msg):
                continue
            messages.append(msg)
    return messages


async def run_dream_for_date(
    provider, daily, session_manager, dream_state, date_str: str
) -> None:
    """对指定日期执行一次做梦整理（失败静默，供启动补做与定时调度共用）。

    - 数据源：当天各会话关键消息（``collect_messages_for_date``）+ 当天 daily
      已有内容（``dream_consolidate`` 内部读取并让模型去重）；
    - 完成后更新 ``dream_state``（last_dream_date = date_str）；模型调用失败
      时不更新，保证下次启动补做可重试该日期；
    - 任何异常静默返回，不阻塞聊天或启动。
    """
    try:
        messages = collect_messages_for_date(session_manager, date_str)
        done = await dream_consolidate(provider, daily, date_str, messages)
    except Exception:  # noqa: BLE001 - 做梦失败静默
        return
    if done:
        try:
            dream_state.write_last_dream_date(date_str)
        except Exception:  # noqa: BLE001 - 状态写失败不阻断
            pass


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
    # 动态墙钟不再进入 System Prompt；只有相关问题才通过此工具按实例时区查询。
    tools.register(CurrentTimeTool(config.timezone))
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

    # 6) 上下文构建器（稳定规则 + 会话级人设/记忆/技能/Profile 快照；无墙钟）
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

    # 7.2.1) 文件存储：按月归档到 <data_root>/files/YYYY-MM/（如
    #       workspace/files/2026-08/），文件是长期资产，/clear 不删除。
    #       ref_root 取项目根（config.workspace，与 ReadFileTool 的根一致），
    #       使 ref.path = workspace/files/YYYY-MM/name，Agent 可直接 read_file。
    file_store = FileStore(
        os.path.join(WORKSPACE, "workspace", "files"),
        ref_root=config.workspace,
    )

    # 7.3) 视频存储：与 sessions 同目录，按会话落盘到 <safe_key>_videos/，
    #      供 create_video / query_video 下载保存生成的视频（文件可能几十 MB）。
    video_store = VideoStore(sessions_dir)

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

    # 视频生成工具 create_video / query_video：始终注册（异步任务式——创建任务
    # 立即返回 video_id、绝不轮询；稍后由 query_video 查询并下载结果）。未配置
    # video_gen_model 时仍注册，工具内部返回友好提示，由主模型用文字继续。
    tools.register(CreateVideoTool(video_store, config))
    tools.register(QueryVideoTool(video_store, config))

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
    #    现仅由 /clear 触发写入（TASK-006 起压缩不再写 daily）；不暴露为工具。
    daily_memory = DailyMemory(os.path.join(WORKSPACE, "workspace", "memory"))

    # 会话压缩器不再是共享单例（TASK-006）：改为在 make_agent_factory() 的
    # factory(session_key) 内为每个会话创建独立 ContextCompactor，随 AgentLoop
    # 一并注入；压缩结果显式返回，无跨会话共享可变状态。

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
        "file_store": file_store,
        "video_store": video_store,
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
        # session_key 由 Gateway 构造为 f"{channel}:{sender_id}"（渠道+用户）。
        # 只用 split(":", 1) 切第一刀，防止 sender_id 本身含冒号（如微信
        # target 可逆编码）导致裂解；解析结果注入 ContextBuilder 作为
        # 会话级「当前渠道」快照（渠道在会话内恒定，System Prompt 稳定）。
        channel, sender_id = session_key.split(":", 1)
        context = ContextBuilder(
            cfg.workspace,
            cfg.identity_file,
            skills_summary=shared["skills_summary"],
            agents_summary=shared["profile_loader"].build_summary(),
            agents_summary_provider=shared["profile_loader"].build_summary,
            channel=channel,
            sender_id=sender_id,
        )
        # 每会话独立压缩器（TASK-006）：压缩状态/结果属会话私有，绝不跨会话共享；
        # 允许共享 config / tools / session_manager，但 AgentLoop、compactor、
        # token 估算状态与本轮压缩结果都必须是本会话自己的。
        compactor = ContextCompactor(
            provider,
            os.path.join(cfg.workspace, "workspace"),
            token_budget=cfg.context_budget_tokens,
        )
        agent = AgentLoop(
            provider,
            shared["tools"],
            context,
            shared["session_manager"],
            session_key=session_key,
            model=cfg.model,
            max_iterations=cfg.max_iterations,
            compactor=compactor,
            turn_timeout=cfg.turn_timeout_sec,
            image_store=shared["image_store"],
            base_model_multimodal=cfg.base_model_multimodal,
        )
        registry[session_key] = agent
        return agent

    return factory


def _fmt_compact(value) -> str:
    """把 token 数压缩成易读文本（>=1024 用二进制 k 单位，512k=524288）。

    与任务卡示例一致：预算 524288 tokens 显示为 ``512k``。
    """
    if value is None:
        return "0"
    value = float(value)
    if value >= 1024:
        return f"{value / 1024:.1f}k"
    return str(int(value))


def format_context_usage(usage: dict) -> str:
    """把 ``AgentLoop.get_context_usage()`` 结果格式化为用户可读文本。

    /context 命令直接回复；Web 进度条另有前端渲染，不依赖本文本。
    """
    budget = usage.get("budget")
    system_tokens = usage.get("system_tokens")
    history_tokens = usage.get("history_tokens")
    ratio = usage.get("ratio")
    last = usage.get("last_usage") or {}
    input_tokens = last.get("input_tokens")
    cache_ratio = last.get("cache_ratio")

    lines = ["当前会话上下文占用："]
    if budget is not None:
        lines.append(f"· 预算 {_fmt_compact(budget)}（{int(budget):,} tokens）")
    else:
        lines.append("· 预算：未启用上下文压缩")
    if input_tokens is not None:
        hit = (
            f"（缓存命中 {cache_ratio * 100:.0f}%）"
            if cache_ratio is not None else ""
        )
        calls = last.get("calls")
        calls_part = f"，调用 {calls} 次" if isinstance(calls, int) else ""
        lines.append(
            f"· 上一回合 input_tokens：{int(input_tokens):,}{hit}{calls_part}"
        )
    else:
        lines.append("· 上一回合 input_tokens：暂无真实数据")
    sys_part = _fmt_compact(system_tokens) if system_tokens is not None else "未知"
    his_part = _fmt_compact(history_tokens) if history_tokens is not None else "未知"
    lines.append(f"· 估算：System ~{sys_part} + 历史 ~{his_part}")
    if ratio is not None:
        lines.append(f"· 占用比：约 {ratio * 100:.1f}%")
    return "\n".join(lines)


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
    # 内置与成功连接的 MCP 条件工具至此全部确定。冻结排序后的 schema，
    # 后续所有会话复用同一个明确 cache boundary。
    tools_hash = tools.freeze()
    print(f"工具 Schema 已冻结：{tools_hash[:16]}（{len(tools.list_tools())} 个）")
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
    dream_scheduler = None
    dream_scheduler_task = None
    catch_up_task = None
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
            await asyncio.to_thread(shared["video_store"].clear, session_key)

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
            return await publish_reminder_delivery(
                execution,
                output,
                repository=reminder_repository,
                service=reminder_service,
                async_repository=async_repository,
                bus=bus,
            )

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

    # 每日做梦整理（TASK-011）：启动补做 + 每日定时调度。
    # - 启动补做：异步后台执行，不阻塞启动；昨天未整理则补整理前一天。
    # - 定时调度：独立 asyncio 后台 task，每个本地日到 dream_time 整理当天。
    # 两者失败静默（内部已吞异常），不会阻塞聊天或启动；与 reminders 调度器
    # 是相互独立的 task，互不影响（优雅关闭见下方 amain 收尾段）。
    dream_state = DreamState(
        os.path.join(shared["config"].workspace, "workspace", "memory")
    )

    async def run_dream(date_str: str) -> None:
        await run_dream_for_date(
            shared["provider"],
            shared["daily_memory"],
            shared["session_manager"],
            dream_state,
            date_str,
        )

    async def consolidate_today() -> None:
        await run_dream(_today_date(shared["config"].timezone).isoformat())

    async def catch_up_yesterday() -> None:
        target = should_catch_up(
            dream_state.read_last_dream_date(),
            _today_date(shared["config"].timezone),
        )
        if target is not None:
            await run_dream(target)

    dream_scheduler = DreamScheduler(
        consolidate_today,
        dream_time=shared["config"].dream_time,
        timezone=shared["config"].timezone,
    )
    dream_scheduler_task = dream_scheduler.start()
    catch_up_task = asyncio.create_task(catch_up_yesterday())

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
        # 清除该会话落盘的视频目录（视频文件可能几十 MB，避免无限堆积）。
        video_store = shared.get("video_store")
        if video_store is not None:
            video_store.clear(session_key)

    cli_channel._clear_callback = clear_callback

    def context_callback(session_key: str) -> str:
        """/context 命令回调：按完整 session_key 查询当前会话占用并返回文本。

        直接从 ``agents_registry`` 取 AgentLoop 调用 ``get_context_usage()``，
        不经过模型；Agent 尚未创建（如进程重启后还没聊过）时返回提示。
        供 CLI/飞书/网页注入，微信经 ``build_weixin_channel`` 走同一回调。
        """
        agent = agents_registry.get(session_key)
        if agent is None:
            return "当前会话尚未开始对话，暂无占用数据。"
        try:
            return format_context_usage(agent.get_context_usage())
        except Exception as exc:  # noqa: BLE001 - 查询失败只回提示，不扩散
            return f"⚠️ 查询上下文占用失败：{exc}"

    cli_channel._context_callback = context_callback

    # 组装所有启用的渠道。多实例由「不同文件夹 + 不同配置/端口」实现，
    # 本函数只负责按当前配置启用对应渠道。
    channels: list = []
    cfg = shared["config"]

    # CLI 仅在交互终端下启用；无终端（如纯网页部署）则跳过，由网页渠道独立运行
    cli_task_index = None
    if sys.stdin.isatty():
        cli_channel.tool_names = tools.list_tools()
        cli_channel._clear_callback = clear_callback
        cli_channel._context_callback = context_callback
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
        feishu_channel._context_callback = context_callback  # /context 占用查询
        channels.append(feishu_channel)
        print("（飞书渠道：已启用·常开）")
    else:
        print("（飞书渠道：未配置 App ID/Secret，未启用）")

    # 微信渠道：启用后由 Python 维护一个薄 Node Bridge 子进程。Bridge 独占
    # 登录凭据、cursor 和 context token；普通消息只携带稳定账号/用户 target。
    try:
        weixin_channel = build_weixin_channel(
            cfg,
            bus,
            shared["image_store"],
            shared["file_store"],
            bind_callback=(
                reminder_service.bind_weixin if reminder_service is not None else None
            ),
            unbind_callback=(
                reminder_service.unbind_weixin if reminder_service is not None else None
            ),
            suspend_callback=(
                reminder_service.suspend_weixin if reminder_service is not None else None
            ),
            context_callback=context_callback,
        )
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
        web_channel._context_callback = context_callback  # /context 占用查询
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
        timezone=cfg.timezone,
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
    if dream_scheduler_task is not None:
        watched.add(dream_scheduler_task)
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
    if dream_scheduler is not None:
        await dream_scheduler.stop()
    if catch_up_task is not None:
        catch_up_task.cancel()

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
        *([dream_scheduler_task] if dream_scheduler_task is not None else []),
        *([catch_up_task] if catch_up_task is not None else []),
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
