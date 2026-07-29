"""Agent-facing tools for persistent reminders and scheduled Agent runs."""

from __future__ import annotations

from agent.tools.base import Tool


class CreateReminderTool(Tool):
    """Persist one validated reminder through the shared ReminderService."""

    name = "create_reminder"
    description = (
        "创建会主动发送到已绑定飞书私聊的定时任务。用户说法有歧义时必须先确认，"
        "尤其是‘每隔一天/每隔两天’；不要猜测。一次性任务使用 count=1。"
        "message 类型必须传入创建时已写好的最终 delivery_text，到点不调用模型；"
        "agent 类型必须传入到点才执行的 agent_prompt。不得传 chat_id。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "enum": ["message", "agent"],
                "description": "message=直接发送预生成正文；agent=到点运行实时任务。",
            },
            "subject": {
                "type": "string",
                "description": "供查询列表展示的简短主题。",
            },
            "delivery_text": {
                "type": "string",
                "description": "message 专用：按当前人设写好的克制、准确、自包含最终正文。",
            },
            "agent_prompt": {
                "type": "string",
                "description": "agent 专用：到点交给独立 Agent 会话执行的完整任务说明。",
            },
            "start_at": {
                "type": "string",
                "description": "首个本地墙钟时间，ISO-8601，例如 2026-08-01T09:00:00。",
            },
            "timezone": {
                "type": "string",
                "description": "IANA 时区，例如 Asia/Shanghai；不要使用 CST 等缩写。",
            },
            "frequency": {
                "type": "string",
                "enum": ["HOURLY", "DAILY", "WEEKLY", "MONTHLY"],
                "description": "RFC 5545 FREQ。一次性任务也用 DAILY 并设置 count=1。",
            },
            "interval": {
                "type": "integer",
                "minimum": 1,
                "description": "每 N 个 frequency 周期执行，默认 1。",
            },
            "by_weekday": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                },
                "uniqueItems": True,
                "description": "可选星期过滤。",
            },
            "by_monthday": {
                "type": "array",
                "items": {"type": "integer", "minimum": -31, "maximum": 31},
                "uniqueItems": True,
                "description": "可选月日期；允许 -31~-1 或 1~31，不允许 0。",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "description": "可选总执行次数；与 until 互斥。一次性任务设为 1。",
            },
            "until": {
                "type": "string",
                "description": "可选带时区 ISO-8601 截止时间；与 count 互斥。",
            },
        },
        "required": [
            "task_type", "subject", "start_at", "timezone", "frequency",
        ],
        "additionalProperties": False,
    }

    def __init__(self, service) -> None:
        self.service = service

    async def execute(self, **kwargs) -> str:
        return await self.service.create_reminder(**kwargs)


class ListRemindersTool(Tool):
    """List reminder tasks belonging to the instance's bound target."""

    name = "list_reminders"
    description = "查询当前实例已绑定飞书私聊的定时任务；默认只列仍活动的任务。"
    parameters = {
        "type": "object",
        "properties": {
            "include_inactive": {
                "type": "boolean",
                "description": "是否同时列出已完成、已取消或失败任务，默认 false。",
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, service) -> None:
        self.service = service

    async def execute(self, **kwargs) -> str:
        return await self.service.list_reminders(
            include_inactive=bool(kwargs.get("include_inactive", False))
        )


class CancelReminderTool(Tool):
    """Cancel one task after checking target ownership in the repository."""

    name = "cancel_reminder"
    description = "取消当前已绑定飞书目标下的一个定时任务；不会影响其它任务。"
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "list_reminders 返回的任务 ID。",
            }
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }

    def __init__(self, service) -> None:
        self.service = service

    async def execute(self, **kwargs) -> str:
        task_id = kwargs.get("task_id")
        if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 1:
            return "错误：task_id 必须是正整数。"
        return await self.service.cancel_reminder(task_id)
