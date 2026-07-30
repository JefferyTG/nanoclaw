import unittest

from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry


class _NamedTool(Tool):
    description = "deterministic tool"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": [],
    }

    def __init__(self, name: str):
        self._name = name

    @property
    def name(self):
        return self._name

    async def execute(self, **kwargs):
        return "ok"


class ToolSchemaCacheTests(unittest.TestCase):
    @staticmethod
    def _registry(*names: str) -> ToolRegistry:
        registry = ToolRegistry()
        for name in names:
            registry.register(_NamedTool(name))
        return registry

    def test_registration_order_does_not_change_schema_or_hash(self):
        first = self._registry("builtin_b", "mcp_a")
        second = self._registry("mcp_a", "builtin_b")

        self.assertEqual(first.get_definitions(), second.get_definitions())
        self.assertEqual(first.schema_hash, second.schema_hash)
        self.assertEqual(first.list_tools(), ["builtin_b", "mcp_a"])

    def test_freeze_is_immutable_and_partial_tool_set_is_a_new_boundary(self):
        complete = self._registry("builtin", "mcp_success")
        complete_hash = complete.freeze()
        returned = complete.get_definitions()
        returned[0]["function"]["description"] = "caller mutation"

        self.assertNotEqual(
            returned[0]["function"]["description"],
            complete.get_definitions()[0]["function"]["description"],
        )
        with self.assertRaises(RuntimeError):
            complete.register(_NamedTool("late"))

        partial = self._registry("builtin")
        self.assertNotEqual(complete_hash, partial.freeze())


if __name__ == "__main__":
    unittest.main()
