from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from acp.schema import AvailableCommand
from jusi.protocol import validate_completion

from jusi_acp.application import ACPApplication


def _payload(tmp_path: Path, body: str = "") -> dict:
    agent = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"
    return {
        "plugin_id": "fixture",
        "plugin_version": "1",
        "launch": {"argv": [sys.executable, str(agent)], "cwd": str(tmp_path), "environment": {}},
        "submission": {
            "alias": "test", "body": body, "cwd": str(tmp_path),
            "additional_directories": [], "mcp_servers": [],
            "session_action": "new", "session_id": "",
        },
    }


def test_real_acp_process_followup_terminal_and_reuse(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        assert app._connected.wait(10)
        first = app.handle_operation("followup", {"body": "hello"})
        terminal = app.handle_operation("followup", {"body": "terminal"})
        assert first["stop_reason"] == "end_turn"
        assert terminal["stop_reason"] == "end_turn"
        texts = [row["text"] for row in app.store.rows]
        assert "echo:hello" in texts
        assert "terminal-ok:0" in texts
        again = app.handle_operation("followup", {"body": "again"})
        assert again["session_id"] == "fake-session"
    finally:
        app.close()


def test_real_acp_followup_cancellation_preserves_session(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    app = ACPApplication(_payload(tmp_path))
    app.start()
    outcome: list[BaseException] = []

    def followup() -> None:
        try:
            app.handle_operation("followup", {"body": "wait"})
        except BaseException as exc:
            outcome.append(exc)

    try:
        assert app._connected.wait(10)
        thread = threading.Thread(target=followup)
        thread.start()
        time.sleep(0.2)
        app.handle_operation("interrupt", {})
        thread.join(5)
        assert len(outcome) == 1 and isinstance(outcome[0], InterruptedError)
        assert app.handle_operation("followup", {"body": "healthy"})["stop_reason"] == "end_turn"
    finally:
        app.close()


def test_initial_turn_can_be_cancelled_as_soon_as_application_is_ready(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    app = ACPApplication(_payload(tmp_path, body="wait"))
    app.start()
    try:
        assert app._connected.wait(10)
        app.cancel_initial()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(row["title"] == "Initial turn cancelled" for row in app.store.rows):
                break
            time.sleep(0.01)
        assert any(row["title"] == "Initial turn cancelled" for row in app.store.rows)
    finally:
        app.close()


def test_initial_body_uses_the_same_family_command_dispatch_as_followup(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    app = ACPApplication(_payload(tmp_path, body="/config model qwen"))
    app.start()
    try:
        assert app._connected.wait(10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(row["title"] == "Initial command rejected" for row in app.store.rows):
                break
            time.sleep(0.01)
        rejected = [row for row in app.store.rows if row["title"] == "Initial command rejected"]
        assert rejected and "does not advertise" in rejected[0]["text"]
        assert not any(row["type"] == "user_prompt" for row in app.store.rows)
    finally:
        app.close()


def test_interrupt_before_followup_submission_is_not_lost(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        assert app._connected.wait(10)
        assert app.handle_operation("begin_followup", {}) == {"begun": True}
        app.handle_operation("interrupt", {})
        with pytest.raises(InterruptedError, match="cancelled"):
            app.handle_operation("followup", {"body": "wait"})
        assert app.handle_operation("followup", {"body": "healthy"})["stop_reason"] == "end_turn"
    finally:
        app.close()


def test_completion_uses_explicit_unicode_range_and_preserves_suffix(tmp_path: Path) -> None:
    app = ACPApplication(_payload(tmp_path))
    body = "α\n/cancelXYZ"
    prefix = "α\n/ca"
    result = app.handle_operation("complete", {
        "body": body, "prefix": prefix, "cursor_pos": len(prefix),
        "cursor_row": 1, "cursor_col": 3,
    })
    assert validate_completion(result, len(prefix)) == result
    assert result["items"][0]["text"] == "/cancel"
    assert result["items"][0]["start"] == 2


def test_agent_capabilities_control_optional_family_commands(tmp_path: Path) -> None:
    app = ACPApplication(_payload(tmp_path))
    assert [item["text"] for item in app.handle_operation(
        "complete", {"prefix": "/", "cursor_pos": 1}
    )["items"]] == ["/cancel"]
    app.session_modes = SimpleNamespace()
    app.config_options = [SimpleNamespace(id="model")]
    app.initialize_response = SimpleNamespace(auth_methods=[SimpleNamespace(id="login")])
    texts = [item["text"] for item in app.handle_operation(
        "complete", {"prefix": "/", "cursor_pos": 1}
    )["items"]]
    assert texts == ["/cancel", "/mode", "/config", "/auth"]


def test_available_command_completion_includes_standard_input_hint(tmp_path: Path) -> None:
    app = ACPApplication(_payload(tmp_path))
    app.available_commands = [AvailableCommand(
        name="skills", description="List available skills", input={"hint": "optional filter"}
    )]
    item = app.handle_operation(
        "complete", {"prefix": "/sk", "cursor_pos": 3}
    )["items"][0]
    assert item["text"] == "/skills"
    assert item["detail"] == "List available skills — input: optional filter"
