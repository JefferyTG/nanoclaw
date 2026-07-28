"""Private Skill assets for reusable scene agents.

Assets are deliberately kept below ``<workspace>/workspace/agents``.  The
``SceneSkillLoader`` view mirrors the read-only API of :class:`SkillsLoader`,
so a scene-agent integration can combine it with the existing Skill tools
without changing the shared loader.
"""

from __future__ import annotations

import os
import json
import re
import tempfile
from collections.abc import Mapping
from typing import Any, Optional

import yaml


MAX_SKILL_TEXT_BYTES = 256 * 1024
"""Maximum UTF-8 size of one private ``SKILL.md``."""

MAX_RESOURCE_BYTES = 1024 * 1024
"""Maximum size returned by ``read_resource``."""

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_TOOL_MANIFEST_BYTES = 128 * 1024
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "token",
        "secret",
        "password",
        "authorization",
        "access_token",
        "refresh_token",
        "credential",
        "credentials",
        "key",
        "auth",
        "bearer",
        "private_key",
        "bearer_token",
    }
)


class SceneSkillAssetError(ValueError):
    """An invalid private Skill request or unsafe asset path."""


class SceneSkillAssets:
    """Manage one workspace's per-scene-Agent private Skill directories.

    ``workspace`` is the value of ``config.workspace``.  This object never
    reads profiles or global skills; the caller supplies the Agent name for
    each operation.
    """

    def __init__(self, workspace: str) -> None:
        if not isinstance(workspace, str) or not workspace.strip():
            raise SceneSkillAssetError("workspace 必须是非空路径。")
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        self.agents_dir = os.path.realpath(
            os.path.join(self.workspace, "workspace", "agents")
        )

    @staticmethod
    def validate_name(value: Any, kind: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SceneSkillAssetError(f"{kind} 名称不能为空。")
        name = value.strip()
        if _NAME_RE.fullmatch(name) is None:
            raise SceneSkillAssetError(
                f"{kind} 名称只能包含字母、数字、下划线和短横线。"
            )
        return name

    def _inside_agents(self, path: str) -> bool:
        try:
            return os.path.commonpath((self.agents_dir, path)) == self.agents_dir
        except ValueError:
            return False

    def _checked_path(self, *parts: str) -> str:
        path = os.path.realpath(os.path.join(self.agents_dir, *parts))
        if not self._inside_agents(path):
            raise SceneSkillAssetError("私有 Skill 路径越过 workspace/agents 边界。")
        return path

    @staticmethod
    def _reject_symlink(path: str) -> None:
        if os.path.lexists(path) and os.path.islink(path):
            raise SceneSkillAssetError("私有 Skill 路径不能使用符号链接。")

    def _agent_dir(self, agent_name: Any) -> tuple[str, str]:
        name = self.validate_name(agent_name, "Agent")
        raw = os.path.join(self.agents_dir, name)
        self._reject_symlink(raw)
        return name, self._checked_path(name)

    def _skills_dir(self, agent_name: Any) -> tuple[str, str]:
        name, agent_dir = self._agent_dir(agent_name)
        raw = os.path.join(agent_dir, "skills")
        self._reject_symlink(raw)
        return name, self._checked_path(name, "skills")

    def _skill_dir(self, agent_name: Any, skill_name: Any) -> tuple[str, str]:
        name, skills_dir = self._skills_dir(agent_name)
        skill = self.validate_name(skill_name, "Skill")
        raw = os.path.join(skills_dir, skill)
        self._reject_symlink(raw)
        return name, self._checked_path(name, "skills", skill)

    def _skill_file(self, agent_name: Any, skill_name: Any) -> tuple[str, str]:
        _, skill_dir = self._skill_dir(agent_name, skill_name)
        raw = os.path.join(skill_dir, "SKILL.md")
        self._reject_symlink(raw)
        return skill_dir, self._checked_path(
            self.validate_name(agent_name, "Agent"),
            "skills",
            self.validate_name(skill_name, "Skill"),
            "SKILL.md",
        )

    @staticmethod
    def _check_text(content: Any) -> bytes:
        if not isinstance(content, str):
            raise SceneSkillAssetError("Skill 内容必须是文本。")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_SKILL_TEXT_BYTES:
            raise SceneSkillAssetError("Skill 内容超过大小限制。")
        return encoded

    @staticmethod
    def _read_bytes(path: str, max_bytes: int, label: str) -> bytes:
        try:
            if not os.path.isfile(path):
                raise SceneSkillAssetError(f"{label} 不存在。")
            if os.path.getsize(path) > max_bytes:
                raise SceneSkillAssetError(f"{label} 超过大小限制。")
            with open(path, "rb") as file:
                data = file.read(max_bytes + 1)
        except OSError as exc:
            raise SceneSkillAssetError(f"无法读取{label}：{exc}") from exc
        if len(data) > max_bytes:
            raise SceneSkillAssetError(f"{label} 超过大小限制。")
        return data

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        if not content.startswith("---\n"):
            return {}, content
        end = content.find("---", 4)
        if end == -1:
            return {}, content
        try:
            metadata = yaml.safe_load(content[4:end]) or {}
        except yaml.YAMLError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return metadata, content[end + 3 :].strip()

    def list_skills(self, agent_name: Any) -> list[dict[str, str]]:
        """List valid private Skills, ordered by their directory names."""
        _, skills_dir = self._skills_dir(agent_name)
        if not os.path.isdir(skills_dir):
            return []
        skills: list[dict[str, str]] = []
        for entry in sorted(os.listdir(skills_dir)):
            try:
                _, path = self._skill_file(agent_name, entry)
            except SceneSkillAssetError:
                # An unexpected entry is not a Skill; a symlink is unsafe.
                if os.path.islink(os.path.join(skills_dir, entry)):
                    raise
                continue
            if not os.path.isfile(path):
                continue
            if self.load_skill(agent_name, entry) is None:
                continue
            raw = self._read_bytes(path, MAX_SKILL_TEXT_BYTES, "SKILL.md")
            try:
                full_text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SceneSkillAssetError("SKILL.md 不是 UTF-8 文本。") from exc
            metadata, _ = self._parse_frontmatter(full_text)
            name = metadata.get("name")
            if not isinstance(name, str) or not name.strip():
                name = entry
            elif name.strip() != entry:
                raise SceneSkillAssetError(
                    f"私有 Skill frontmatter name '{name.strip()}' 必须与目录名 '{entry}' 一致。"
                )
            description = metadata.get("description", "")
            if not isinstance(description, str):
                description = ""
            skills.append(
                {"name": name.strip(), "description": description, "path": path}
            )
        return skills

    def load_skill(self, agent_name: Any, skill_name: Any) -> Optional[str]:
        """Read a Skill body (without YAML frontmatter), or return ``None``."""
        try:
            _, path = self._skill_file(agent_name, skill_name)
        except SceneSkillAssetError:
            raise
        if not os.path.isfile(path):
            return None
        raw = self._read_bytes(path, MAX_SKILL_TEXT_BYTES, "SKILL.md")
        try:
            _, body = self._parse_frontmatter(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise SceneSkillAssetError("SKILL.md 不是 UTF-8 文本。") from exc
        return body

    def create_skill(self, agent_name: Any, skill_name: Any, content: Any) -> str:
        """Exclusively create one private Skill; never overwrite an existing one."""
        data = self._check_text(content)
        agent, agent_dir = self._agent_dir(agent_name)
        skill = self.validate_name(skill_name, "Skill")
        os.makedirs(self.agents_dir, exist_ok=True)
        self._reject_symlink(agent_dir)
        os.makedirs(agent_dir, exist_ok=True)
        _, skills_dir = self._skills_dir(agent)
        os.makedirs(skills_dir, exist_ok=True)
        skill_dir = self._checked_path(agent, "skills", skill)
        self._reject_symlink(skill_dir)
        try:
            os.mkdir(skill_dir)
        except FileExistsError as exc:
            raise SceneSkillAssetError(f"私有 Skill '{skill}' 已存在，不能重复创建。") from exc
        path = self._checked_path(agent, "skills", skill, "SKILL.md")
        fd, temp_path = tempfile.mkstemp(prefix=".skill-", suffix=".tmp", dir=skill_dir)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            try:
                os.rmdir(skill_dir)
            except OSError:
                pass
            raise
        return path

    def update_skill(self, agent_name: Any, skill_name: Any, content: Any) -> str:
        """Atomically replace an existing private ``SKILL.md``."""
        data = self._check_text(content)
        skill_dir, path = self._skill_file(agent_name, skill_name)
        if not os.path.isfile(path):
            raise SceneSkillAssetError("私有 Skill 不存在，不能更新。")
        fd, temp_path = tempfile.mkstemp(prefix=".skill-", suffix=".tmp", dir=skill_dir)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return path

    def read_resource(
        self, agent_name: Any, skill_name: Any, file_path: Any, *, binary: bool = False
    ) -> str | bytes:
        """Read a bounded file below one private Skill directory.

        ``file_path`` is a relative path such as ``templates/post.md``.  It
        cannot name ``SKILL.md`` through a symlink or escape the Skill folder.
        """
        if not isinstance(file_path, str) or not file_path.strip():
            raise SceneSkillAssetError("file_path 必须是非空相对路径。")
        relative = file_path.strip()
        if os.path.isabs(relative) or ".." in relative.replace("\\", "/").split("/"):
            raise SceneSkillAssetError("file_path 必须是相对路径。")
        skill_dir, _ = self._skill_file(agent_name, skill_name)
        raw = os.path.join(skill_dir, relative)
        resolved = os.path.realpath(raw)
        try:
            inside = os.path.commonpath((skill_dir, resolved)) == skill_dir
        except ValueError:
            inside = False
        if not inside:
            raise SceneSkillAssetError("资源路径越过私有 Skill 边界。")
        self._reject_symlink(raw)
        data = self._read_bytes(resolved, MAX_RESOURCE_BYTES, "资源文件")
        if binary:
            return data
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SceneSkillAssetError("资源文件不是 UTF-8 文本；请使用 binary=True。") from exc

    def for_agent(
        self, agent_name: Any, allowed_names: Optional[list[str]] = None
    ) -> "SceneSkillLoader":
        """Return a SkillsLoader-compatible read view for one scene Agent."""
        return SceneSkillLoader(
            self, self.validate_name(agent_name, "Agent"), allowed_names=allowed_names
        )


class SceneSkillLoader:
    """Read-only per-Agent view compatible with existing Skill query tools."""

    _SUMMARY_HEADER = (
        "你有以下技能可用。当你需要使用某项技能时，\n"
        "请先用 load_skill 工具读取对应的详细指南。\n\n可用技能：\n"
    )

    def __init__(
        self,
        assets: SceneSkillAssets,
        agent_name: str,
        allowed_names: Optional[list[str]] = None,
    ) -> None:
        self.assets = assets
        self.agent_name = assets.validate_name(agent_name, "Agent")
        self._allowed_names = (
            None
            if allowed_names is None
            else frozenset(
                assets.validate_name(name, "Skill") for name in allowed_names
            )
        )

    def list_skills(self) -> list[dict[str, str]]:
        skills = self.assets.list_skills(self.agent_name)
        if self._allowed_names is None:
            return skills
        return [skill for skill in skills if skill["name"] in self._allowed_names]

    def load_skill(self, name: str) -> Optional[str]:
        if not isinstance(name, str):
            return None
        requested = name.strip()
        for skill in self.list_skills():
            if requested == skill["name"] or requested == os.path.basename(
                os.path.dirname(skill["path"])
            ):
                return self.assets.load_skill(self.agent_name, os.path.basename(os.path.dirname(skill["path"])))
        return None

    def build_skills_summary(self) -> str:
        lines = [
            f"- {skill['name']} ({skill['path']})：{skill['description']}"
            for skill in self.list_skills()
        ]
        return self._SUMMARY_HEADER + "\n".join(lines) + "\n" if lines else ""

    def load_skill_resource(self, name: str, resource_path: str) -> Optional[str]:
        if not isinstance(name, str):
            return None
        requested = name.strip()
        if requested not in {skill["name"] for skill in self.list_skills()}:
            return None
        try:
            content = self.assets.read_resource(
                self.agent_name, requested, resource_path, binary=False
            )
        except SceneSkillAssetError:
            return None
        return content if isinstance(content, str) else None


class SceneToolAssets:
    """Store data-only private-tool manifests below one scene Agent directory."""

    def __init__(self, workspace: str) -> None:
        self._paths = SceneSkillAssets(workspace)
        self.workspace = self._paths.workspace
        self.agents_dir = self._paths.agents_dir

    def _tools_dir(self, agent_name: Any) -> tuple[str, str]:
        agent, agent_dir = self._paths._agent_dir(agent_name)
        raw = os.path.join(agent_dir, "tools")
        self._paths._reject_symlink(raw)
        return agent, self._paths._checked_path(agent, "tools")

    def _manifest_path(self, agent_name: Any, tool_name: Any) -> tuple[str, str]:
        agent, tools_dir = self._tools_dir(agent_name)
        tool = self._paths.validate_name(tool_name, "Tool")
        raw = os.path.join(tools_dir, f"{tool}.json")
        self._paths._reject_symlink(raw)
        return tools_dir, self._paths._checked_path(agent, "tools", f"{tool}.json")

    @classmethod
    def _validate_secret_refs(cls, value: Any, path: str = "config") -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    raise SceneSkillAssetError("私有工具 config 的键必须是字符串。")
                key = raw_key.lower().replace("-", "_")
                if key.endswith("_env"):
                    if not isinstance(item, str) or _ENV_NAME_RE.fullmatch(item) is None:
                        raise SceneSkillAssetError(
                            f"私有工具 {path}.{raw_key} 必须引用合法环境变量名。"
                        )
                    cls._validate_secret_refs(item, f"{path}.{raw_key}")
                    continue
                is_secret_field = key in _SECRET_KEYS or key.endswith(
                    (
                        "_key",
                        "_token",
                        "_secret",
                        "_password",
                        "_auth",
                        "_authorization",
                        "_credential",
                        "_credentials",
                    )
                )
                if is_secret_field:
                    raise SceneSkillAssetError(
                        f"私有工具 {path}.{raw_key} 不得保存密钥值；请改用 {raw_key}_env。"
                    )
                cls._validate_secret_refs(item, f"{path}.{raw_key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                cls._validate_secret_refs(item, f"{path}[{index}]")

    def validate_manifest(self, manifest: Any, expected_name: str | None = None) -> dict:
        if not isinstance(manifest, Mapping):
            raise SceneSkillAssetError("私有工具 manifest 必须是 JSON 对象。")
        missing = [key for key in ("name", "factory", "config") if key not in manifest]
        if missing:
            raise SceneSkillAssetError("私有工具 manifest 缺少字段：" + ", ".join(missing))
        name = self._paths.validate_name(manifest["name"], "Tool")
        factory = self._paths.validate_name(manifest["factory"], "Factory")
        if expected_name is not None and name != expected_name:
            raise SceneSkillAssetError("私有工具 manifest name 必须与文件名一致。")
        config = manifest["config"]
        if not isinstance(config, Mapping):
            raise SceneSkillAssetError("私有工具 config 必须是 JSON 对象。")
        self._validate_secret_refs(config)
        normalized = {"name": name, "factory": factory, "config": dict(config)}
        try:
            encoded = json.dumps(normalized, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SceneSkillAssetError("私有工具 config 必须是可序列化 JSON 数据。") from exc
        if len(encoded) > MAX_TOOL_MANIFEST_BYTES:
            raise SceneSkillAssetError("私有工具 manifest 超过大小限制。")
        return normalized

    def load_tool(self, agent_name: Any, tool_name: Any) -> Optional[dict]:
        expected = self._paths.validate_name(tool_name, "Tool")
        _, path = self._manifest_path(agent_name, expected)
        if not os.path.isfile(path):
            return None
        raw = self._paths._read_bytes(path, MAX_TOOL_MANIFEST_BYTES, "私有工具 manifest")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SceneSkillAssetError("私有工具 manifest 不是有效 UTF-8 JSON。") from exc
        return self.validate_manifest(data, expected_name=expected)

    def list_tools(self, agent_name: Any) -> list[dict]:
        _, tools_dir = self._tools_dir(agent_name)
        if not os.path.isdir(tools_dir):
            return []
        manifests: list[dict] = []
        for entry in sorted(os.listdir(tools_dir)):
            if not entry.endswith(".json"):
                continue
            tool_name = entry[:-5]
            manifest = self.load_tool(agent_name, tool_name)
            if manifest is not None:
                manifests.append(manifest)
        return manifests

    def _write(self, agent_name: Any, manifest: Any, *, replace: bool) -> str:
        normalized = self.validate_manifest(manifest)
        tools_dir, path = self._manifest_path(agent_name, normalized["name"])
        if replace and not os.path.isfile(path):
            raise SceneSkillAssetError("私有工具不存在，不能更新。")
        if not replace and os.path.lexists(path):
            raise SceneSkillAssetError(
                f"私有工具 '{normalized['name']}' 已存在，不能重复创建。"
            )
        os.makedirs(tools_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".tool-", suffix=".tmp", dir=tools_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(normalized, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            if replace:
                os.replace(temp_path, path)
            else:
                try:
                    os.link(temp_path, path)
                except FileExistsError as exc:
                    raise SceneSkillAssetError(
                        f"私有工具 '{normalized['name']}' 已存在，不能重复创建。"
                    ) from exc
                os.unlink(temp_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return path

    def create_tool(self, agent_name: Any, manifest: Any) -> str:
        return self._write(agent_name, manifest, replace=False)

    def update_tool(self, agent_name: Any, manifest: Any) -> str:
        return self._write(agent_name, manifest, replace=True)
