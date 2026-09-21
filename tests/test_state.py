from __future__ import annotations

from pathlib import Path

from jusi_acp.state import EventStore


def test_diff_content_is_preserved_for_editor_opening(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    store = EventStore("gigacode", tmp_path)
    store.bind_session("session-1", load_cache=False)
    row = store.add({
        "sessionUpdate": "tool_call_update",
        "content": [
            {"type": "diff", "path": "one.py", "oldText": "old", "newText": "new"},
            {"type": "diff", "path": "two.py", "newText": "created"},
        ],
    })
    assert row["diffs"] == [
        {"path": "one.py", "old_text": "old", "new_text": "new"},
        {"path": "two.py", "old_text": "", "new_text": "created"},
    ]


def test_available_commands_are_readable_in_the_event_sheet(tmp_path: Path) -> None:
    store = EventStore("gigacode", tmp_path)
    row = store.add({
        "sessionUpdate": "available_commands_update",
        "availableCommands": [{
            "name": "skills",
            "description": "List available skills",
            "input": {"hint": "optional filter"},
        }],
    })
    assert row["text"] == "/skills — List available skills — input: optional filter"


def test_turn_projection_groups_streams_without_losing_raw_journal(tmp_path: Path) -> None:
    store = EventStore("gigacode", tmp_path)
    store.add({"kind": "user_prompt", "text": "fix it"}, source="user")
    store.add({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "one "}})
    store.add({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "two"}})
    store.add({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "think "}})
    store.add({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "more"}})
    store.add({
        "sessionUpdate": "tool_call", "toolCallId": "call-1", "title": "Run", "status": "pending",
    })
    store.add({"kind": "status", "title": "Permission required", "status": "pending"}, source="client")
    store.add({
        "sessionUpdate": "tool_call_update", "toolCallId": "call-1", "title": "Ran", "status": "completed",
    })
    store.update_terminal("term-1", "partial", False, "running")
    store.update_terminal("term-1", "complete", False, "exit 0")
    store.add({"kind": "turn_stopped", "stopReason": "end_turn"})

    assert len(store.rows) == 10
    assert len(store.turns) == 1
    turn = store.turns[0]
    assert turn["prompt"] == "fix it"
    assert turn["reply"] == "one two"
    assert turn["status"] == "end_turn"
    assert [row["group"] for row in turn["events"]] == [
        "", "assistant", "thought", "tool", "", "terminal", "",
    ]
    assert turn["events"][1]["text"] == "one two"
    assert turn["events"][2]["text"] == "think more"
    assert turn["events"][3]["title"] == "Ran"
    assert turn["events"][3]["status"] == "completed"
    assert turn["events"][5]["text"] == "complete"
    assert turn["events"][5]["status"] == "exit 0"


def test_cached_raw_journal_rebuilds_turn_summaries(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path / "state"))
    first = EventStore("gigacode", tmp_path)
    first.bind_session("session-1", load_cache=False)
    first.add({"kind": "user_prompt", "text": "hello"}, source="user")
    first.add({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}})
    first.add({"kind": "turn_stopped", "stopReason": "end_turn"})

    resumed = EventStore("gigacode", tmp_path)
    resumed.bind_session("session-1", load_cache=True)
    assert len(resumed.rows) == 3
    assert resumed.turns[0]["prompt"] == "hello"
    assert resumed.turns[0]["reply"] == "hi"
    assert resumed.turns[0]["status"] == "end_turn"
    assert all(row["cached"] is True for row in resumed.rows)


def test_tool_input_and_command_survive_partial_updates(tmp_path):
    store = EventStore("fixture", tmp_path)
    store.add({"kind": "user_prompt", "text": "inspect"})
    store.add({"sessionUpdate": "tool_call", "toolCallId": "one", "title": "Shell",
               "kind": "execute", "rawInput": {"command": "ls -la"}, "status": "pending"})
    store.add({"sessionUpdate": "tool_call_update", "toolCallId": "one", "status": "completed"})
    row = store.turn_events_snapshot(1)[1]
    assert row["command"] == "ls -la"
    assert row["tool"] == "execute"
    assert row["raw"]["rawInput"] == {"command": "ls -la"}
    assert row["status"] == "completed"
