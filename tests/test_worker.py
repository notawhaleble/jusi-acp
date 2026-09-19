from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from jusi.plugin_api import OperationRejected

from jusi_acp import ACPWorker, AgentLaunch, ProviderSpec


def _provider(plugin_id: str) -> ProviderSpec:
    def launch(config, cwd):  # type: ignore[no-untyped-def]
        return AgentLaunch((str(config.get("command", "agent")), "--acp"), cwd, {"TOKEN_REF": "name"})
    return ProviderSpec(plugin_id, "1", launch)


def test_worker_returns_shared_application_surface_with_exact_launch(tmp_path: Path) -> None:
    worker = ACPWorker(SimpleNamespace(plugin_id="gigacode"), _provider("gigacode"))
    try:
        result = worker.handle("execute", {
            "alias": "work", "body": "hello", "cwd": str(tmp_path),
            "configuration": {"command": "gigacode"}, "additional_directories": [],
            "mcp_servers": [], "session_action": "new", "session_id": "",
        })
        assert result.result == {"accepted": True}
        surface = result.core_requests[0]
        assert surface.argv[1:3] == ("-m", "jusi_acp.application")
        assert surface.cwd == str(tmp_path)
        assert set(surface.capabilities) == {"input", "resize", "signal"}
        payload_path = Path(surface.argv[3])
        payload = payload_path.read_text(encoding="utf-8")
        assert '"gigacode"' in payload
    finally:
        runtime = worker.runtime_directory
        worker.close()
    assert runtime is not None and not runtime.exists()


def test_worker_identity_must_match_exact_provider() -> None:
    with pytest.raises(ValueError, match="does not match"):
        ACPWorker(SimpleNamespace(plugin_id="other"), _provider("gigacode"))


def test_bad_provider_configuration_is_recoverable(tmp_path: Path) -> None:
    def reject(config, cwd):  # type: ignore[no-untyped-def]
        raise ValueError("missing executable")
    worker = ACPWorker(SimpleNamespace(plugin_id="gigacode"), ProviderSpec("gigacode", "1", reject))
    with pytest.raises(OperationRejected, match="missing executable"):
        worker.handle("execute", {"cwd": str(tmp_path), "configuration": {}})
