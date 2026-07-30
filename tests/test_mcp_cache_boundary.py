import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.tools.mcp import MCPClientManager


class _AsyncContext:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, *args):
        return None


class _SessionContext:
    def __init__(self, *_args):
        self.session = _Session()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return None


class _BrokenTool:
    @property
    def name(self):
        raise ValueError("broken tool metadata")


class _Session:
    async def initialize(self):
        return None

    async def list_tools(self):
        good = SimpleNamespace(
            name="good", description="good", inputSchema={"type": "object"}
        )
        return SimpleNamespace(tools=[good, _BrokenTool()])


class MCPCacheBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_tool_listing_rolls_back_all_server_wrappers(self):
        manager = MCPClientManager({"broken": {"command": "fake"}})
        with patch("agent.tools.mcp.stdio_client", return_value=_AsyncContext()), patch(
            "agent.tools.mcp.ClientSession", _SessionContext
        ):
            await manager.connect_all(timeout=1)

        self.assertEqual(manager.get_tools(), [])
        self.assertNotIn("broken", manager._sessions)


if __name__ == "__main__":
    unittest.main()
