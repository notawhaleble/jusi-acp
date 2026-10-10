"""Client attention delivery and conservative background-tool tracking."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from queue import Queue
import threading
from typing import Any

from jusi import editor_client


class AttentionPublisher:
    """Serialize bounded socket calls outside the UI and ACP event loop.

    A clear queued while creation is in flight runs after its ID is known.
    Failed creation is remembered, never retried by a later question update.
    """

    def __init__(self) -> None:
        self._enabled = bool(os.environ.get("JUSI_EDITOR_ACTION_SOCKET"))
        self._messages: dict[str, str] = {}
        self._queue: Queue[tuple[str, str, str]] = Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None

    def sync_requests(self, messages: dict[str, str]) -> None:
        for key in self._messages.keys() - messages.keys():
            self._submit("clear", key)
        for key, message in messages.items():
            if key not in self._messages:
                self._submit("request", key, message)
            elif self._messages[key] != message:
                self._submit("update", key, message)
        self._messages = dict(messages)

    def notice(self, message: str) -> None:
        self._submit("notice", message=message)

    def reset(self) -> None:
        self._messages.clear()
        self._submit("reset")

    def _submit(self, operation: str, key: str = "", message: str = "") -> None:
        with self._lock:
            if not self._enabled or self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="acp-attention", daemon=True)
                self._thread.start()
            self._queue.put((operation, key, message))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._thread is not None:
                    self._queue.put(("stop", "", ""))

    def _run(self) -> None:
        requests: dict[str, str] = {}
        notices: set[str] = set()
        while True:
            operation, key, message = self._queue.get()
            try:
                if operation in {"reset", "stop"}:
                    for attention_id in set(requests.values()) | notices:
                        self._clear(attention_id)
                    requests.clear()
                    notices.clear()
                    if operation == "stop":
                        return
                elif self._closed:
                    continue
                elif operation == "request":
                    requests[key] = editor_client.request_attention(kind="action_required", message=message)
                elif operation == "update" and key in requests:
                    editor_client.update_attention(requests[key], kind="action_required", message=message)
                elif operation == "clear" and key in requests:
                    self._clear(requests.pop(key))
                elif operation == "notice":
                    notices.add(editor_client.request_attention(kind="notice", message=message))
            except Exception as exc:
                # Delivery failures must not fail an ACP request or retry a
                # creation whose acceptance may simply have been lost.
                logging.getLogger(__name__).warning("Jusi attention %s failed: %s", operation, exc)
            finally:
                self._queue.task_done()

    @staticmethod
    def _clear(attention_id: str) -> None:
        try:
            editor_client.clear_attention(attention_id)
        except Exception as exc:
            logging.getLogger(__name__).warning("Jusi attention clear failed: %s", exc)


@dataclass
class _Tool:
    turn: int
    status: str
    background: bool = False
    finished: bool = False


class BackgroundTools:
    """Recognize live background tool lifecycles and Qwen completion metadata."""

    def __init__(self) -> None:
        self._tools: dict[str, _Tool] = {}
        self._notified_tasks: set[str] = set()
        self._notified_tools: set[str] = set()

    def update(self, raw: dict[str, Any], turn: int | None) -> str | None:
        kind, key = raw.get("sessionUpdate"), raw.get("toolCallId")
        if kind == "agent_message_chunk":
            return self._background_message(raw)
        if kind not in {"tool_call", "tool_call_update"} or not key:
            return None
        tool = self._tools.get(key)
        if tool is None:
            if kind == "tool_call" and turn is not None:
                self._tools[key] = _Tool(turn, raw.get("status") or "pending")
            return None
        status = raw.get("status")
        if not status:
            return None
        previous = tool.status
        tool.status = status
        if tool.finished or status not in {"completed", "failed", "cancelled", "canceled"}:
            return None
        tool.finished = True
        if tool.background and previous == "in_progress" and status in {"completed", "failed"}:
            if key in self._notified_tools:
                return None
            self._notified_tools.add(key)
            return status
        return None

    def _background_message(self, raw: dict[str, Any]) -> str | None:
        # Qwen/GigaCode completes the launching tool immediately and reports
        # the actual result later through two discrete messages for one task.
        # Only its explicit extension metadata qualifies; never inspect text.
        metadata = raw.get("_meta")
        if not isinstance(metadata, dict) or metadata.get("source") not in (
            "background_task_completed", "background_notification"
        ):
            return None
        task = metadata.get("backgroundTask")
        if not isinstance(task, dict):
            return None
        task_id, status = task.get("taskId"), task.get("status")
        if not isinstance(task_id, str) or not task_id.strip() or status not in ("completed", "failed"):
            return None
        tool_id = task.get("toolUseId")
        if not isinstance(tool_id, str):
            tool_id = ""
        duplicate = task_id in self._notified_tasks or bool(tool_id and tool_id in self._notified_tools)
        self._notified_tasks.add(task_id)
        if tool_id:
            self._notified_tools.add(tool_id)
        return None if duplicate else status

    def finish_turn(self, turn: int, *, cancelled: bool = False) -> None:
        for tool in self._tools.values():
            if tool.turn == turn:
                tool.background = not cancelled and tool.status == "in_progress" and not tool.finished
