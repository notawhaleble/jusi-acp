"""Normalized, read-only presentation cache for ACP sessions."""
from __future__ import annotations

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
    _lock: Lock = field(default_factory=Lock, repr=False)

    def bind_session(self, session_id: str, *, load_cache: bool) -> None:
        self.session_id = session_id
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
            "terminal_id": _terminal_id(raw),
            "diffs": _diffs(raw),
            "raw": raw,
            "cached": False,
        }
        with self._lock:
            self.rows.append(row)
        self._append(row)
        return row

    def add_status(self, title: str, text: str = "", status: str = "") -> dict[str, Any]:
        return self.add({"kind": "status", "title": title, "text": text, "status": status}, source="client")

    def update_terminal(self, terminal_id: str, output: str, truncated: bool, status: str = "") -> None:
        with self._lock:
            existing = next((row for row in reversed(self.rows) if row.get("terminal_id") == terminal_id), None)
            if existing is None:
                existing = {
                    "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": "client",
                    "type": "terminal",
                    "title": f"Terminal {terminal_id}",
                    "status": status,
                    "text": output,
                    "tool_call_id": "",
                    "terminal_id": terminal_id,
                    "diffs": [],
                    "raw": {},
                    "cached": False,
                }
                self.rows.append(existing)
            else:
                existing["text"] = output
                existing["status"] = status
                existing["title"] = f"Terminal {terminal_id}" + (" (truncated)" if truncated else "")

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
