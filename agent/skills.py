"""技能加载器：扫描技能目录、解析 SKILL.md、生成技能清单。

SkillsLoader 负责在运行时发现和读取 NanoClaw 的「技能」（skill）。
每个技能是一个子目录，目录内放一份 ``SKILL.md``，文件头部用 YAML
frontmatter 描述元信息（name / description），正文是详细操作指南。

典型用法：
    loader = SkillsLoader("skills")          # 默认技能目录
    summary = loader.build_skills_summary()  # 拼进 System Prompt
    guide   = loader.load_skill("web_fetcher")  # 需要时再读正文
"""

import os
import yaml

from typing import Dict, List, Optional, Tuple


class SkillsLoader:
    """技能目录扫描与 SKILL.md 解析器。"""

    # build_skills_summary 开头的引导语
    _SUMMARY_HEADER = (
        "你有以下技能可用。当你需要使用某项技能时，\n"
        "请先用 read_file 工具读取对应的 SKILL.md 文件获取详细指南。\n\n"
        "可用技能：\n"
    )

    def __init__(self, skills_dir: str = "skills") -> None:
        """初始化加载器。

        Args:
            skills_dir: 技能根目录路径，默认 "skills"。存为绝对路径，
                避免后续相对路径计算受调用方 cwd 变化影响。
        """
        self.skills_dir = os.path.abspath(skills_dir)

    def _parse_frontmatter(self, content: str) -> Tuple[Dict, str]:
        """解析 SKILL.md 的 YAML frontmatter。

        约定：文件以 ``---\\n`` 开头，第二处 ``---`` 之间为 YAML 元数据，
        其后是正文。若文件不含 frontmatter，则原样作为正文返回、元数据为空。

        Args:
            content: SKILL.md 的完整文本内容。

        Returns:
            (metadata, body) 二元组：
            - metadata：safe_load 解析出的字典（无 frontmatter 时为空字典）
            - body：去掉 frontmatter 之后的正文（前后空白已剥离）
        """
        if not content.startswith("---\n"):
            # 没有 frontmatter，整篇当作正文
            return {}, content

        # 找到第二处 "---" 作为 frontmatter 结束标记
        # 第一段 "---\n" 已在 startswith 中确认，从其后开始查找
        end_idx = content.find("---", 4)
        if end_idx == -1:
            # 只有开头一个 ---，格式不完整，退回原样
            return {}, content

        yaml_block = content[4:end_idx]
        # 跳过结束的 "---" 及其后的换行，取正文
        body = content[end_idx + 3:]

        try:
            metadata = yaml.safe_load(yaml_block) or {}
            if not isinstance(metadata, dict):
                # frontmatter 不是字典（如纯标量），降级处理
                metadata = {}
        except yaml.YAMLError:
            # YAML 解析失败，不阻断，正文照常返回
            metadata = {}

        return metadata, body.strip()

    def build_skills_summary(self) -> str:
        """生成技能目录摘要，用于注入 System Prompt。

        遍历 ``skills_dir`` 下每个子目录，读取其中 ``SKILL.md`` 的
        frontmatter，提取 name 与 description，按统一格式拼成清单。

        Returns:
            完整的技能清单字符串（含引导语）；若目录不存在或没有任何
            技能，返回空字符串。
        """
        if not os.path.isdir(self.skills_dir):
            return ""

        lines: List[str] = []
        for entry in sorted(os.listdir(self.skills_dir)):
            subdir = os.path.join(self.skills_dir, entry)
            if not os.path.isdir(subdir):
                continue

            skill_path = os.path.join(subdir, "SKILL.md")
            if not os.path.isfile(skill_path):
                continue

            try:
                with open(skill_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                # 读不了就跳过这个技能，不影响其它
                continue

            metadata, _ = self._parse_frontmatter(content)
            # 与 list_skills 保持一致：缺 name 时回退为目录名
            name = metadata.get("name") or entry
            description = metadata.get("description", "")

            # 相对路径：相对于当前工作目录，方便模型用 read_file 定位
            rel_path = os.path.relpath(skill_path, os.getcwd())
            lines.append(f"- {name} ({rel_path})：{description}")

        if not lines:
            return ""

        return self._SUMMARY_HEADER + "\n".join(lines) + "\n"

    def load_skill(self, name: str) -> Optional[str]:
        """读取指定技能的正文内容（去掉 frontmatter）。

        Args:
            name: 技能名，对应 ``skills_dir/<name>/SKILL.md``。

        Returns:
            技能正文（已去除 frontmatter）；找不到时返回 None。
        """
        skill_path = os.path.join(self.skills_dir, name, "SKILL.md")
        if not os.path.isfile(skill_path):
            return None

        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            return None

        _, body = self._parse_frontmatter(content)
        return body

    def list_skills(self) -> List[Dict]:
        """列出所有已发现的技能，供调试与管理使用。

        Returns:
            技能信息列表，每个元素为 ``{"name", "description", "path"}``。
            - name：技能名（取自 frontmatter，缺省回退为子目录名）
            - description：技能描述
            - path：SKILL.md 的绝对路径
        """
        if not os.path.isdir(self.skills_dir):
            return []

        skills: List[Dict] = []
        for entry in sorted(os.listdir(self.skills_dir)):
            subdir = os.path.join(self.skills_dir, entry)
            if not os.path.isdir(subdir):
                continue

            skill_path = os.path.join(subdir, "SKILL.md")
            if not os.path.isfile(skill_path):
                continue

            try:
                with open(skill_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            metadata, _ = self._parse_frontmatter(content)
            name = metadata.get("name") or entry
            description = metadata.get("description", "")

            skills.append(
                {
                    "name": name,
                    "description": description,
                    "path": skill_path,
                }
            )

        return skills
