"""Shared kernel dispatcher and catalog claim for exact ACP providers."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import copy
import shlex
from typing import Any

from IPython.core.error import UsageError

from .config import AliasConfigurationError, resolve_alias

FAMILY_ID = "acp"
MAGIC_NAME = "acp"
CAPABILITIES = ("execute", "followup", "complete", "interrupt", "editor_actions")
PRESENTATION = {"syntax": "markdown", "indent": "markdown"}
HANDOFF_MIME = "application/vnd.jusi.handoff.v1+json"


def family_claim(*, provider_presentation: Mapping[str, str] | None = None) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "family_id": FAMILY_ID,
        "magic_name": MAGIC_NAME,
        "capabilities": list(CAPABILITIES),
        "presentation": dict(PRESENTATION),
    }
    if provider_presentation is not None:
        claim["provider_presentation"] = dict(provider_presentation)
    return claim


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="%%acp", add_help=False)
    parser.add_argument("alias")
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument("--load", dest="load_session", default="")
    sessions.add_argument("--resume", dest="resume_session", default="")
    return parser


class _KernelRegistry:
    def __init__(self) -> None:
        self.configuration: dict[str, Any] | None = None
        self.providers: dict[str, str] = {}
        self.registered_ipython: set[int] = set()

    def configure(self, configuration: Mapping[str, Any]) -> None:
        snapshot = copy.deepcopy(dict(configuration))
        if self.configuration is not None and self.configuration != snapshot:
            raise RuntimeError("ACP adapters received inconsistent runtime configuration")
        self.configuration = snapshot

    def add_provider(self, plugin_id: str, plugin_version: str) -> None:
        existing = self.providers.get(plugin_id)
        if existing is not None and existing != plugin_version:
            raise RuntimeError(f"ACP provider {plugin_id!r} registered with conflicting versions")
        self.providers[plugin_id] = plugin_version

    def install_magic(self, ipython: Any) -> None:
        identity = id(ipython)
        if identity in self.registered_ipython:
            return
        cell_magics = getattr(getattr(ipython, "magics_manager", None), "magics", {}).get("cell", {})
        existing = cell_magics.get(MAGIC_NAME)
        if existing is not None and not getattr(existing, "__jusi_acp_dispatcher_v1__", False):
            raise RuntimeError("The %%acp magic is already owned by another extension")

        def acp_magic(line: str, cell: str) -> None:
            from IPython.display import display

            args = _parser().parse_args(shlex.split(line))
            action = "new"
            session_id = ""
            if args.load_session:
                action, session_id = "load", str(args.load_session)
            elif args.resume_session:
                action, session_id = "resume", str(args.resume_session)
            try:
                target = resolve_alias(
                    self.configuration or {}, str(args.alias), set(self.providers)
                )
            except AliasConfigurationError as exc:
                raise UsageError(str(exc)) from exc
            handoff = {
                "protocol_version": 1,
                "kind": "plugin.handoff",
                "plugin_id": target.provider,
                "plugin_version": self.providers[target.provider],
                "family_id": FAMILY_ID,
                "magic_name": MAGIC_NAME,
                "payload": target.handoff_payload(
                    body=str(cell or ""), session_action=action, session_id=session_id
                ),
            }
            display({HANDOFF_MIME: handoff}, raw=True)

        acp_magic.__jusi_acp_dispatcher_v1__ = True  # type: ignore[attr-defined]
        if existing is None:
            ipython.register_magic_function(acp_magic, magic_kind="cell", magic_name=MAGIC_NAME)
        self.registered_ipython.add(identity)


_REGISTRY = _KernelRegistry()


class KernelProviderAdapter:
    """Thin exact-provider adapter backed by the shared deterministic dispatcher."""

    def __init__(self, plugin_id: str, plugin_version: str) -> None:
        self.plugin_id = plugin_id
        self.plugin_version = plugin_version

    def manifest(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "families": [{"family_id": FAMILY_ID, "magic_name": MAGIC_NAME}],
        }

    def configure(self, configuration: Mapping[str, Any]) -> None:
        _REGISTRY.configure(configuration)

    def load_ipython_extension(self, ipython: Any) -> None:
        _REGISTRY.add_provider(self.plugin_id, self.plugin_version)
        _REGISTRY.install_magic(ipython)


def _reset_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = _KernelRegistry()
