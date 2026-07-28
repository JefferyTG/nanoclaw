import asyncio
import unittest

from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.spawn import SpawnSubagentTool


class _SlowTool(Tool):
    name = "slow"
    description = "A deliberately slow ordinary tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    execution_timeout_sec = 0.01

    async def execute(self, **kwargs):
        await asyncio.sleep(1)
        return "unexpected"


class _ChildLifecycleTool(SpawnSubagentTool):
    """Minimal SpawnSubagentTool double with its inherited timeout policy."""

    def __init__(self):
        pass

    async def execute(self, **kwargs):
        await asyncio.sleep(0.05)
        return "child-finished"


class _CancellableChildTool(_ChildLifecycleTool):
    name = "cancellable_child"

    def __init__(self):
        self.cancelled = asyncio.Event()

    async def execute(self, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class SpawnTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_tool_uses_its_registry_timeout(self):
        registry = ToolRegistry()
        registry.register(_SlowTool())

        result = await registry.execute("slow", {})

        self.assertIn("执行超时（0.01秒）", result)

    async def test_tool_with_no_registry_timeout_can_finish_on_its_own(self):
        registry = ToolRegistry()
        registry.register(_ChildLifecycleTool())

        result = await asyncio.wait_for(registry.execute("spawn_subagent", {}), 0.2)

        self.assertEqual(result, "child-finished")

    async def test_outer_cancellation_reaches_tool_without_registry_timeout(self):
        registry = ToolRegistry()
        tool = _CancellableChildTool()
        registry.register(tool)

        task = asyncio.create_task(registry.execute("cancellable_child", {}))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(tool.cancelled.wait(), 0.1)


if __name__ == "__main__":
    unittest.main()
