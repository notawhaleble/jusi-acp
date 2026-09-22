from __future__ import annotations

import asyncio
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


def test_refresh_requests_are_coalesced_and_preserve_tail_choice(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = ACPApplication(_payload(tmp_path))
    queued = []
    monkeypatch.setattr("jusi_acp.application.queue_action", queued.append)
    app._last_refresh = 0.0

    for _ in range(20):
        app._refresh(follow_tail=True)

    assert len(queued) == 1

    class FakeSheet:
        def __init__(self) -> None:
            self.rows = [{}, {}, {}]
            self.cursorRowIndex = 2
            self.recalculated = 0

        def recalc(self) -> None:
            self.recalculated += 1

    app.live_sheet = FakeSheet()
    queued.pop()()
    assert app.live_sheet.recalculated == 1
    assert app.live_sheet.cursorRowIndex == 2


def test_live_tail_is_not_followed_after_user_moves_away(tmp_path: Path) -> None:
    app = ACPApplication(_payload(tmp_path))

    class FakeSheet:
        rows = [{}, {}, {}]
        cursorRowIndex = 0

    app.live_sheet = FakeSheet()
    assert app._live_sheet_should_follow_tail() is False
    app.live_sheet.cursorRowIndex = 2
    assert app._live_sheet_should_follow_tail() is True


def test_prompt_failure_closes_turn_and_keeps_error_event(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = ACPApplication(_payload(tmp_path))
    app.session_id = "session-1"
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)

    class BrokenConnection:
        async def prompt(self, session_id, content):  # type: ignore[no-untyped-def]
            _ = session_id, content
            raise RuntimeError("agent broke")

    async def run() -> None:
        app._prompt_lock = asyncio.Lock()
        app.connection = BrokenConnection()
        with pytest.raises(RuntimeError, match="agent broke"):
            await app._prompt("hello", initial=True)

    asyncio.run(run())
    turn = app.store.turns[0]
    assert turn["status"] == "failed"
    assert [row["type"] for row in turn["events"]][-2:] == ["turn_error", "turn_stopped"]


def test_asyncio_background_errors_are_queued_for_visidata(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = ACPApplication(_payload(tmp_path))
    queued = []
    monkeypatch.setattr("jusi_acp.application.queue_action", queued.append)
    error = RuntimeError("background broke")

    loop = asyncio.new_event_loop()
    try:
        app._handle_asyncio_exception(loop, {"exception": error})
    finally:
        loop.close()

    assert app.store.rows[-1]["title"] == "ACP background task failed"
    assert app.store.rows[-1]["status"] == "failed"
    with pytest.raises(RuntimeError, match="background broke"):
        queued[-1]()


def test_browser_environment_reaches_auth_process(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BROWSER", "fixture-browser")
    payload = _payload(tmp_path)
    payload["launch"]["auth_method"] = "browser"
    app = ACPApplication(payload)
    app.start()
    try:
        assert app._connected.wait(10)
        assert app._failed is None
        assert app.session_id == "fake-session"
        assert any(row["title"] == "Authenticated" for row in app.store.rows)
    finally:
        app.close()


def test_model_snapshot_is_retained_per_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = "session"

    class Connection:
        async def prompt(self, *args):
            return SimpleNamespace(stop_reason="end_turn")

    async def run():
        app.connection = Connection()
        app._prompt_lock = asyncio.Lock()
        for model in ("first", "second"):
            app.config_options = [SimpleNamespace(category="model", current_value=model)]
            await app._prompt("hello", initial=False)

    asyncio.run(run())
    assert [row["model"] for row in app.store.turns_snapshot()] == ["first", "second"]


def test_sdk_logging_stays_in_diagnostics_without_visidata_error(tmp_path, monkeypatch, capsys):
    import logging
    from jusi_acp.application import _ApplicationLogHandler
    from acp import RequestError
    from jusi_acp.ui import drain_actions
    from visidata import vd

    from collections import deque
    monkeypatch.setattr("jusi_acp.ui._ACTIONS", deque())
    monkeypatch.setattr(vd, "status", lambda *args, **kwargs: None)
    app = ACPApplication(_payload(tmp_path))
    monkeypatch.setattr(app, "_refresh", lambda **kwargs: None)
    previous_errors = len(vd.lastErrors)
    logger = logging.getLogger("test-acp-routing")
    monkeypatch.setattr(logger, "handlers", [_ApplicationLogHandler(app)])
    monkeypatch.setattr(logger, "propagate", False)
    try:
        raise RequestError.method_not_found("fixture/unknown")
    except RequestError:
        logger.exception("Unhandled notification method=fixture/unknown")
    drain_actions()
    assert "fixture/unknown" in app.store.rows[-1]["text"]
    assert len(vd.lastErrors) == previous_errors
    assert capsys.readouterr().err == ""


def test_followup_strips_only_the_acp_cell_header(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        result = app.handle_operation("followup", {"body": "%%acp test\nhello"})
        assert result["stop_reason"] == "end_turn"
        prompts = [row["text"] for row in app.store.rows if row["type"] == "user_prompt"]
        assert prompts == ["hello"]
        result = app.handle_operation("followup", {"body": "keep\n%%acp literal"})
        assert result["stop_reason"] == "end_turn"
        prompts = [row["text"] for row in app.store.rows if row["type"] == "user_prompt"]
        assert prompts[-1] == "keep\n%%acp literal"
    finally:
        app.close()


def test_sessions_command_lists_then_loads_selected_session(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: action())
    monkeypatch.setattr(ACPApplication, "_push_sessions", lambda self, rows: setattr(self, "listed", rows))
    monkeypatch.setattr(ACPApplication, "_focus_turns_sheet", lambda self: None)
    app = ACPApplication(_payload(tmp_path, body="/sessions"))
    app.start()
    try:
        assert app._connected.wait(10)
        assert app.session_id == ""
        assert app.listed[0]["session_id"] == "old-session"
        app.select_session("old-session")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and app.session_id != "old-session":
            time.sleep(0.01)
        assert app.session_id == "old-session"
        assert any(row["text"] == "replayed" for row in app.store.rows)
        assert app.handle_operation("followup", {"body": "continued"})["session_id"] == "old-session"
    finally:
        app.close()


def test_legacy_model_response_is_preserved_before_sdk_validation(tmp_path):
    app = ACPApplication(_payload(tmp_path))
    app._observe_protocol(SimpleNamespace(direction="incoming", message={
        "id": 1, "result": {"sessionId": "fake-session", "models": {"currentModelId": "giga-model"}},
    }))
    assert app._current_model() == "giga-model"


def test_question_waits_for_answers_and_can_be_cancelled(tmp_path, monkeypatch):
    from acp.schema import PermissionOption, ToolCallUpdate
    app = ACPApplication(_payload(tmp_path))
    app.session_id = "session"
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)

    async def run():
        task = asyncio.create_task(app.request_permission("session", ToolCallUpdate(
            tool_call_id="question", raw_input={"questions": [{"question": "Which?", "options": []}]},
        ), [PermissionOption(option_id="allow", kind="allow_once", name="Answer")]))
        await asyncio.sleep(0)
        assert not task.done()
        app._cancel_permissions()
        response = await asyncio.wait_for(task, 1)
        assert response.outcome.outcome == "cancelled"
        assert not app._permissions

    asyncio.run(run())


@pytest.mark.parametrize("initial", [False, True])
def test_questions_are_answered_by_followups_in_the_same_turn(tmp_path, monkeypatch, initial):
    import json
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path, "questions-many" if initial else ""))
    app.start()
    try:
        assert app._connected.wait(10)
        if initial:
            deadline = time.monotonic() + 5
            while app._answer_state is None and time.monotonic() < deadline:
                time.sleep(0.01)
            waiting = app._answer_state
        else:
            waiting = app.handle_operation("followup", {"body": "questions-many"})
        assert waiting["status"] == "awaiting_answer"
        assert waiting["question_number"] == 1
        assert app.store.turns_snapshot()[0]["status"] == "awaiting_answer"
        with pytest.raises(ValueError, match="empty"):
            app.handle_operation("followup", {"body": " \n "})
        assert not app._questions[waiting["request_id"]].answers
        completions = app.handle_operation("complete", {"prefix": "App", "cursor_pos": 3})
        assert validate_completion(completions, 3) == completions
        assert completions["items"][0]["text"] == "Application"
        waiting = app.handle_operation("followup", {"body": "Application"})
        assert waiting["question_number"] == 2
        answer = "  Keep this indentation.\nα and β\n/config is literal answer text here.  "
        result = app.handle_operation("followup", {"body": answer})
        assert result["stop_reason"] == "end_turn"
        expected = {"0": "Application", "1": answer}
        replies = [row["text"] for row in app.store.raw_snapshot() if row["type"] == "agent_message_chunk"]
        assert json.loads(replies[0].removeprefix("ANSWERS:")) == expected
        assert len(app.store.turns_snapshot()) == 1
        answers = [row["text"] for row in app.store.turn_events_snapshot(1) if row["type"] == "question_answer"]
        assert answers == ["Application", answer]
        assert app.handle_operation("followup", {"body": "normal again"})["session_id"] == "fake-session"
        assert len(app.store.turns_snapshot()) == 2
    finally:
        app.close()


def test_second_question_request_releases_followup_again(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        first = app.handle_operation("followup", {"body": "questions-again"})
        second = app.handle_operation("followup", {"body": "/mode is an answer"})
        assert second["status"] == "awaiting_answer"
        assert first["request_id"] != second["request_id"]
        assert app.handle_operation("followup", {"body": "Library"})["stop_reason"] == "end_turn"
        assert len(app.store.turns_snapshot()) == 1
    finally:
        app.close()


def test_cancel_waiting_questions_from_followup_preserves_session(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        app.handle_operation("followup", {"body": "questions-many"})
        app.handle_operation("followup", {"body": "Application"})
        assert app.handle_operation("followup", {"body": "/cancel"}) == {"command": "cancel"}
        assert app._answer_state is None
        assert not app._questions
        assert app.store.turns_snapshot()[0]["status"] == "cancelled"
        assert app.handle_operation("followup", {"body": "healthy"})["stop_reason"] == "end_turn"
    finally:
        app.close()


def test_resumed_answer_operation_is_interruptible(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.start()
    errors = []
    def answer():
        try:
            app.handle_operation("followup", {"body": "Application"})
        except BaseException as exc:
            errors.append(exc)
    try:
        app.handle_operation("followup", {"body": "questions-wait"})
        thread = threading.Thread(target=answer)
        thread.start()
        deadline = time.monotonic() + 5
        while not any(row["text"] == "AFTER-ANSWERS" for row in app.store.raw_snapshot()) and time.monotonic() < deadline:
            time.sleep(0.01)
        app.handle_operation("interrupt", {})
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert app.handle_operation("followup", {"body": "healthy"})["stop_reason"] == "end_turn"
    finally:
        app.close()


def test_interrupt_before_answer_submission_does_not_answer_question(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        app.handle_operation("followup", {"body": "questions"})
        app.handle_operation("begin_followup", {})
        app.handle_operation("interrupt", {})
        with pytest.raises(InterruptedError):
            app.handle_operation("followup", {"body": "must not be sent"})
        deadline = time.monotonic() + 5
        while not app._turn_task.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(row["type"] == "question_answer" for row in app.store.raw_snapshot())
        assert app.handle_operation("followup", {"body": "healthy"})["stop_reason"] == "end_turn"
    finally:
        app.close()


def test_finished_turn_cancels_outstanding_question_target(tmp_path, monkeypatch):
    from acp.schema import PermissionOption, ToolCallUpdate
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = "session"

    async def run():
        finish = asyncio.Event()
        permission_task = None
        class Connection:
            async def prompt(self, *args):
                nonlocal permission_task
                permission_task = asyncio.create_task(app.request_permission("session", ToolCallUpdate(
                    tool_call_id="question", raw_input={"questions": [{"question": "Which?", "options": []}]},
                ), [PermissionOption(option_id="allow", kind="allow_once", name="Answer")]))
                await finish.wait()
                return SimpleNamespace(stop_reason="end_turn")
        app.connection = Connection()
        app._prompt_lock = asyncio.Lock()
        result = await app._prompt_or_command("ask", initial=True)
        assert result["status"] == "awaiting_answer"
        finish.set()
        await app._turn_task
        assert app._answer_state is None
        assert not app._questions
        assert (await permission_task).outcome.outcome == "cancelled"
    asyncio.run(run())


@pytest.mark.parametrize("body", ["mode-notification", "mode-notification-quen", "mode-notification-standalone"])
def test_vendor_mode_notifications_do_not_raise_or_duplicate(tmp_path, monkeypatch, caplog, body):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        assert app.handle_operation("followup", {"body": body})["stop_reason"] == "end_turn"
        assert app._current_mode_id == "plan"
        updates = [row for row in app.store.raw_snapshot() if row["type"] == "current_mode_update"]
        assert len(updates) == 1
        assert updates[0]["raw"]["currentModeId"] == "plan"
        assert not any(record.levelno >= 40 for record in caplog.records)
        assert app.handle_operation("followup", {"body": "healthy"})["stop_reason"] == "end_turn"
    finally:
        app.close()


def test_mode_compatibility_preserves_router_errors_and_session_scope(tmp_path, monkeypatch):
    from acp import RequestError
    from acp.client.router import build_client_router
    from jusi_acp.compat import install_mode_notifications
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = "current"
    app.session_modes = SimpleNamespace(current_mode_id="default")
    router = build_client_router(app)
    untouched = build_client_router(app)
    install_mode_notifications(SimpleNamespace(_conn=SimpleNamespace(_handler=router)), app._mode_notification)
    method = "qwen/notify/session/mode-update"
    async def run():
        await router(method, {"sessionId": "other", "currentModeId": "plan"}, True)
        assert app.session_modes.current_mode_id == "default"
        await router(method, {"sessionId": "current", "currentModeId": "plan"}, True)
        assert app.session_modes.current_mode_id == "plan"
        await router("_" + method, {"sessionId": "current", "currentModeId": "plan"}, True)
        assert len(app.store.raw_snapshot()) == 1
        for target, name, params, notification, code in [
            (router, method, {}, True, -32602),
            (router, "other/unknown", {}, True, -32601),
            (router, method, {}, False, -32601),
            (untouched, method, {}, True, -32601),
        ]:
            with pytest.raises(RequestError) as error:
                await target(name, params, notification)
            assert error.value.code == code
    asyncio.run(run())
