"""Family-owned alias resolution for the frozen Jusi runtime snapshot."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AliasConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedAlias:
    name: str
    provider: str
    cwd: Path
    additional_directories: tuple[Path, ...]
    mcp_servers: tuple[dict[str, Any], ...]
    configuration: dict[str, Any]

    def handoff_payload(self, *, body: str, session_action: str, session_id: str) -> dict[str, Any]:
        return {
            "alias": self.name,
            "body": body,
            "cwd": str(self.cwd),
            "additional_directories": [str(path) for path in self.additional_directories],
            "mcp_servers": [dict(server) for server in self.mcp_servers],
            "configuration": dict(self.configuration),
            "session_action": session_action,
            "session_id": session_id,
        }


def resolve_alias(configuration: Mapping[str, Any], alias: str, available: set[str]) -> ResolvedAlias:
    family = configuration.get("acp")
    if not isinstance(family, Mapping):
        raise AliasConfigurationError("ACP aliases are not configured")
    raw = family.get(alias)
    if not isinstance(raw, Mapping):
        raise AliasConfigurationError(f"Unknown ACP alias {alias!r}")
    provider = str(raw.get("provider", "")).strip()
    if not provider:
        raise AliasConfigurationError(f"ACP alias {alias!r} requires a provider")
    if provider not in available:
        raise AliasConfigurationError(
            f"ACP alias {alias!r} selects unavailable provider {provider!r}"
        )

    raw_path = str(raw.get("path", "")).strip()
    if not raw_path:
        raise AliasConfigurationError(f"ACP alias {alias!r} requires a path")
    cwd = Path(raw_path).expanduser().resolve()

    raw_additional = raw.get("additional_directories", [])
    if not isinstance(raw_additional, list) or any(not isinstance(item, str) for item in raw_additional):
        raise AliasConfigurationError("additional_directories must be an array of paths")
    additional = tuple(_resolve_path(item, cwd) for item in raw_additional)

    raw_mcp = raw.get("mcp_servers", [])
    if not isinstance(raw_mcp, list) or any(not isinstance(item, Mapping) for item in raw_mcp):
        raise AliasConfigurationError("mcp_servers must be an array of tables")

    return ResolvedAlias(
        name=alias,
        provider=provider,
        cwd=cwd,
        additional_directories=additional,
        mcp_servers=tuple(dict(item) for item in raw_mcp),
        configuration=dict(raw),
    )


def _resolve_path(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    return (cwd / path).resolve() if not path.is_absolute() else path.resolve()
