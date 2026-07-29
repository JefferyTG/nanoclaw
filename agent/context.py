"""上下文构建器（ContextBuilder）。

负责把「系统人设 + 当前时间 + 工作区 + 用户信息(USER) + 长期记忆(MEMORY)」
拼装成给模型的 System Prompt，并进一步组合出完整的 messages 列表
（System + 历史 + 当前输入）。

这是 Agent 与模型对话前的「上下文装配中心」：把散落在文件（identity.md、
memory/USER.md、memory/MEMORY.md）与时间里的信息，统一收敛成模型能消费的文本。

说明：
- 人设文件默认位于 ``<workspace>/identity.md``，可由构造参数覆盖文件名。
- 用户信息默认位于 ``<workspace>/workspace/memory/USER.md``。
- 长期记忆默认位于 ``<workspace>/workspace/memory/MEMORY.md``。
- 两者读取失败或文件不存在时返回空字符串，不影响主流程。
"""


import os
from datetime import datetime
from collections.abc import Callable
from typing import List, Optional

from agent.identity import DEFAULT_IDENTITY


class ContextBuilder:
    """构建 Agent 所需的 System Prompt 与完整 messages。"""

    def __init__(
        self,
        workspace: str,
        identity_file: str = "identity.md",
        skills_summary: str = "",
        agents_summary: str = "",
        agents_summary_provider: Optional[Callable[[], str]] = None,
    ):
        self.workspace = os.path.abspath(workspace)
        self.identity_file = identity_file
        self.skills_summary = skills_summary
        self.agents_summary = agents_summary
        self.agents_summary_provider = agents_summary_provider
        # 注意：MEMORY.md 路径按第六章约定放在 <workspace>/memory/ 下
        self.memory_path = os.path.join(self.workspace, "workspace", "memory", "MEMORY.md")
        # USER.md：用户长期稳定信息（身份/兴趣/偏好），与 MEMORY.md 同级
        self.user_path = os.path.join(self.workspace, "workspace", "memory", "USER.md")

    def _load_identity(self) -> str:
        """读取人设文件内容；文件不存在时返回默认人设。"""
        path = os.path.join(self.workspace, self.identity_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return DEFAULT_IDENTITY
        except Exception as exc:  # noqa: BLE001 - 读取异常时降级为默认人设
            return DEFAULT_IDENTITY + f"\n（人设文件读取失败：{exc}）"

    def _load_memory(self) -> str:
        """读取长期记忆（MEMORY.md）；不存在或读取失败返回空字符串。"""
        try:
            with open(self.memory_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except Exception:  # noqa: BLE001 - 记忆缺失不应阻断对话
            return ""

    def _load_user(self) -> str:
        """读取用户长期信息（USER.md）；不存在或读取失败返回空字符串。

        USER.md 保存用户身份、长期兴趣、长期偏好等稳定信息，与
        MEMORY.md（工作记忆）分工：USER 偏「人」，MEMORY 偏「事」。
        """
        try:
            with open(self.user_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except Exception:  # noqa: BLE001 - USER 信息缺失不应阻断对话
            return ""

    def _memory_instructions(self) -> str:
        """记忆管理完整规则（每轮注入 system prompt，靠 prompt cache 摊销成本）。

        规则完整写入而非「简版 + 按需 load_skill」——主流 agent（Claude Code /
        Cursor / OpenClaw）均把规则文件每 session 注入 prompt，不依赖模型主动
        load。规则处于固定前缀位置，prompt cache 命中后几乎零成本。
        """
        return (
            "\n\n## 记忆管理\n"
            "你有两份记忆文件，均用 write_file / read_file 直接管理（不引入其他工具）：\n"
            "- workspace/memory/USER.md：用户本人长期信息，≤3000 字符。默认分类 Basic（身份/所在地等）/ Interest（兴趣/爱好）/ Preference（交流偏好/关系设定），可按需新增分类。\n"
            "- workspace/memory/MEMORY.md：项目与工作环境，≤5000 字符。默认分类如「项目状态/已装技能/工作约定」，可按需新增分类。\n"
            "分工判据：关于「用户这个人」的（身份/兴趣/爱好/偏好/关系设定）→ USER；关于「正在做的事」的（项目/技术决策/技能/操作约定）→ MEMORY。工作约定（如「装技能前先审计」）归 MEMORY。\n"
            "\n"
            "何时该写：用户明确要求记住、用户主动告知的长期稳定信息（含兴趣/爱好）、用户纠正过你的错误、项目重要变化。\n"
            "何时不写：临时状态、一次性问题、用户未确认的推测、角色扮演内容。未确认的猜测一律不写——「用户确认」指用户主动且明确地告知或认可，不是你推测（用户说「我喜欢X」可写；你推测「用户好像喜欢X」不可写；你建议后用户没回应不可写）。\n"
            "\n"
            "写入流程（必须遵循）：1.read 目标文件现有内容 → 2.read 另一份文件检查是否已有（避免重复）→ 3.在合适分类下合并/追加（- 列表格式，分类不够可新增）→ 4.write 回完整内容（write_file 是整文件覆盖，必须写全部，不能只写增量）。超长时删低价值/过时条目。\n"
            "\n"
            "修改与删除：用户指出旧记忆不对时，read 后改/删对应条目再 write 回。用户要求「不要再提某事」时，主动 read 并删除/改写相关条目再 write 回，不要只口头答应而文件留着。发现过时信息主动更新。\n"
            "\n"
            "引用记忆与搜索：用户问起过去（「之前怎么讨论X」「你记得我提过Y吗」）用 memory_search 工具检索（scope=memory 先搜记忆，无果换 session）。搜索结果不要直接贴给用户，先理解再用你当前人设自然融入回答；检索不到就如实说，不编造。引用要自然（如「你之前也说过喜欢安静点的环境」），不要炫耀式（如「根据我的记忆，你喜欢安静」）。单次最多引用 1 条，无关联别硬引。同一条记忆 14 天内不主动再提（用户问起除外），靠你自律。\n"
            "\n"
            "Follow Up 跟进：用户表达「以后研究X」「下次试试Y」等未来意向时，记入 workspace/memory/followups.jsonl（每行一个JSON：{topic,content,created,max_remind:2,reminded_count:0,status:\"open\"}）。用 read_file 读、write_file 写回完整内容（文件不存在则新建，JSONL 每行一个对象，写回要写全部行）。当你察觉用户当前话题与某个 open 状态 followup 的 topic 相关时，自然提一句，提醒后 reminded_count+1 写回；达 max_remind 或用户表示已处理/不感兴趣则 status 改 closed；超过30天未触发主动清理；用户要求不再提立即 closed。不要每轮都读 followups，只在话题可能相关时读。"
        )

    @staticmethod
    def _reminder_instructions() -> str:
        """定时任务的可靠创建规则；工具未注册时模型会自然忽略不可用能力。"""
        return (
            "\n\n## 主动提醒与定时任务\n"
            "当用户要求稍后提醒、周期提醒或到点执行 Agent 任务时，必须使用 "
            "create_reminder 工具持久化，不能只口头答应。查询与取消分别使用 "
            "list_reminders、cancel_reminder；若这些工具未出现在本轮可用工具中，"
            "应明确说明当前实例未启用提醒能力。\n"
            "创建前确认本地开始时间与 IANA 时区。‘每隔一天/每隔两天’等可能指不同"
            "间隔的说法必须先向用户确认；不得自行猜测。工具成功后把返回的未来最多"
            "三次执行时间清楚展示给用户。\n"
            "message 任务的 delivery_text 是到点直接发送的最终正文：创建时就按当前"
            "人设写成克制、准确、自包含的成稿，不要包含内部说明。agent 任务只保存"
            "明确、可独立执行的 agent_prompt，到点才运行 Agent。\n"
            "提醒只能投递到用户通过飞书私聊 /bind-reminders 显式绑定的目标；不得"
            "编造、索取或向工具传入 chat_id。未绑定时原样转达工具给出的绑定指引。"
        )

    def build_system_prompt(self) -> str:
        """拼接完整 System Prompt。

        顺序：人设 → 工作区 → 用户信息(USER,可选) → 长期记忆(MEMORY,可选)
              → 记忆指引 → 可用技能（可选）→ 子 Agent 摘要 → 当前时间（末尾）。

        说明：当前时间刻意放到最后，使前面的人设/工作区/记忆/技能等
        固定内容始终处于前缀位置，避免每次时间变化导致整段前缀无法命中
        Prompt Cache（如 Anthropic/OpenAI 的缓存机制），降低命中率与增加延迟。
        """
        identity = self._load_identity()
        user = self._load_user()
        memory = self._load_memory()
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")

        sections = [
            "【人设】\n" + identity,
            f"【工作区】\n{self.workspace}",
        ]

        # USER（用户长期信息）与 MEMORY（工作记忆）并列注入；为空则跳过
        if user:
            sections.append("【用户信息】\n" + user)
        if memory:
            sections.append("【长期记忆】\n" + memory)
        sections.append(self._memory_instructions())  # 记忆更新指引
        sections.append(self._reminder_instructions())
        prompt = "\n\n".join(sections)

        # 若注入了技能摘要，追加「可用技能」段
        if self.skills_summary:
            prompt += "\n\n## 可用技能\n" + self.skills_summary

        prompt += (
            "\n\n## 子 Agent 派遣\n"
            "复杂通用任务可以直接调用 spawn_subagent(task)，无需指定 agent_name。\n"
            "只有明确需要某个场景能力时，才调用 "
            "spawn_subagent(agent_name=..., task=...)。"
        )
        agents_summary = self.agents_summary
        if self.agents_summary_provider is not None:
            try:
                agents_summary = self.agents_summary_provider()
            except Exception:  # noqa: BLE001 - Profile 损坏不应阻断主对话
                pass
        if agents_summary:
            prompt += "\n\n## 可派遣的场景 Agent\n" + agents_summary

        # 当前时间放到最后：避免每次时间变化导致前缀（人设/工作区/记忆/技能）无法命中缓存
        prompt += f"\n\n【当前时间】\n{now}"

        return prompt

    def build_messages(
        self,
        history: Optional[List[dict]] = None,
        current_message: str = "",
    ) -> List[dict]:
        """构建完整 messages 列表。

        结构：``[System Prompt] + 历史对话 + 当前用户消息``。

        参数：
            history: 历史对话列表，每个元素形如
                ``{"role": "user"/"assistant", "content": "..."}``。
            current_message: 当前轮的用户输入；为空则不含用户消息（例如仅想
                重新组织系统提示时）。
        """
        messages: List[dict] = [
            {"role": "system", "content": self.build_system_prompt()}
        ]

        if history:
            messages.extend(history)

        if current_message:
            messages.append({"role": "user", "content": current_message})

        return messages
