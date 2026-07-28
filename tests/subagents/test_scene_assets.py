import os
import tempfile
import unittest

from agent.scene_assets import (
    MAX_RESOURCE_BYTES,
    MAX_SKILL_TEXT_BYTES,
    SceneSkillAssetError,
    SceneSkillAssets,
    SceneToolAssets,
)


class SceneSkillAssetsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.assets = SceneSkillAssets(self.tempdir.name)
        self.tool_assets = SceneToolAssets(self.tempdir.name)

    def test_create_list_read_update_and_loader_view(self):
        content = "---\nname: writing\ndescription: Private writing guide\n---\n\nFIRST"
        path = self.assets.create_skill("writer", "writing", content)

        expected = os.path.join(
            self.tempdir.name, "workspace", "agents", "writer", "skills", "writing", "SKILL.md"
        )
        self.assertEqual(path, os.path.realpath(expected))
        self.assertEqual(self.assets.load_skill("writer", "writing"), "FIRST")
        self.assertEqual(
            self.assets.list_skills("writer"),
            [{"name": "writing", "description": "Private writing guide", "path": os.path.realpath(expected)}],
        )

        loader = self.assets.for_agent("writer")
        self.assertEqual(loader.load_skill("writing"), "FIRST")
        self.assertIn("Private writing guide", loader.build_skills_summary())

        self.assets.update_skill("writer", "writing", "---\ndescription: Updated\n---\nSECOND")
        self.assertEqual(self.assets.load_skill("writer", "writing"), "SECOND")

    def test_duplicate_invalid_names_and_size_limits_are_rejected(self):
        self.assets.create_skill("writer", "guide", "body")
        with self.assertRaisesRegex(SceneSkillAssetError, "已存在"):
            self.assets.create_skill("writer", "guide", "new")
        for value in ("../escape", "bad name", "", "/absolute"):
            with self.subTest(value=value), self.assertRaises(SceneSkillAssetError):
                self.assets.create_skill(value, "guide", "body")
            with self.subTest(skill=value), self.assertRaises(SceneSkillAssetError):
                self.assets.create_skill("writer", value, "body")
        with self.assertRaisesRegex(SceneSkillAssetError, "大小"):
            self.assets.create_skill("writer", "large", "x" * (MAX_SKILL_TEXT_BYTES + 1))

    def test_resources_are_bounded_and_cannot_traverse(self):
        self.assets.create_skill("writer", "guide", "body")
        root = os.path.join(
            self.tempdir.name, "workspace", "agents", "writer", "skills", "guide"
        )
        with open(os.path.join(root, "template.txt"), "w", encoding="utf-8") as file:
            file.write("template")
        self.assertEqual(self.assets.read_resource("writer", "guide", "template.txt"), "template")
        self.assertEqual(
            self.assets.for_agent("writer", ["guide"]).load_skill_resource(
                "guide", "template.txt"
            ),
            "template",
        )
        self.assertIsNone(
            self.assets.for_agent("writer", []).load_skill_resource(
                "guide", "template.txt"
            )
        )
        with self.assertRaisesRegex(SceneSkillAssetError, "相对|边界"):
            self.assets.read_resource("writer", "guide", "../guide/SKILL.md")
        with open(os.path.join(root, "large.bin"), "wb") as file:
            file.write(b"x" * (MAX_RESOURCE_BYTES + 1))
        with self.assertRaisesRegex(SceneSkillAssetError, "大小"):
            self.assets.read_resource("writer", "guide", "large.bin", binary=True)

    def test_symlinks_cannot_escape_and_agents_are_isolated(self):
        self.assets.create_skill("alpha", "guide", "ALPHA")
        self.assets.create_skill("beta", "guide", "BETA")
        self.assertEqual(self.assets.load_skill("alpha", "guide"), "ALPHA")
        self.assertEqual(self.assets.load_skill("beta", "guide"), "BETA")

        alpha_root = os.path.join(
            self.tempdir.name, "workspace", "agents", "alpha", "skills", "guide"
        )
        outside = os.path.join(self.tempdir.name, "outside.txt")
        with open(outside, "w", encoding="utf-8") as file:
            file.write("SECRET")
        os.symlink(outside, os.path.join(alpha_root, "escape.txt"))
        with self.assertRaisesRegex(SceneSkillAssetError, "边界|符号链接"):
            self.assets.read_resource("alpha", "guide", "escape.txt")

        agents_dir = os.path.join(self.tempdir.name, "workspace", "agents")
        os.symlink(outside, os.path.join(agents_dir, "linked"))
        with self.assertRaisesRegex(SceneSkillAssetError, "符号链接|边界"):
            self.assets.list_skills("linked")

    def test_private_tool_manifests_are_isolated_atomic_and_secret_safe(self):
        manifest = {
            "name": "publisher",
            "factory": "approved",
            "config": {"api_key_env": "PUBLISH_API_KEY", "timeout": 10},
        }
        path = self.tool_assets.create_tool("writer", manifest)

        self.assertTrue(path.endswith("writer/tools/publisher.json"))
        self.assertEqual(self.tool_assets.load_tool("writer", "publisher"), manifest)
        self.assertEqual(self.tool_assets.list_tools("writer"), [manifest])
        self.assertIsNone(self.tool_assets.load_tool("other", "publisher"))
        with self.assertRaisesRegex(SceneSkillAssetError, "已存在"):
            self.tool_assets.create_tool("writer", manifest)

        updated = {**manifest, "config": {"api_key_env": "NEW_KEY", "timeout": 20}}
        self.tool_assets.update_tool("writer", updated)
        self.assertEqual(self.tool_assets.load_tool("writer", "publisher"), updated)

        with self.assertRaisesRegex(SceneSkillAssetError, "不得保存密钥"):
            self.tool_assets.create_tool(
                "writer",
                {**manifest, "name": "unsafe", "config": {"api_key": "secret"}},
            )
        for field in ("key", "bearer_token", "private_key", "service_auth"):
            with self.subTest(field=field), self.assertRaisesRegex(
                SceneSkillAssetError, "不得保存密钥"
            ):
                self.tool_assets.create_tool(
                    "writer",
                    {
                        **manifest,
                        "name": "unsafe_" + field,
                        "config": {field: "secret"},
                    },
                )
        with self.assertRaisesRegex(SceneSkillAssetError, "环境变量"):
            self.tool_assets.create_tool(
                "writer",
                {**manifest, "name": "bad_env", "config": {"token_env": "bad env"}},
            )

    def test_private_skill_frontmatter_name_must_match_directory(self):
        self.assets.create_skill(
            "writer", "directory-name", "---\nname: other\n---\nbody"
        )
        with self.assertRaisesRegex(SceneSkillAssetError, "目录名"):
            self.assets.list_skills("writer")


if __name__ == "__main__":
    unittest.main()
