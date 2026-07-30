"""A deterministic, timezone-aware current-time tool for model calls."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from agent.tools.base import Tool
from config import validate_iana_timezone


class CurrentTimeTool(Tool):
    """Return the current time in the instance default or a valid IANA zone."""

    name = "get_current_time"
    description = (
        "获取当前日期、时间、星期、UTC 偏移和 IANA 时区。仅在需要知道现在、今天/明天、"
        "星期、时间差、提醒或定时任务时调用；不要用 exec 查询时间。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "可选 IANA 时区，例如 Asia/Shanghai；省略时使用实例默认时区。",
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    def __init__(
        self,
        default_timezone: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.default_timezone = self._validated_timezone(default_timezone)
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _validated_timezone(value: object) -> str:
        return validate_iana_timezone(value)

    async def execute(self, timezone: str | None = None, **kwargs) -> str:
        if kwargs:
            return "错误：只接受可选参数 timezone。"
        try:
            selected = self.default_timezone if timezone is None else self._validated_timezone(timezone)
        except ValueError:
            return "错误：timezone 必须是有效的 IANA timezone，例如 Asia/Shanghai。"

        now = self._clock()
        if not isinstance(now, datetime):
            return "错误：时间源返回了无效值。"
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        local = now.astimezone(ZoneInfo(selected))
        return json.dumps(
            {
                "date": local.date().isoformat(),
                "datetime": local.isoformat(timespec="seconds"),
                "time": local.strftime("%H:%M:%S%z")[:8]
                + local.strftime("%z")[:3]
                + ":"
                + local.strftime("%z")[3:],
                "timezone": selected,
                "utc_offset": local.strftime("%z")[:3] + ":" + local.strftime("%z")[3:],
                "weekday": local.strftime("%A"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
