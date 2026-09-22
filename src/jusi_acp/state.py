"""Normalized, read-only presentation cache for ACP sessions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


def state_root(provider: str, cwd: Path) -> Path:
    base = os.environ.get("JUSI_STATE_HOME") or os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() / "jusi" if base and not os.environ.get("JUSI_STATE_HOME") else None
    if os.environ.get("JUSI_STATE_HOME"):
        root = Path(os.environ["JUSI_STATE_HOME"]).expanduser()
    if root is None:
        root = Path.home() / ".local" / "state" / "jusi"
    digest = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:12]
    project = f"{cwd.name or 'root'}-{digest}"
    return root / "plugins" / "acp" / provider / project


@dataclass
class EventStore:
    provider: str
    cwd: Path
    session_id: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)
    _active_turn: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def bind_session(self, session_id: str, *, load_cache: bool) -> None:
        with self._lock:
            rebinding_session = bool(self.session_id)
            self.session_id = session_id
            if rebinding_session:
                self.rows.clear()
                self.turns.clear()
                self._active_turn = None
        if not load_cache:
            return
        path = self._events_path()
        if not path.exists():
            return
        cached: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        value["cached"] = True
                        cached.append(value)
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self.rows[:] = cached
            self.turns.clear()
            self._active_turn = None
            for row in cached:
                self._project(row)

    def add(self, update: Any, *, source: str = "agent") -> dict[str, Any]:
        raw = _json_value(update)
        kind = str(raw.get("sessionUpdate", raw.get("kind", "update")))
        row = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "type": kind,
            "title": _title(raw, kind),
            "status": str(raw.get("status", raw.get("stopReason", "")) or ""),
            "text": _text(raw),
            "tool_call_id": str(raw.get("toolCallId", "") or ""),
            "tool": str((raw.get("_meta") or {}).get("toolName", raw.get("name", raw.get("kind", "")))) if kind in {"tool_call", "tool_call_update"} else "",
            "command": _command(raw),
            "input": _display_value(raw.get("rawInput")),
            "terminal_id": _terminal_id(raw),
            "diffs": _diffs(raw),
            "raw": raw,
            "cached": False,
        }
        with self._lock:
            self.rows.append(row)
            self._project(row)
        self._append(row)
        return row

    def add_status(self, title: str, text: str = "", status: str = "") -> dict[str, Any]:
        return self.add({"kind": "status", "title": title, "text": text, "status": status}, source="client")

    def update_terminal(self, terminal_id: str, output: str, truncated: bool, status: str = "", *, command: str = "") -> None:
        row = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "client",
            "type": "terminal",
            "title": f"Terminal {terminal_id}" + (" (truncated)" if truncated else ""),
            "status": status,
            "text": output,
            "tool_call_id": "",
            "terminal_id": terminal_id,
            "command": command,
            "tool": "terminal",
            "diffs": [],
            "raw": {
                "kind": "terminal",
                "terminalId": terminal_id,
                "output": output,
                "command": command,
                "truncated": truncated,
                "status": status,
            },
            "cached": False,
        }
        with self._lock:
            existing = next(
                (item for item in reversed(self.rows) if item.get("terminal_id") == terminal_id),
                None,
            )
            if existing is None:
                self.rows.append(row)
            else:
                if not command:
                    row["command"] = existing.get("command", "")
                    row["raw"]["command"] = row["command"]
                existing.update(row)
            self._project(row)
        if status != "running":
            self._append(row)

    @property
    def active_turn(self) -> dict[str, Any] | None:
        with self._lock:
            return self._active_turn

    def raw_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self.rows)

    def turns_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy({key: value for key, value in turn.items() if key != "events"})
                    for turn in self.turns]

    def turn_events_snapshot(self, turn_number: int) -> list[dict[str, Any]]:
        with self._lock:
            turn = next((item for item in self.turns if item.get("turn") == turn_number), None)
            return deepcopy(turn.get("events", [])) if turn is not None else []

    def finish_replay(self) -> None:
        """Close the final history turn after session/load finishes replaying it."""
        with self._lock:
            if self._active_turn is not None:
                self._active_turn["status"] = "loaded"
                self._active_turn = None

    def _project(self, row: dict[str, Any]) -> None:
        kind = str(row.get("type", ""))
        if kind == "user_message_chunk" and self._active_turn is not None:
            events = self._active_turn["events"]
            if events and events[-1].get("group") == "user":
                text = str(row.get("text", ""))
                events[-1]["text"] = str(events[-1].get("text", "")) + text
                self._active_turn["prompt"] = str(self._active_turn.get("prompt", "")) + text
                return
            self._active_turn["status"] = "loaded"
            self._active_turn = None
        if kind in {"user_prompt", "user_message_chunk"}:
            turn = {
                "turn": len(self.turns) + 1,
                "time": row.get("time", ""),
                "prompt": row.get("text", ""),
                "reply": "",
                "model": row.get("raw", {}).get("model", ""),
                "status": "running",
                "stop_reason": "",
                "events": [],
            }
            self.turns.append(turn)
            self._active_turn = turn
            turn["events"].append(_presentation_row(
                row, group="user" if kind == "user_message_chunk" else ""
            ))
            return
        turn = self._active_turn
        if turn is None:
            return
        if kind in {"question_waiting", "questions_resumed"}:
            turn["status"] = row.get("status", "running")
        if kind == "config_option_update":
            for option in row.get("raw", {}).get("configOptions", []):
                if option.get("category") == "model" or option.get("id") == "model":
                    turn["model"] = str(option.get("currentValue", ""))
        if kind == "turn_stopped":
            turn["events"].append(_presentation_row(row))
            stop_reason = str(row.get("status", "") or "")
            turn["stop_reason"] = stop_reason
            turn["status"] = stop_reason or "stopped"
            self._active_turn = None
            return
        _merge_presentation_row(turn["events"], row)
        replies = [
            str(event.get("text", ""))
            for event in turn["events"]
            if event.get("group") == "assistant"
        ]
        turn["reply"] = replies[-1] if replies else ""

    def _events_path(self) -> Path:
        safe_session = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()[:24]
        return state_root(self.provider, self.cwd) / "sessions" / safe_session / "events.jsonl"

    def _append(self, row: dict[str, Any]) -> None:
        if not self.session_id:
            return
        path = self._events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _json_value(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        return dict(dumped) if isinstance(dumped, dict) else {"value": dumped}
    return dict(value) if isinstance(value, dict) else {"value": str(value)}


def _title(raw: dict[str, Any], kind: str) -> str:
    if raw.get("title"):
        return str(raw["title"])
    if kind == "tool_call_update":
        return ""
    if kind == "plan":
        return "Plan"
    if kind.endswith("message_chunk"):
        return kind.removesuffix("_chunk").replace("_", " ").title()
    if kind == "usage_update":
        return "Usage"
    if kind == "available_commands_update":
        return "Available commands"
    return str(raw.get("name", kind.replace("_", " ").title()))


def _text(raw: dict[str, Any]) -> str:
    value = raw.get("text")
    if isinstance(value, str):
        return value
    content = raw.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text", ""))
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "content":
                block = item.get("content", {})
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
        if texts:
            return "\n".join(texts)
    if raw.get("entries") is not None:
        return json.dumps(raw["entries"], ensure_ascii=False)
    available = raw.get("availableCommands")
    if isinstance(available, list):
        lines: list[str] = []
        for command in available:
            if not isinstance(command, dict):
                continue
            name = str(command.get("name", "")).lstrip("/")
            description = str(command.get("description", "") or "")
            command_input = command.get("input", {})
            hint = str(command_input.get("hint", "") or "") if isinstance(command_input, dict) else ""
            suffix = description
            if hint:
                suffix = f"{suffix} — input: {hint}" if suffix else f"input: {hint}"
            lines.append(f"/{name}" + (f" — {suffix}" if suffix else ""))
        return "\n".join(lines)
    return ""


def _diffs(raw: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in raw.get("content", []) if isinstance(raw.get("content"), list) else []:
        if isinstance(item, dict) and item.get("type") == "diff":
            result.append({
                "path": str(item.get("path", "change.txt")),
                "old_text": str(item.get("oldText", "") or ""),
                "new_text": str(item.get("newText", "") or ""),
            })
    return result


def _terminal_id(raw: dict[str, Any]) -> str:
    for item in raw.get("content", []) if isinstance(raw.get("content"), list) else []:
        if isinstance(item, dict) and item.get("type") == "terminal":
            return str(item.get("terminalId", ""))
    return ""


def _presentation_row(row: dict[str, Any], *, group: str = "") -> dict[str, Any]:
    projected = deepcopy(row)
    projected["group"] = group
    return projected


def _message_group(kind: str) -> str:
    if kind.endswith("thought_chunk"):
        return "thought"
    if kind.endswith("message_chunk") and ("agent" in kind or "assistant" in kind):
        return "assistant"
    return ""


def _merge_presentation_row(events: list[dict[str, Any]], row: dict[str, Any]) -> None:
    kind = str(row.get("type", ""))
    group = _message_group(kind)
    if group and events and events[-1].get("group") == group:
        events[-1]["text"] = str(events[-1].get("text", "")) + str(row.get("text", ""))
        events[-1]["time"] = row.get("time", events[-1].get("time", ""))
        return

    tool_call_id = str(row.get("tool_call_id", "") or "")
    if tool_call_id and kind in {"tool_call", "tool_call_update"}:
        existing = next(
            (event for event in reversed(events) if event.get("tool_call_id") == tool_call_id),
            None,
        )
        if existing is not None:
            _update_projected_row(existing, row)
            existing["group"] = "tool"
            return
        group = "tool"

    terminal_id = str(row.get("terminal_id", "") or "")
    if terminal_id:
        existing = next(
            (event for event in reversed(events) if event.get("terminal_id") == terminal_id),
            None,
        )
        if existing is not None:
            _update_projected_row(existing, row)
            existing["group"] = "terminal"
            return
        group = "terminal"

    events.append(_presentation_row(row, group=group))


def _update_projected_row(existing: dict[str, Any], row: dict[str, Any]) -> None:
    for key in ("time", "type", "title", "status", "text", "tool", "command", "input"):
        value = row.get(key)
        if value not in (None, ""):
            existing[key] = value
    existing.setdefault("raw", {}).update(row.get("raw", {}))
    if row.get("diffs"):
        existing["diffs"] = row["diffs"]


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _command(raw: dict[str, Any]) -> str:
    value = raw.get("rawInput")
    if isinstance(value, dict):
        command = value.get("command", value.get("cmd", ""))
        return _display_value(command)
    return ""
