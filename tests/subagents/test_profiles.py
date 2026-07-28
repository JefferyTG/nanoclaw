import json
import os
import tempfile
import unittest

from agent.profiles import AgentProfileError, AgentProfileLoader


class AgentProfileLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.loader = AgentProfileLoader(os.path.join(self.tempdir.name, "agents"))
        self.data = {
            "name": "xiaohongshu",
            "description": "负责内容创作",
            "system_prompt": "你是内容策划。",
            "model": "",
            "tools": ["web_search", "read_file"],
            "skills": ["xhs-writing"],
        }

    def test_create_get_list_and_summary(self):
        created = self.loader.create_profile(self.data)

        self.assertEqual(created.name, "xiaohongshu")
        self.assertEqual(self.loader.get_profile("xiaohongshu"), created)
        self.assertEqual(self.loader.list_profiles(), [created])
        self.assertEqual(self.loader.build_summary(), "- xiaohongshu：负责内容创作")

        path = os.path.join(
            self.tempdir.name, "agents", "xiaohongshu", "profile.json"
        )
        with open(path, encoding="utf-8") as file:
            stored = json.load(file)
        self.assertEqual(stored, created.to_dict())
        self.assertEqual(created.version, 2)
        self.assertEqual(created.private_skills, [])
        self.assertEqual(created.private_tools, [])

    def test_rejects_duplicate_invalid_name_and_bad_lists(self):
        self.loader.create_profile(self.data)
        with self.assertRaisesRegex(AgentProfileError, "已存在"):
            self.loader.create_profile(self.data)

        for name in ("../escape", "bad name", ""):
            bad = {**self.data, "name": name}
            with self.subTest(name=name), self.assertRaises(AgentProfileError):
                self.loader.create_profile(bad)

        with self.assertRaisesRegex(AgentProfileError, "tools"):
            self.loader.create_profile({**self.data, "name": "bad-tools", "tools": "all"})
        with self.assertRaisesRegex(AgentProfileError, "skills"):
            self.loader.create_profile({**self.data, "name": "bad-skills", "skills": [1]})

    def test_symlink_profile_cannot_escape_agents_directory(self):
        os.makedirs(self.loader.profiles_dir)
        outside = os.path.join(self.tempdir.name, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "profile.json"), "w", encoding="utf-8") as file:
            json.dump({**self.data, "name": "linked", "version": 2}, file)
        os.symlink(outside, os.path.join(self.loader.profiles_dir, "linked"))

        with self.assertRaisesRegex(AgentProfileError, "越过"):
            self.loader.get_profile("linked")

    def test_ignores_legacy_single_file_profile(self):
        os.makedirs(self.loader.profiles_dir)
        legacy_path = os.path.join(self.loader.profiles_dir, "legacy.json")
        legacy = {**self.data, "name": "legacy"}
        with open(legacy_path, "w", encoding="utf-8") as file:
            json.dump(legacy, file)

        profile = self.loader.get_profile("legacy")

        self.assertIsNone(profile)
        self.assertEqual(self.loader.list_profiles(), [])
        self.assertTrue(os.path.isfile(legacy_path))

    def test_update_profile_is_atomic_and_cannot_rename(self):
        self.loader.create_profile(self.data)

        updated = self.loader.update_profile(
            "xiaohongshu", {"private_skills": ["brand-style"]}
        )

        self.assertEqual(updated.private_skills, ["brand-style"])
        self.assertEqual(
            self.loader.get_profile("xiaohongshu").private_skills, ["brand-style"]
        )
        with self.assertRaisesRegex(AgentProfileError, "重命名"):
            self.loader.update_profile("xiaohongshu", {"name": "renamed"})


if __name__ == "__main__":
    unittest.main()
