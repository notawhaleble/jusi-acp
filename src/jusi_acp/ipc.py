"""Bounded private control channel between the Jusi worker and ACP application."""
from __future__ import annotations

import json
import socket
import struct
import threading
from typing import Any, BinaryIO, Callable
from uuid import uuid4

MAX_FRAME_BYTES = 1024 * 1024


def _write_frame(stream: BinaryIO, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("ACP application control frame exceeds 1 MiB")
    stream.write(struct.pack(">I", len(encoded)))
    stream.write(encoded)
    stream.flush()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack(">I", _read_exact(stream, 4))[0]
    if size > MAX_FRAME_BYTES:
        raise ValueError("ACP application control frame exceeds 1 MiB")
    value = json.loads(_read_exact(stream, size).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ACP application control frame must be an object")
    return value


class WorkerApplicationBridge:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(socket_path)
        self._listener.listen(1)
        self._connection: socket.socket | None = None
        self._reader: BinaryIO | None = None
        self._writer: BinaryIO | None = None
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._write_lock = threading.Lock()
        self._condition = threading.Condition()
        self._responses: dict[str, dict[str, Any]] = {}
        self._pending: set[str] = set()
        threading.Thread(target=self._accept, name="jusi-acp-worker-bridge", daemon=True).start()

    def _accept(self) -> None:
        try:
            connection, _ = self._listener.accept()
            self._connection = connection
            self._reader = connection.makefile("rb")
            self._writer = connection.makefile("wb")
            self._connected.set()
            while True:
                response = _read_frame(self._reader)
                request_id = str(response.get("id", ""))
                if request_id:
                    with self._condition:
                        if request_id in self._pending:
                            self._responses[request_id] = response
                            self._condition.notify_all()
        except (EOFError, OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            self._closed.set()
            self._connected.set()
            with self._condition:
                self._condition.notify_all()

    def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._connected.wait(10) or self._writer is None:
            raise RuntimeError("ACP application did not connect")
        request_id = uuid4().hex
        with self._condition:
            self._pending.add(request_id)
        self._send({"id": request_id, "operation": operation, "payload": payload})
        with self._condition:
            while request_id not in self._responses and not self._closed.is_set():
                self._condition.wait()
            response = self._responses.pop(request_id, None)
            self._pending.discard(request_id)
        if response is None:
            raise RuntimeError("ACP application disconnected")
        return response

    def interrupt(self) -> None:
        if self._writer is None or self._closed.is_set():
            return
        self._send({"id": uuid4().hex, "operation": "interrupt", "payload": {}})

    def _send(self, message: dict[str, Any]) -> None:
        writer = self._writer
        if writer is None:
            raise RuntimeError("ACP application is unavailable")
        with self._write_lock:
            _write_frame(writer, message)

    def close(self) -> None:
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass
        connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        try:
            import os
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        with self._condition:
            self._condition.notify_all()


class ApplicationController:
    def __init__(self, socket_path: str, handler: Callable[[str, dict[str, Any]], dict[str, Any]]) -> None:
        self._handler = handler
        self._connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._connection.connect(socket_path)
        self._reader = self._connection.makefile("rb")
        self._writer = self._connection.makefile("wb")
        self._write_lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._read, name="jusi-acp-application-control", daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                message = _read_frame(self._reader)
                operation = str(message.get("operation", ""))
                if operation == "interrupt":
                    self._handle(message)
                else:
                    if operation == "followup":
                        try:
                            self._handler("begin_followup", {})
                        except BaseException as exc:
                            self._respond(
                                message,
                                {"ok": False, "error": "rejected", "message": str(exc)},
                            )
                            continue
                    threading.Thread(
                        target=self._handle,
                        args=(message,),
                        name=f"jusi-acp-{operation}",
                        daemon=True,
                    ).start()
        except (EOFError, OSError, ValueError, json.JSONDecodeError):
            return

    def _handle(self, message: dict[str, Any]) -> None:
        operation = str(message.get("operation", ""))
        payload = message.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        try:
            response = {"ok": True, "result": self._handler(operation, payload)}
        except InterruptedError as exc:
            response = {"ok": False, "error": "interrupted", "message": str(exc)}
        except ValueError as exc:
            response = {"ok": False, "error": "rejected", "message": str(exc)}
        except BaseException as exc:
            response = {"ok": False, "error": "fatal", "message": f"{type(exc).__name__}: {exc}"}
        self._respond(message, response)

    def _respond(self, message: dict[str, Any], response: dict[str, Any]) -> None:
        value = {"id": str(message.get("id", "")), **response}
        try:
            with self._write_lock:
                _write_frame(self._writer, value)
        except (OSError, ValueError):
            return
