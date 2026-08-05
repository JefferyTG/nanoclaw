"""Tests for TASK-001 渠道感知.

Agent 经会话级快照（System Prompt「当前渠道」section）感知自身所在渠道
（feishu/weixin/web/cli）与用户标识（sender_id）。渠道信息源自
``make_agent_factory`` 对 ``session_key`` 的解析（``split(":", 1)`` 只切
第一刀，防止 sender_id 本身含冒号裂解），在会话内恒定，不破坏 System
Prompt 前缀稳定（Prompt Cache 友好）。

测试直接驱动真实 ``make_agent_factory`` 工厂（最小 shared 字典），覆盖
factory 内 session_key → ContextBuilder 的真实解析链路，而不只是单独
测 ContextBuilder。
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context import ContextBuilder
from agent.tools.registry import ToolRegistry
from main import make_agent_factory
from session.manager import SessionManager

# 「当前渠道」section 在 System Prompt 中的固定标题
CHANNEL_SECTION_TITLE = "## 当前渠道"


def _make_shared(workspace: str) -> dict:
    """构建 make_agent_factory 所需的最小 shared 字典（真实代码路径）。

    仅填充 factory 实际访问的键；不触发任何网络请求（Provider 构造只建
    客户端对象，不发请求）。
    """
    return {
        "config": SimpleNamespace(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="test-model",
            workspace=workspace,
            identity_file="identity.md",
            max_iterations=4,
            turn_timeout_sec=10,
            base_model_multimodal=False,
            # TASK-006：factory 每会话创建 ContextCompactor，需读取该预算字段
            context_budget_tokens=524288,
        ),
        "skills_summary": "",
        "profile_loader": SimpleNamespace(build_summary=lambda: ""),
        "tools": ToolRegistry(),
        "session_manager": SessionManager(os.path.join(workspace, "sessions")),
        "image_store": None,
    }


class ChannelContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        workspace = self.tmp.name
        root = Path(workspace)
        (root / "identity.md").write_text("identity v1", encoding="utf-8")
        (root / "workspace" / "memory").mkdir(parents=True)
        (root / "workspace" / "memory" / "USER.md").write_text(
            "user v1", encoding="utf-8"
        )
        (root / "workspace" / "memory" / "MEMORY.md").write_text(
            "memory v1", encoding="utf-8"
        )
        registry: dict = {}
        self.factory = make_agent_factory(_make_shared(workspace), registry)
        self.registry = registry

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _channel_section(self, session_key: str) -> str:
        """通过 factory 创建 Agent，返回其 System Prompt 的「当前渠道」section。"""
        agent = self.factory(session_key)
        prompt = agent.context.build_system_prompt()
        self.assertIn(CHANNEL_SECTION_TITLE, prompt)
        lines = prompt.splitlines()
        idx = lines.index(CHANNEL_SECTION_TITLE)
        # section 正文紧随标题（直到空行/下一个 ## 之前，此处只取下一行）
        return lines[idx + 1]

    def test_different_channels_and_users_in_system_prompt(self):
        cases = [
            ("feishu:xxx", "feishu", "xxx"),
            ("weixin:yyy", "weixin", "yyy"),
            ("web:zzz", "web", "zzz"),
            ("cli:local1", "cli", "local1"),
        ]
        for session_key, channel, sender in cases:
            with self.subTest(session_key=session_key):
                section = self._channel_section(session_key)
                self.assertIn(f"本会话所在渠道：{channel}", section)
                self.assertIn(f"用户标识：{sender}", section)
                self.assertIn(
                    "渠道名取内部名（feishu/weixin/web/cli）", section
                )

    def test_sender_id_with_colon_does_not_split(self):
        """sender_id 含冒号（如 feishu:user:name）→ 只切第一刀，不裂解。"""
        section = self._channel_section("feishu:user:name")
        self.assertIn("本会话所在渠道：feishu", section)
        self.assertIn("用户标识：user:name", section)
        self.assertNotIn("本会话所在渠道：feishu:user", section)

    def test_scheduled_session_gets_channel_snapshot(self):
        """提醒/定时任务会话（scheduled:task:exec）同样获得渠道快照，不回归。"""
        section = self._channel_section("scheduled:123:456")
        self.assertIn("本会话所在渠道：scheduled", section)
        self.assertIn("用户标识：123:456", section)

    def test_system_prompt_stable_within_session(self):
        """同一 Agent 会话内 System Prompt 恒定（不破坏 Prompt Cache 前缀稳定）。"""
        agent = self.factory("cli:local1")
        self.assertEqual(
            agent.context.build_system_prompt(),
            agent.context.build_system_prompt(),
        )
        # 追加式多轮：下一轮请求精确包含上一轮请求前缀
        first = agent.context.build_messages(current_message="first")
        second = agent.context.build_messages(
            history=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
            ],
            current_message="second",
        )
        self.assertEqual(second[: len(first)], first)

    def test_refresh_snapshot_keeps_channel_constant(self):
        """refresh_snapshot 刷新文件类快照时，渠道快照保持恒定。"""
        agent = self.factory("weixin:yyy")
        before = agent.context.build_system_prompt()
        Path(self.tmp.name, "identity.md").write_text(
            "identity v2", encoding="utf-8"
        )
        # 未显式刷新：System Prompt 不变
        self.assertEqual(before, agent.context.build_system_prompt())
        agent.context.refresh_context()
        refreshed = agent.context.build_system_prompt()
        self.assertIn("identity v2", refreshed)
        self.assertIn(CHANNEL_SECTION_TITLE, refreshed)
        self.assertIn("本会话所在渠道：weixin", refreshed)
        self.assertIn("用户标识：yyy", refreshed)

    def test_builder_without_channel_omits_section(self):
        """未提供渠道信息（如共享上下文构建器）时不注入「当前渠道」section。"""
        builder = ContextBuilder(self.tmp.name)
        prompt = builder.build_system_prompt()
        self.assertNotIn(CHANNEL_SECTION_TITLE, prompt)
        self.assertNotIn("本会话所在渠道", prompt)


if __name__ == "__main__":
    unittest.main()
