from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from acp.schema import ElicitationUrlSessionMode, PermissionOption, ToolCallUpdate
from jusi import editor_client

from jusi_acp.application import ACPApplication
from jusi_acp.attention import AttentionPublisher, BackgroundTools
from test_application import _payload
from test_session_features import _question


@pytest.fixture
def delivered(monkeypatch):
    monkeypatch.setenv("JUSI_EDITOR_ACTION_SOCKET", "/fixture-attention")
    calls = []

    def request(**content):
        attention_id = f"attention-{len(calls)}"
        calls.append(("request", attention_id, content))
        return attention_id

    monkeypatch.setattr(editor_client, "request_attention", request)
    monkeypatch.setattr(editor_client, "update_attention", lambda key, **content: calls.append(("update", key, content)))
    monkeypatch.setattr(editor_client, "clear_attention", lambda key: calls.append(("clear", key)))
    return calls


@pytest.fixture
def app(tmp_path, monkeypatch, delivered):
    monkeypatch.setattr("jusi_acp.application.queue_action", lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = "session"
    app.store.persist = False
    yield app
    app.close()
    if app.attention._thread:
        app.attention._thread.join(2)
        assert not app.attention._thread.is_alive()


def test_questions_update_same_attention_and_clear_on_answer_or_withdrawal(app, delivered):
    async def run():
        first = asyncio.create_task(_question(app, "private question", 2))
        await asyncio.sleep(0)
        app.attention._queue.join()
        first_id = delivered[0][1]
        assert delivered == [("request", first_id, {"kind": "action_required", "message": "Answer required (1/2)"})]

        second = asyncio.create_task(_question(app, "another private question"))
        await asyncio.sleep(0)
        app.attention._queue.join()
        second_id = delivered[1][1]
        # Reopening/closing a view does not withdraw the underlying request.
        app._publish_question(force=True)
        app.attention._queue.join()
        assert len(delivered) == 2

        await app._answer_question("first answer")
        app.attention._queue.join()
        assert delivered[-1] == ("update", first_id, {"kind": "action_required", "message": "Answer required (2/2)"})
        await app._answer_question("second answer")
        await first
        app.attention._queue.join()
        assert delivered[-1] == ("clear", first_id)

        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        app.attention._queue.join()
        assert delivered[-1] == ("clear", second_id)
        assert sum(call[0] == "request" for call in delivered) == 2
    asyncio.run(run())


def test_approval_and_elicitation_clear_when_resolved(app, delivered):
    async def run():
        approval = asyncio.create_task(app.request_permission("session", ToolCallUpdate(
            tool_call_id="sensitive-command", title="secret"),
            [PermissionOption(option_id="no", name="Reject", kind="reject_once")]))
        await asyncio.sleep(0)
        app.attention._queue.join()
        assert delivered[0][2] == {"kind": "action_required", "message": "Approval required"}
        request_id = app._answer_state["request_id"]
        app.select_permission(request_id, "no")
        await approval
        app.attention._queue.join()
        assert delivered[-1] == ("clear", delivered[0][1])

        elicitation = asyncio.create_task(app.create_elicitation("private URL", ElicitationUrlSessionMode(
            session_id="session", elicitation_id="url", url="https://example.com/secret")))
        await asyncio.sleep(0)
        app.attention._queue.join()
        url_id = delivered[-1][1]
        await app._answer_question("/decline")
        assert (await elicitation).action == "decline"
        app.attention._queue.join()
        assert delivered[-1] == ("clear", url_id)
    asyncio.run(run())


def test_cancel_clears_all_pending_requests(app, delivered):
    async def run():
        questions = [asyncio.create_task(_question(app, str(i))) for i in range(2)]
        await asyncio.sleep(0)
        app.attention._queue.join()
        ids = {call[1] for call in delivered}
        app.cancel_initial()
        await asyncio.gather(*questions)
        app.attention._queue.join()
        assert {call[1] for call in delivered if call[0] == "clear"} == ids
    asyncio.run(run())


@pytest.mark.parametrize("closing", [False, True])
def test_slow_creation_does_not_block_and_clear_waits_for_id(delivered, monkeypatch, closing):
    entered, release = threading.Event(), threading.Event()

    def request(**content):
        entered.set()
        assert release.wait(3)
        delivered.append(("request", "slow", content))
        return "slow"

    monkeypatch.setattr(editor_client, "request_attention", request)
    publisher = AttentionPublisher()
    try:
        publisher.sync_requests({"question": "Answer required"})
        assert entered.wait(2)
        if closing:
            publisher.close()
        else:
            publisher.sync_requests({})
        assert not delivered
        release.set()
        publisher._queue.join()
        assert delivered[-1] == ("clear", "slow")
    finally:
        release.set()
        publisher.close()
        publisher._thread.join(2)


def test_failed_creation_is_not_retried_and_does_not_prevent_answer(app, delivered, monkeypatch, caplog):
    def fail(**content):
        delivered.append(("failed", content))
        raise editor_client.EditorDeliveryError("Acceptance unknown")

    monkeypatch.setattr(editor_client, "request_attention", fail)

    async def run():
        question = asyncio.create_task(_question(app, "question", 2))
        await asyncio.sleep(0)
        app.attention._queue.join()
        await app._answer_question("one")
        await app._answer_question("two")
        assert (await question).answers == {"0": "one", "1": "two"}
        app.attention._queue.join()
        assert len(delivered) == 1
        assert "Acceptance unknown" in caplog.text
    asyncio.run(run())


def test_reset_clears_requests_and_notices(delivered):
    publisher = AttentionPublisher()
    publisher.sync_requests({"question": "Answer required"})
    publisher.notice("Background tool finished")
    publisher._queue.join()
    ids = {call[1] for call in delivered}
    publisher.reset()
    publisher._queue.join()
    assert {call[1] for call in delivered if call[0] == "clear"} == ids
    publisher.close()
    publisher._thread.join(2)


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_background_tool_completion_is_distinct_from_messages_and_foreground_tools(status):
    tracker = BackgroundTools()
    def update(key, state, turn=1, kind="tool_call_update"):
        return tracker.update({"sessionUpdate": kind, "toolCallId": key, "status": state}, turn)
    assert update("background", "in_progress", kind="tool_call") is None
    assert update("foreground", "in_progress", kind="tool_call") is None
    assert update("foreground", "completed") is None
    tracker.finish_turn(1)
    assert tracker.update({"sessionUpdate": "agent_message_chunk", "content": {"text": "Job finished"}}, None) is None
    assert update("unknown", status, turn=2) is None
    assert update("background", status, turn=2) == status
    assert update("background", status, turn=2) is None
    assert update("background", "", turn=2) is None


def test_cancelled_and_unattributed_tools_do_not_notify():
    tracker = BackgroundTools()
    for key, turn in [("cancelled", 1), ("unattributed", None)]:
        tracker.update({"sessionUpdate": "tool_call", "toolCallId": key, "status": "in_progress"}, turn)
    tracker.finish_turn(1, cancelled=True)
    for key in ["cancelled", "unattributed"]:
        assert tracker.update({"sessionUpdate": "tool_call_update", "toolCallId": key, "status": "completed"}, None) is None


def _background_message(task_id="task-1", tool_id="launch-1", source="background_task_completed", status="completed"):
    return {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Background agent completed."},
        "_meta": {"source": source, "qwenDiscreteMessage": True, "backgroundTask": {
            "taskId": task_id, "toolUseId": tool_id, "status": status,
            "kind": "agent", "sourceTurnId": "session########1",
        }},
    }


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("sources", [
    ("background_task_completed", "background_notification"),
    ("background_notification", "background_task_completed"),
])
def test_background_metadata_notifies_once_after_launch_tool_finished(status, sources):
    tracker = BackgroundTools()
    for tool in ["launch-1", "launch-2"]:
        tracker.update({"sessionUpdate": "tool_call", "toolCallId": tool, "status": "in_progress"}, 1)
        assert tracker.update({"sessionUpdate": "tool_call_update", "toolCallId": tool, "status": "completed"}, 1) is None
    tracker.finish_turn(1)
    for number in [1, 2]:
        first = _background_message(f"task-{number}", f"launch-{number}", sources[0], status)
        duplicate = _background_message(f"task-{number}", f"launch-{number}", sources[1], status)
        # Metadata carries the meaning regardless of the human-readable text.
        first["content"]["text"] = "Results ready"
        assert tracker.update(first, 2) == status
        assert tracker.update(duplicate, 2) is None
        assert tracker.update(first, 2) is None
        duplicate["_meta"]["backgroundTask"].pop("toolUseId")
        assert tracker.update(duplicate, None) is None


@pytest.mark.parametrize("metadata_first", [True, False])
def test_background_metadata_and_tool_completion_share_deduplication(metadata_first):
    tracker = BackgroundTools()
    tracker.update({"sessionUpdate": "tool_call", "toolCallId": "launch-1", "status": "in_progress"}, 1)
    tracker.finish_turn(1)
    events = [_background_message(), {
        "sessionUpdate": "tool_call_update", "toolCallId": "launch-1", "status": "completed",
    }]
    if not metadata_first:
        events.reverse()
    assert [tracker.update(event, None) for event in events] == ["completed", None]
    assert tracker.update(_background_message(source="background_notification"), None) is None


@pytest.mark.parametrize("metadata", [
    None, [], {}, {"source": "other", "backgroundTask": {"taskId": "task", "status": "completed"}},
    {"source": "background_task_completed"},
    {"source": "background_task_completed", "backgroundTask": []},
    {"source": "background_task_completed", "backgroundTask": {"status": "completed"}},
    {"source": "background_task_completed", "backgroundTask": {"taskId": [], "status": "completed"}},
    {"source": "background_task_completed", "backgroundTask": {"taskId": " ", "status": "completed"}},
    {"source": "background_task_completed", "backgroundTask": {"taskId": "task", "status": "running"}},
    {"source": "background_task_completed", "backgroundTask": {"taskId": "task", "status": "cancelled"}},
])
def test_message_text_and_unqualified_metadata_do_not_notify(metadata):
    raw = _background_message()
    raw["_meta"] = metadata
    assert BackgroundTools().update(raw, None) is None


def test_background_metadata_delivery_ignores_replay_and_wrong_session(app, delivered):
    async def update(raw, session="session"):
        await app.session_update(session, SimpleNamespace(model_dump=lambda **kwargs: raw))

    async def run():
        first = _background_message()
        app._session_loading = True
        await update(first)
        app._session_loading = False
        await update(first, "other-session")
        app.attention._queue.join()
        assert not delivered
        # A live event still qualifies after an ignored replay/foreign event.
        await update(first)
        await update(_background_message(source="background_notification"))
        await update(_background_message(task_id="task-2", tool_id="launch-2"))
        await update(_background_message(task_id="task-2", tool_id="launch-2", source="background_notification"))
        app.attention._queue.join()
        assert [call[2] for call in delivered] == [
            {"kind": "notice", "message": "Background tool finished"},
            {"kind": "notice", "message": "Background tool finished"},
        ]
    asyncio.run(run())


def test_application_notifies_only_live_background_completion(app, delivered):
    async def update(kind, **fields):
        raw = {"sessionUpdate": kind, **fields}
        await app.session_update("session", SimpleNamespace(model_dump=lambda **kwargs: raw))

    class Connection:
        async def prompt(self, *args):
            await update("tool_call", toolCallId="job", status="in_progress")
            await update("agent_message_chunk", content={"type": "text", "text": "Started"})
            return SimpleNamespace(stop_reason="end_turn")

    async def run():
        app._prompt_lock = asyncio.Lock()
        app.connection = Connection()
        await app._prompt("start", initial=False)
        app.attention._queue.join()
        assert not delivered
        app.store.add({"kind": "user_prompt", "text": "another turn"})
        await update("tool_call_update", toolCallId="job", status="completed")
        await update("tool_call_update", toolCallId="job", status="completed")
        await update("agent_message_chunk", content={"type": "text", "text": "Finished"})
        app._session_loading = True
        await update("tool_call", toolCallId="replay", status="in_progress")
        app._session_loading = False
        app._background_tools.finish_turn(2)
        await update("tool_call_update", toolCallId="replay", status="completed")
        app.attention._queue.join()
        assert len(delivered) == 1
        assert delivered[0][2] == {"kind": "notice", "message": "Background tool finished"}
    asyncio.run(run())
