import unittest

from agent.tool_factories import (
    PrivateToolManifestError,
    ToolFactoryError,
    ToolFactoryRegistry,
    UnknownToolFactoryError,
)
from agent.tools.base import Tool


class FakeTool(Tool):
    description = "fake private tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, config: dict) -> None:
        self._name = name
        self.config = config

    @property
    def name(self) -> str:
        return self._name

    async def execute(self, **kwargs) -> str:
        return "ok"


class ToolFactoryRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolFactoryRegistry()
        self.created: list[FakeTool] = []

        def factory(name, config):
            tool = FakeTool(name, dict(config))
            self.created.append(tool)
            return tool

        def validate(config):
            if config.get("mode") != "safe":
                raise ValueError("invalid configuration")
            return {**config, "validated": True}

        self.registry.register("fake", factory, config_validator=validate)
        self.manifest = {
            "name": "private_echo",
            "factory": "fake",
            "config": {"mode": "safe", "nested": {"retry": 1}},
        }

    def test_builds_fresh_tool_instances_for_each_dispatch(self) -> None:
        first = self.registry.build_many([self.manifest])[0]
        second = self.registry.build_many([self.manifest])[0]

        self.assertIsNot(first, second)
        self.assertEqual(
            first.config,
            {"mode": "safe", "nested": {"retry": 1}, "validated": True},
        )
        first.config["nested"]["retry"] = 2
        self.assertEqual(second.config["nested"]["retry"], 1)
        self.assertEqual(len(self.created), 2)

    def test_validation_does_not_construct_tool(self) -> None:
        normalized = self.registry.validate_manifest(self.manifest)

        self.assertEqual(normalized["name"], "private_echo")
        self.assertEqual(self.created, [])

    def test_rejects_unknown_factory(self) -> None:
        with self.assertRaisesRegex(UnknownToolFactoryError, "未注册.*missing"):
            self.registry.build({**self.manifest, "factory": "missing"})

    def test_rejects_duplicate_factory_registration(self) -> None:
        with self.assertRaisesRegex(ToolFactoryError, "fake.*已注册"):
            self.registry.register(
                "fake",
                lambda name, config: FakeTool(name, dict(config)),
                config_validator=lambda config: config,
            )

    def test_rejects_bad_manifest_shape_and_name(self) -> None:
        with self.assertRaisesRegex(PrivateToolManifestError, "缺少字段.*config"):
            self.registry.build({"name": "private_echo", "factory": "fake"})
        with self.assertRaisesRegex(PrivateToolManifestError, "name.*只能包含"):
            self.registry.build({**self.manifest, "name": "../escape"})
        with self.assertRaisesRegex(PrivateToolManifestError, "config 必须是对象"):
            self.registry.build({**self.manifest, "config": ["safe"]})

    def test_reports_invalid_config_without_echoing_values(self) -> None:
        secret = "do-not-echo-this-secret"
        with self.assertRaisesRegex(PrivateToolManifestError, "config 校验失败") as caught:
            self.registry.build({**self.manifest, "config": {"mode": "unsafe", "api_key": secret}})
        self.assertNotIn(secret, str(caught.exception))

    def test_rejects_duplicate_tool_names_in_one_dispatch(self) -> None:
        with self.assertRaisesRegex(PrivateToolManifestError, "名称重复"):
            self.registry.build_many([self.manifest, self.manifest])


if __name__ == "__main__":
    unittest.main()
