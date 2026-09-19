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
