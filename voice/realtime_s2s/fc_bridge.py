"""Function Calling 桥接骨架（TASK-037）。

豆包全双工支持函数调用（``response.function_call_arguments.done`` 按
``call_id`` 配对）。本任务**不注册任何工具**（tools 空数组），只把架构搭好：

- ``tools`` 属性：传给 session.create 的工具声明数组（默认空 = 不启用 FC）；
- ``handle(event)``：收到 ``response.function_call_arguments.done`` 时按
  ``call_id`` 记录 ``(name, arguments)`` 到 ``_pending``；默认（无执行器）
  仅打日志；注入 ``executor(call_id, name, arguments)`` 后可插真实工具。

后续接入 FC 工具时：注册工具 → tools 非空 → 执行器回调返回结果 → 经
``response.create``（text 或 audio）回传豆包。POC 阶段全部留空即可。
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from loguru import logger

# 执行器签名：async (call_id, name, arguments: dict) -> Any
FcExecutor = Callable[[str, str, dict], Awaitable[object]]


class FcBridge:
    """函数调用桥接骨架：tools 声明 + call_id 配对执行器预留，默认仅日志。"""

    def __init__(
        self, tools: list | None = None, executor: FcExecutor | None = None
    ) -> None:
        self._tools = list(tools or [])
        self._executor = executor
        self._pending: dict[str, tuple[str, dict]] = {}

    @property
    def tools(self) -> list:
        """当前注册的工具声明（空 = 不启用函数调用）。"""
        return list(self._tools)

    def register_executor(self, executor: FcExecutor) -> None:
        """预留：注册真实工具执行器（POC 阶段不调用）。"""
        self._executor = executor

    def pending(self) -> dict[str, tuple[str, dict]]:
        """只读视图：call_id → (name, arguments) 配对表（测试/调试用）。"""
        return dict(self._pending)

    async def handle(self, event: dict) -> None:
        """处理 ``response.function_call_arguments.done``：按 call_id 配对并执行。"""
        call_id = event.get("call_id")
        if not call_id:
            logger.warning("fc_bridge: 函数调用事件缺 call_id，忽略")
            return
        name = str(event.get("name") or "")
        raw_args = event.get("arguments") or ""
        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except (TypeError, ValueError):
            arguments = {}
        self._pending[call_id] = (name, arguments)
        if self._executor is None:
            logger.info(
                f"fc_bridge: 收到函数调用（未注册执行器，仅记录）"
                f"call_id={call_id} name={name}"
            )
            return
        try:
            result = await self._executor(call_id, name, arguments)
        except Exception as exc:  # noqa: BLE001 - 工具失败不阻断会话
            logger.warning(f"fc_bridge: 函数执行失败 call_id={call_id} name={name}：{exc}")
            return
        # 预留：把 result 回传给豆包（S2S 暂无标准函数结果通道，POC 仅日志）
        logger.info(
            f"fc_bridge: 函数执行完成 call_id={call_id} name={name} "
            f"result={str(result)[:200]}"
        )
