"""Persistent profiles for reusable, ephemeral scene agents."""

from dataclasses import asdict, dataclass
import json
import os
import re
import tempfile
from typing import Any, Mapping, Optional


_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class AgentProfileError(ValueError):
    """Profile input or storage is invalid."""


@dataclass(frozen=True)
class AgentProfile:
    """Validated scene-agent configuration.

    ``tools`` and ``skills`` retain the v1 meaning: references to capabilities
    owned by the main application.  ``private_*`` names resolve only below the
    owning scene agent's asset directory.
    """

    name: str
    description: str
    system_prompt: str
    model: str
    tools: list[str]
    skills: list[str]
    private_tools: list[str]
    private_skills: list[str]
    version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentProfileLoader:
    """Validate and store profiles below one fixed agents directory.

    Profiles use ``<agents>/<name>/profile.json`` so private assets can be
    colocated below the same directory.  Legacy ``<agents>/<name>.json`` files
    are intentionally ignored.
    """

    def __init__(self, profiles_dir: str) -> None:
        self.profiles_dir = os.path.realpath(os.path.abspath(profiles_dir))

    @staticmethod
    def validate_name(name: Any) -> str:
        if not isinstance(name, str) or not name.strip():
            raise AgentProfileError("Agent 名称不能为空。")
        normalized = name.strip()
        if _AGENT_NAME_RE.fullmatch(normalized) is None:
            raise AgentProfileError("Agent 名称只能包含字母、数字、下划线和短横线。")
        return normalized

    def _safe_path(self, *parts: str) -> str:
        path = os.path.realpath(os.path.join(self.profiles_dir, *parts))
        try:
            inside = os.path.commonpath((self.profiles_dir, path)) == self.profiles_dir
        except ValueError:
            inside = False
        if not inside:
            raise AgentProfileError("Agent 资产路径越过 workspace/agents 边界。")
        return path

    def agent_dir(self, name: str) -> str:
        """Return the validated v2 asset directory for one scene agent."""
        return self._safe_path(self.validate_name(name))

    def private_skills_dir(self, name: str) -> str:
        return self._safe_path(self.validate_name(name), "skills")

    def private_tools_dir(self, name: str) -> str:
        return self._safe_path(self.validate_name(name), "tools")

    def _v2_profile_path(self, name: str) -> str:
        return self._safe_path(self.validate_name(name), "profile.json")

    @staticmethod
    def _string_list(
        data: Mapping[str, Any], field: str, *, required: bool = True
    ) -> list[str]:
        if field not in data and not required:
            return []
        if field not in data or not isinstance(data[field], list):
            raise AgentProfileError(f"{field} 必须是字符串列表。")
        values: list[str] = []
        for item in data[field]:
            if not isinstance(item, str) or not item.strip():
                raise AgentProfileError(f"{field} 必须是非空字符串列表。")
            value = item.strip()
            if value not in values:
                values.append(value)
        return values

    def _validate(self, data: Mapping[str, Any]) -> AgentProfile:
        if not isinstance(data, Mapping):
            raise AgentProfileError("Agent Profile 必须是 JSON 对象。")

        name = self.validate_name(data.get("name"))
        text_fields: dict[str, str] = {}
        for field in ("description", "system_prompt"):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AgentProfileError(f"{field} 不能为空。")
            text_fields[field] = value.strip()

        model_value = data.get("model", "")
        if model_value is None:
            model_value = ""
        if not isinstance(model_value, str):
            raise AgentProfileError("model 必须是字符串或 null。")

        version = data.get("version", 2)
        if version != 2:
            raise AgentProfileError("当前仅支持 version=2 的目录式 Agent Profile。")

        return AgentProfile(
            name=name,
            description=text_fields["description"],
            system_prompt=text_fields["system_prompt"],
            model=model_value.strip(),
            tools=self._string_list(data, "tools"),
            skills=self._string_list(data, "skills"),
            private_tools=self._string_list(data, "private_tools", required=False),
            private_skills=self._string_list(data, "private_skills", required=False),
            version=version,
        )

    def list_profiles(self) -> list[AgentProfile]:
        """Return valid profiles sorted by name; bad files do not break startup."""
        if not os.path.isdir(self.profiles_dir):
            return []

        names: set[str] = set()
        for entry in os.listdir(self.profiles_dir):
            if os.path.isdir(os.path.join(self.profiles_dir, entry)):
                names.add(entry)

        profiles: list[AgentProfile] = []
        for name in sorted(names):
            try:
                profile = self.get_profile(name)
            except (
                AgentProfileError,
                OSError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ):
                continue
            if profile is not None:
                profiles.append(profile)
        return profiles

    def get_profile(self, name: str) -> Optional[AgentProfile]:
        """Load one profile, returning ``None`` when it does not exist."""
        normalized = self.validate_name(name)
        path = self._v2_profile_path(normalized)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        profile = self._validate(data)
        if profile.name != normalized:
            raise AgentProfileError("Profile 内的 name 与文件名/目录名不一致。")
        return profile

    def create_profile(self, data: Mapping[str, Any]) -> AgentProfile:
        """Validate and exclusively create a v2 profile without overwriting."""
        profile = self._validate({**data, "version": 2})
        os.makedirs(self.profiles_dir, exist_ok=True)
        agent_dir = self.agent_dir(profile.name)
        try:
            os.mkdir(agent_dir)
        except FileExistsError as exc:
            raise AgentProfileError(f"Agent '{profile.name}' 已存在，不能重复创建。") from exc
        path = self._v2_profile_path(profile.name)
        try:
            with open(path, "x", encoding="utf-8") as file:
                json.dump(profile.to_dict(), file, ensure_ascii=False, indent=2)
                file.write("\n")
        except Exception:
            try:
                os.rmdir(agent_dir)
            except OSError:
                pass
            raise
        return profile

    def update_profile(self, name: str, updates: Mapping[str, Any]) -> AgentProfile:
        """Validate and atomically update an existing profile."""
        current = self.get_profile(name)
        if current is None:
            raise AgentProfileError(f"Agent '{name}' 不存在。")
        if "name" in updates and self.validate_name(updates["name"]) != current.name:
            raise AgentProfileError("不能通过 update_profile 重命名 Agent。")
        path = self._v2_profile_path(current.name)
        profile = self._validate(
            {
                **current.to_dict(),
                **dict(updates),
                "name": current.name,
                "version": 2,
            }
        )
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".profile-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(profile.to_dict(), file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return profile

    def build_summary(self) -> str:
        """Build the minimal name/description summary for the main Agent."""
        profiles = self.list_profiles()
        if not profiles:
            return ""
        return "\n".join(f"- {profile.name}：{profile.description}" for profile in profiles)
