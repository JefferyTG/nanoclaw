"""上下文构建器（ContextBuilder）。

负责把「系统人设 + 当前时间 + 工作区 + 长期记忆」拼装成给模型的 System Prompt，
并进一步组合出完整的 messages 列表（System + 历史 + 当前输入）。

这是 Agent 与模型对话前的「上下文装配中心」：把散落在文件（identity.md、
memory/MEMORY.md）与时间里的信息，统一收敛成模型能消费的文本。

说明：
- 人设文件默认位于 ``<workspace>/identity.md``，可由构造参数覆盖文件名。
- 长期记忆默认位于 ``<workspace>/memory/MEMORY.md``（第六章预留接口），
  读取失败或文件不存在时返回空字符串，不影响主流程。
"""


import os
from datetime import datetime
from typing import List, Optional

# 文件缺失时使用的默认人设
_DEFAULT_IDENTITY = (
    "你是一个务实、可靠、乐于助人的 AI 助手。\n"
    "你会优先用已有信息解决问题，遇到不确定会主动澄清，而不是猜测。\n"
    "你使用中文与用户交流，回答简洁、准确、可执行。"
)


class ContextBuilder:
    """构建 Agent 所需的 System Prompt 与完整 messages。"""

    def __init__(
        self,
        workspace: str,
        identity_file: str = "identity.md",
        skills_summary: str = "",
    ):
        self.workspace = os.path.abspath(workspace)
        self.identity_file = identity_file
        self.skills_summary = skills_summary
        # 注意：MEMORY.md 路径按第六章约定放在 <workspace>/memory/ 下
        self.memory_path = os.path.join(self.workspace, "workspace", "memory", "MEMORY.md")

    def _load_identity(self) -> str:
        """读取人设文件内容；文件不存在时返回默认人设。"""
        path = os.path.join(self.workspace, self.identity_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return _DEFAULT_IDENTITY
        except Exception as exc:  # noqa: BLE001 - 读取异常时降级为默认人设
            return _DEFAULT_IDENTITY + f"\n（人设文件读取失败：{exc}）"

    def _load_memory(self) -> str:
        """读取长期记忆（MEMORY.md）；不存在或读取失败返回空字符串。"""
        try:
            with open(self.memory_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except Exception:  # noqa: BLE001 - 记忆缺失不应阻断对话
            return ""

    def _memory_instructions(self) -> str:
        return """
            \n\n## 记忆管理指引
            当你在对话中发现以下类型的重要信息时，使用 write_file 工具更新 工作目录下的workspace/memory/MEMORY.md：
            - 用户的姓名、职业、技术偏好
            - 用户的项目信息和工作习惯
            - 用户明确要求你记住的事情
            - 用户纠正过你的错误（避免下次再犯）

            更新时读取现有内容，在末尾追加新条目，保持 Markdown 列表格式。
            不要记录琐碎的对话细节，只记录长期有价值的信息。
        """

    def build_system_prompt(self) -> str:
        """拼接完整 System Prompt。

        顺序：人设 → 工作区 → 长期记忆（可选）→ 记忆指引 → 可用技能（可选）
              → 当前时间（末尾）。

        说明：当前时间刻意放到最后，使前面的人设/工作区/记忆/技能等
        固定内容始终处于前缀位置，避免每次时间变化导致整段前缀无法命中
        Prompt Cache（如 Anthropic/OpenAI 的缓存机制），降低命中率与增加延迟。
        """
        identity = self._load_identity()
        memory = self._load_memory()
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")

        sections = [
            "【人设】\n" + identity,
            f"【工作区】\n{self.workspace}",
        ]

        if memory:
            sections.append("【长期记忆】\n" + memory)
        sections.append(self._memory_instructions())  # 记忆更新指引
        prompt = "\n\n".join(sections)

        # 若注入了技能摘要，追加「可用技能」段
        if self.skills_summary:
            prompt += "\n\n## 可用技能\n" + self.skills_summary

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
