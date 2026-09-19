from __future__ import annotations

from pathlib import Path

import pytest
from IPython.core.error import UsageError
from jusi.plugin_api import ProtocolValidationError, validate_catalog_claims

from jusi_acp.config import AliasConfigurationError, resolve_alias
from jusi_acp.family import (
    CAPABILITIES,
    HANDOFF_MIME,
    KernelProviderAdapter,
    _reset_registry_for_tests,
    family_claim,
)


def _plugin(plugin_id: str, version: str, module: str) -> dict:
    return {
        "plugin_id": plugin_id,
        "plugin_version": version,
        "distribution": f"jusi-{plugin_id}",
        "families": [family_claim()],
        "kernel_extensions": [module],
        "worker_entry_point": f"jusi_{plugin_id}.worker:create_worker",
        "media_types": ["text/x-ansi"],
        "interaction": "terminal_interactive",
    }


def test_two_exact_providers_make_compatible_catalog_claims() -> None:
    catalog = {
        "protocol_version": 1,
        "catalog_version": 1,
        "discovery_id": "two_acp_providers",
        "plugins": [_plugin("gigacode", "1", "gigacode.kernel"), _plugin("fixture", "2", "fixture.kernel")],
    }
    assert validate_catalog_claims(catalog) == catalog
    assert family_claim()["capabilities"] == list(CAPABILITIES)


def test_family_rejects_incompatible_shared_claims_but_allows_provider_presentation() -> None:
    first = _plugin("gigacode", "1", "gigacode.kernel")
    second = _plugin("fixture", "2", "fixture.kernel")
    second["families"][0]["provider_presentation"] = {"syntax": "python"}
    catalog = {
        "protocol_version": 1,
        "catalog_version": 1,
        "discovery_id": "compatible_provider_presentation",
        "plugins": [first, second],
    }
    assert validate_catalog_claims(catalog) == catalog

    second["families"][0]["capabilities"] = ["execute"]
    with pytest.raises(ProtocolValidationError, match="Conflicting family claim"):
        validate_catalog_claims(catalog)


def test_alias_resolution_uses_flat_legacy_shape(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    result = resolve_alias({
        "acp": {"work": {
            "provider": "gigacode", "path": str(target), "model": "qwen",
            "additional_directories": ["../shared"],
        }}
    }, "work", {"gigacode"})
    assert result.provider == "gigacode"
    assert result.cwd == target
    assert result.configuration["model"] == "qwen"
    assert result.additional_directories == ((target / "../shared").resolve(),)


@pytest.mark.parametrize("configuration, message", [
    ({}, "not configured"),
    ({"acp": {}}, "Unknown ACP alias"),
    ({"acp": {"work": {"path": "/tmp"}}}, "requires a provider"),
    ({"acp": {"work": {"provider": "missing", "path": "/tmp"}}}, "unavailable provider"),
])
def test_alias_resolution_rejects_missing_or_unavailable(configuration: dict, message: str) -> None:
    with pytest.raises(AliasConfigurationError, match=message):
        resolve_alias(configuration, "work", {"gigacode"})


def test_two_kernel_adapters_register_one_dispatcher_and_route_exact_provider(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _reset_registry_for_tests()
    displayed: list[dict] = []
    registered: dict[str, object] = {}

    class FakeIPython:
        magics_manager = type("Manager", (), {"magics": {"cell": {}}})()

        def register_magic_function(self, function, magic_kind, magic_name):  # type: ignore[no-untyped-def]
            registered[magic_name] = function
            self.magics_manager.magics[magic_kind][magic_name] = function

    monkeypatch.setattr("IPython.display.display", lambda value, raw=False: displayed.append(value))
    config = {"acp": {
        "one": {"provider": "gigacode", "path": str(tmp_path)},
        "two": {"provider": "fixture", "path": str(tmp_path)},
    }}
    first = KernelProviderAdapter("gigacode", "1")
    second = KernelProviderAdapter("fixture", "2")
    ipython = FakeIPython()
    for adapter in (first, second):
        adapter.configure(config)
        adapter.load_ipython_extension(ipython)
    assert list(registered) == ["acp"]

    registered["acp"]("two --resume session-7", "hello")  # type: ignore[operator]
    handoff = displayed[0][HANDOFF_MIME]
    assert handoff["plugin_id"] == "fixture"
    assert handoff["plugin_version"] == "2"
    assert handoff["payload"]["session_action"] == "resume"
    assert handoff["payload"]["session_id"] == "session-7"


def test_dispatcher_rejects_an_unavailable_configured_provider(tmp_path: Path) -> None:
    _reset_registry_for_tests()
    registered = {}

    class FakeIPython:
        magics_manager = type("Manager", (), {"magics": {"cell": {}}})()
        def register_magic_function(self, function, magic_kind, magic_name):  # type: ignore[no-untyped-def]
            registered[magic_name] = function

    adapter = KernelProviderAdapter("gigacode", "1")
    adapter.configure({"acp": {"work": {"provider": "other", "path": str(tmp_path)}}})
    adapter.load_ipython_extension(FakeIPython())
    with pytest.raises(UsageError, match="unavailable provider"):
        registered["acp"]("work", "hello")


def test_full_restart_can_load_a_new_snapshot(tmp_path: Path) -> None:
    _reset_registry_for_tests()
    adapter = KernelProviderAdapter("gigacode", "1")
    adapter.configure({"acp": {"old": {"provider": "gigacode", "path": str(tmp_path)}}})
    _reset_registry_for_tests()
    adapter.configure({"acp": {"new": {"provider": "gigacode", "path": str(tmp_path)}}})
