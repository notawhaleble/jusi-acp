"""Deliver completed replies through the owning client's Markdown overlay."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from queue import SimpleQueue
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Callable

from jusi.editor_client import EditorDeliveryError, close_document, open_document

from .ui import queue_action


class MarkdownReplies:
    def __init__(self) -> None:
        self._queue: SimpleQueue = SimpleQueue()
        self._thread: Thread | None = None
        self._current: object | None = None

    def present(self, reply: str, fallback: Callable[[], None]) -> None:
        if not os.environ.get("JUSI_EDITOR_ACTION_SOCKET"):
            fallback()
            return
        ticket = object()
        self._current = ticket

        def show_fallback() -> None:
            if self._current is ticket:
                self._current = None
                fallback()

        if self._thread is None:
            self._thread = Thread(target=self._deliver, name="acp-markdown", daemon=True)
            self._thread.start()
        self._queue.put(("open", reply, show_fallback))

    def dismiss(self) -> None:
        if self._current is not None:
            self._current = None
            self._queue.put(("close", "", None))

    def stop(self) -> None:
        self._current = None
        self._queue.put(None)

    def _deliver(self) -> None:
        opened = False
        while (work := self._queue.get()) is not None:
            operation, reply, fallback = work
            if operation == "open":
                try:
                    with TemporaryDirectory(prefix="jusi-acp-reply-") as directory:
                        path = Path(directory) / "reply.md"
                        path.write_text(reply, encoding="utf-8")
                        open_document(path, media_type="text/markdown", provider="markdown",
                                      slot="acp-reply", lifetime="client")
                    opened = True
                except (EditorDeliveryError, OSError, ValueError) as exc:
                    logging.getLogger(__name__).debug("Markdown reply unavailable: %s", exc)
                    queue_action(fallback)
            elif opened:
                try:
                    close_document(slot="acp-reply")
                except EditorDeliveryError as exc:
                    logging.getLogger(__name__).warning("Cannot close ACP reply: %s", exc)
                opened = False
