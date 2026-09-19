"""Immutable exact-provider hooks consumed by the shared ACP family."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentLaunch:
    """A validated exact ACP agent invocation.

    Exact providers construct this inside their worker factory. The shared
    application receives only this data; it never imports the exact provider.
    """

    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    auth_method: str = ""

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise ValueError("ACP agent argv must contain a command")
        if not self.cwd.is_absolute():
            raise ValueError("ACP agent cwd must be absolute")
        if any(not isinstance(item, str) or "\x00" in item for item in self.argv):
            raise ValueError("ACP agent arguments must be strings without NUL")
        if any(not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value
               for key, value in self.environment.items()):
            raise ValueError("ACP agent environment must contain strings without NUL")

    def to_json(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "environment": dict(self.environment),
            "auth_method": self.auth_method,
        }


LaunchResolver = Callable[[Mapping[str, Any], Path], AgentLaunch]


@dataclass(frozen=True)
class ProviderSpec:
    """Static contract supplied by one exact ACP provider package."""

    plugin_id: str
    plugin_version: str
    resolve_launch: LaunchResolver

    def __post_init__(self) -> None:
        if len(self.plugin_id) < 3:
            raise ValueError("plugin_id must contain at least three characters")
        if not self.plugin_version:
            raise ValueError("plugin_version is required")
