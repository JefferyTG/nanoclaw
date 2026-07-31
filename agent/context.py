"""Stable, session-scoped model context construction.

``ContextBuilder`` takes a snapshot of prompt inputs when a session is created.
The snapshot deliberately remains unchanged across turns so an append-only
conversation is also an exact API-message prefix.  Call ``refresh_snapshot``
at a deliberate session boundary when local prompt inputs must be reloaded.
"""

import os
from collections.abc import Callable
from typing import Optional

from agent.identity import DEFAULT_IDENTITY


class ContextBuilder:
    """Build a stable system prompt plus append-only chat messages.

    Identity, USER.md, MEMORY.md, skill text, and the scene-agent summary are
    session snapshots.  This makes their refresh boundary explicit instead of
    silently changing every request's system message.
    """

    def __init__(
        self,
        workspace: str,
        identity_file: str = "identity.md",
        skills_summary: str = "",
        agents_summary: str = "",
        agents_summary_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.workspace = os.path.abspath(workspace)
        self.identity_file = identity_file
        self.skills_summary = skills_summary
        self.agents_summary = agents_summary
        self.agents_summary_provider = agents_summary_provider
        self.memory_path = os.path.join(self.workspace, "workspace", "memory", "MEMORY.md")
        self.user_path = os.path.join(self.workspace, "workspace", "memory", "USER.md")
        self._snapshot: dict[str, str] = {}
        self.refresh_snapshot()

    @staticmethod
    def _read_text(path: str, fallback: str = "") -> str:
        try:
            with open(path, "r", encoding="utf-8") as file:
                return file.read().strip()
        except (OSError, UnicodeDecodeError):
            return fallback

    def _load_identity(self) -> str:
        return self._read_text(
            os.path.join(self.workspace, self.identity_file), DEFAULT_IDENTITY
        )

    def _load_memory(self) -> str:
        return self._read_text(self.memory_path)

    def _load_user(self) -> str:
        return self._read_text(self.user_path)

    def _load_agents_summary(self) -> str:
        if self.agents_summary_provider is None:
            return self.agents_summary
        try:
            return self.agents_summary_provider() or ""
        except Exception:  # noqa: BLE001 - a bad profile must not block chat
            return self.agents_summary

    def refresh_snapshot(self) -> None:
        """Reload slow-changing prompt inputs at an explicit session boundary.

        Existing callers normally create one builder per AgentLoop, so this is
        intentionally *not* invoked by ``build_system_prompt``.  A host that
        supports an explicit "refresh context" action can call it after a
        successful local edit and before the next model request.
        """
        self._snapshot = {
            "identity": self._load_identity(),
            "user": self._load_user(),
            "memory": self._load_memory(),
            "skills": self.skills_summary,
            "agents": self._load_agents_summary(),
        }

    # A descriptive alias for integrations that expose a user-visible action.
    refresh_context = refresh_snapshot

    @staticmethod
    def _memory_instructions() -> str:
        return (
            "## 记忆管理\n"
            "你有两份记忆文件，均用 write_file / read_file 直接管理（不引入其他工具）：\n"
            "- workspace/memory/USER.md：用户本人长期信息，≤3000 字符。默认分类 Basic（身份/所在地等）/ Interest（兴趣/爱好）/ Preference（交流偏好/关系设定），可按需新增分类。\n"
            "- workspace/memory/MEMORY.md：项目与工作环境，≤5000 字符。默认分类如「项目状态/已装技能/工作约定」，可按需新增分类。\n"
            "分工判据：关于「用户这个人」的（身份/兴趣/爱好/偏好/关系设定）→ USER；关于「正在做的事」的（项目/技术决策/技能/操作约定）→ MEMORY。工作约定（如「装技能前先审计」）归 MEMORY。\n\n"
            "何时该写：用户明确要求记住、用户主动告知的长期稳定信息（含兴趣/爱好）、用户纠正过你的错误、项目重要变化。\n"
            "何时不写：临时状态、一次性问题、用户未确认的推测、角色扮演内容。未确认的猜测一律不写——「用户确认」指用户主动且明确地告知或认可，不是你推测（用户说「我喜欢X」可写；你推测「用户好像喜欢X」不可写；你建议后用户没回应不可写）。\n\n"
            "写入流程（必须遵循）：1.read 目标文件现有内容 → 2.read 另一份文件检查是否已有（避免重复）→ 3.在合适分类下合并/追加（- 列表格式，分类不够可新增）→ 4.write 回完整内容（write_file 是整文件覆盖，必须写全部，不能只写增量）。超长时删低价值/过时条目。\n\n"
            "修改与删除：用户指出旧记忆不对时，read 后改/删对应条目再 write 回。用户要求「不要再提某事」时，主动 read 并删除/改写相关条目再 write 回，不要只口头答应而文件留着。发现过时信息主动更新。\n\n"
            "引用记忆与搜索：用户问起过去（「之前怎么讨论X」「你记得我提过Y吗」）用 memory_search 工具检索（scope=memory 先搜记忆，无果换 session）。搜索结果不要直接贴给用户，先理解再用你当前人设自然融入回答；检索不到就如实说，不编造。引用要自然（如「你之前也说过喜欢安静点的环境」），不要炫耀式（如「根据我的记忆，你喜欢安静」）。单次最多引用 1 条，无关联别硬引。同一条记忆 14 天内不主动再提（用户问起除外），靠你自律。\n\n"
            "Follow Up 跟进：用户表达「以后研究X」「下次试试Y」等未来意向时，记入 workspace/memory/followups.jsonl（每行一个JSON：{topic,content,created,max_remind:2,reminded_count:0,status:\"open\"}）。用 read_file 读、write_file 写回完整内容（文件不存在则新建，JSONL 每行一个对象，写回要写全部行）。当你察觉用户当前话题与某个 open 状态 followup 的 topic 相关时，自然提一句，提醒后 reminded_count+1 写回；达 max_remind 或用户表示已处理/不感兴趣则 status 改 closed；超过30天未触发主动清理；用户要求不再提立即 closed。不要每轮都读 followups，只在话题可能相关时读。"
        )

    @staticmethod
    def _reminder_instructions() -> str:
        return (
            "## 主动提醒与定时任务\n"
            "当用户要求稍后提醒、周期提醒或到点执行 Agent 任务时，必须使用 "
            "create_reminder 工具持久化，不能只口头答应。查询与取消分别使用 "
            "list_reminders、cancel_reminder；若这些工具未出现在本轮可用工具中，应明确说明当前实例未启用提醒能力。\n"
            "创建前确认本地开始时间与 IANA 时区。‘每隔一天/每隔两天’等可能指不同间隔的说法必须先向用户确认；不得自行猜测。工具成功后把返回的未来最多三次执行时间清楚展示给用户。\n"
            "message 任务的 delivery_text 是到点直接发送的最终正文：创建时就按当前人设写成克制、准确、自包含的成稿，不要包含内部说明。agent 任务只保存明确、可独立执行的 agent_prompt，到点才运行 Agent。\n"
            "提醒只能投递到用户通过飞书或微信私聊 /bind-reminders 显式绑定的唯一目标；原绑定用户可先发送 /unbind-reminders，再去另一渠道重新绑定，已有任务会跟随新目标。不得编造、索取或向工具传入 channel、chat_id、user_id。未绑定时原样转达工具给出的绑定指引。"
        )

    @staticmethod
    def _time_tool_instructions() -> str:
        return (
            "## 当前时间\n"
            "每一轮用户消息开头已自动带有当前时间戳前缀（格式 [YYYY-MM-DD HH:MM]，"
            "实例默认时区），可直接作为当前时间依据，无需再调用工具。\n"
            "仅当需要精确到秒的时间、查询其它时区的当前时间，"
            "或处理提醒/定时任务等需要标准时间格式的场景时，"
            "才调用 get_current_time 工具。"
        )

    def build_system_prompt(self) -> str:
        """Return the session-stable system prompt; it contains no wall clock."""
        snapshot = self._snapshot
        # Fixed rules lead.  Snapshot data follows, from broadly stable to more
        # frequently edited, so an explicit refresh has the narrowest practical
        # cache boundary while retaining one coherent system message.
        sections = [
            self._memory_instructions(),
            self._reminder_instructions(),
            self._time_tool_instructions(),
            "## 子 Agent 派遣\n复杂通用任务可以直接调用 spawn_subagent(task)，无需指定 agent_name。\n"
            "只有明确需要某个场景能力时，才调用 spawn_subagent(agent_name=..., task=...)。",
            "【人设】\n" + snapshot["identity"],
            f"【工作区】\n{self.workspace}",
        ]
        if snapshot["skills"]:
            sections.append("## 可用技能\n" + snapshot["skills"])
        if snapshot["user"]:
            sections.append("【用户信息】\n" + snapshot["user"])
        if snapshot["memory"]:
            sections.append("【长期记忆】\n" + snapshot["memory"])
        if snapshot["agents"]:
            sections.append("## 可派遣的场景 Agent\n" + snapshot["agents"])
        return "\n\n".join(sections)

    def build_messages(
        self, history: Optional[list[dict]] = None, current_message: str = ""
    ) -> list[dict]:
        """Build ``[system] + history + current user`` without mutating history."""
        messages: list[dict] = [{"role": "system", "content": self.build_system_prompt()}]
        if history:
            messages.extend(history)
        if current_message:
            messages.append({"role": "user", "content": current_message})
        return messages
