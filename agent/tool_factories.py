"""Controlled construction of private tools for scene-agent dispatches.

Private-tool manifests are data, not executable extensions.  Factories must be
registered by reviewed application code; this module never imports Python from
a workspace and never provides shell execution.  A factory receives a validated
``name`` and configuration mapping and must return a fresh :class:`Tool`.

Configuration should contain references such as ``token_env`` or ``api_key_env``
rather than secret values.  Resolve ``*_env`` references at the trusted
composition boundary, and do not write or echo their values in manifests,
exceptions, logs, or tool results.
"""

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from agent.tools.base import Tool


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ToolFactoryError(ValueError):
    """Base error for private-tool factory registration and construction."""


class PrivateToolManifestError(ToolFactoryError):
    """A private-tool manifest has an invalid shape or value."""


class UnknownToolFactoryError(ToolFactoryError):
    """A manifest refers to a factory that trusted code did not register."""


ToolFactory = Callable[[str, Mapping[str, Any]], Tool]
ConfigValidator = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


@dataclass(frozen=True)
class _RegisteredFactory:
    create: ToolFactory
    validate_config: ConfigValidator


class ToolFactoryRegistry:
    """Registry of reviewed factories for isolated private :class:`Tool` objects.

    Each ``build`` call invokes the registered factory anew.  Therefore a
    dispatch never shares a mutable private-tool instance with another dispatch.
    Factories are deliberately registered in Python code; manifests cannot name
    Python modules, filesystem paths, shell commands, URLs, or arbitrary callables.
    """

    def __init__(self) -> None:
        self._factories: dict[str, _RegisteredFactory] = {}

    @staticmethod
    def _validate_name(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise PrivateToolManifestError(f"私有工具 {field} 必须是非空字符串。")
        name = value.strip()
        if _TOOL_NAME_RE.fullmatch(name) is None:
            raise PrivateToolManifestError(
                f"私有工具 {field} 只能包含字母、数字、下划线和短横线。"
            )
        return name

    def register(
        self,
        name: str,
        factory: ToolFactory,
        *,
        config_validator: ConfigValidator,
    ) -> None:
        """Register one code-reviewed factory.

        Duplicate names are rejected rather than silently replacing a reviewed
        factory.  Validators may return a normalized mapping or ``None`` to
        retain the supplied configuration; validators must not expose secrets in
        their exceptions because exception details are intentionally suppressed.
        """
        factory_name = self._validate_name(name, "factory")
        if not callable(factory):
            raise ToolFactoryError("私有工具 factory 必须可调用。")
        if not callable(config_validator):
            raise ToolFactoryError("私有工具 config_validator 必须可调用。")
        if factory_name in self._factories:
            raise ToolFactoryError(f"私有工具 factory '{factory_name}' 已注册。")
        self._factories[factory_name] = _RegisteredFactory(factory, config_validator)

    def list_factories(self) -> list[str]:
        """Return reviewed factory names in registration order."""
        return list(self._factories)

    def validate_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize a manifest without constructing a Tool."""
        if not isinstance(manifest, Mapping):
            raise PrivateToolManifestError("私有工具 manifest 必须是对象。")
        missing = [key for key in ("name", "factory", "config") if key not in manifest]
        if missing:
            raise PrivateToolManifestError(
                "私有工具 manifest 缺少字段：" + ", ".join(missing)
            )

        tool_name = self._validate_name(manifest["name"], "name")
        factory_name = self._validate_name(manifest["factory"], "factory")
        config = manifest["config"]
        if not isinstance(config, Mapping):
            raise PrivateToolManifestError(f"私有工具 '{tool_name}' 的 config 必须是对象。")

        registered = self._factories.get(factory_name)
        if registered is None:
            raise UnknownToolFactoryError(
                f"私有工具 '{tool_name}' 引用了未注册的 factory '{factory_name}'。"
            )

        try:
            validated_config: Mapping[str, Any] = deepcopy(dict(config))
        except Exception as exc:  # noqa: BLE001 - configuration must be data-only
            raise PrivateToolManifestError(
                f"私有工具 '{tool_name}' 的 config 必须是可复制的数据对象。"
            ) from exc
        try:
            result = registered.validate_config(validated_config)
        except Exception as exc:  # noqa: BLE001 - do not expose configuration values
            raise PrivateToolManifestError(
                f"私有工具 '{tool_name}' 的 config 校验失败。"
            ) from exc
        if result is not None:
            if not isinstance(result, Mapping):
                raise PrivateToolManifestError(
                    f"私有工具 '{tool_name}' 的 config 校验器必须返回对象或 None。"
                )
            try:
                validated_config = deepcopy(dict(result))
            except Exception as exc:  # noqa: BLE001 - configuration must be data-only
                raise PrivateToolManifestError(
                    f"私有工具 '{tool_name}' 的 config 必须是可复制的数据对象。"
                ) from exc

        return {
            "name": tool_name,
            "factory": factory_name,
            "config": deepcopy(dict(validated_config)),
        }

    def build(self, manifest: Mapping[str, Any]) -> Tool:
        """Construct one fresh private tool from a validated data-only manifest.

        Invalid configuration and factory errors never include configuration
        content, so secret values cannot be echoed accidentally.
        """
        normalized = self.validate_manifest(manifest)
        tool_name = normalized["name"]
        factory_name = normalized["factory"]
        validated_config = normalized["config"]
        registered = self._factories[factory_name]

        try:
            tool = registered.create(tool_name, deepcopy(dict(validated_config)))
        except Exception as exc:  # noqa: BLE001 - factory details may include secrets
            raise ToolFactoryError(f"私有工具 '{tool_name}' 构造失败。") from exc
        if not isinstance(tool, Tool):
            raise ToolFactoryError(f"私有工具 '{tool_name}' 的 factory 未返回 Tool。")
        if tool.name != tool_name:
            raise ToolFactoryError(
                f"私有工具 '{tool_name}' 的 factory 返回了名称不匹配的 Tool。"
            )
        return tool

    def build_many(self, manifests: Sequence[Mapping[str, Any]]) -> list[Tool]:
        """Build fresh tools for one dispatch from ordered manifest entries."""
        if isinstance(manifests, (str, bytes)) or not isinstance(manifests, Sequence):
            raise PrivateToolManifestError("私有工具 manifests 必须是列表。")
        tools: list[Tool] = []
        names: set[str] = set()
        for manifest in manifests:
            tool = self.build(manifest)
            if tool.name in names:
                raise PrivateToolManifestError(f"私有工具名称重复：'{tool.name}'。")
            names.add(tool.name)
            tools.append(tool)
        return tools
