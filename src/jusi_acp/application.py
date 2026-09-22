"""Shared interactive ACP client application hosted in a Jusi terminal surface."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sys
import threading
import time
from typing import Any
from uuid import uuid4

from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process
from acp.schema import (
    AuthCapabilities,
    ClientCapabilities,
    CurrentModeUpdate,
    FileSystemCapabilities,
    Implementation,
    RequestPermissionResponse,
    TextContentBlock,
)

from . import __version__
from .compat import MODE_NOTIFICATIONS, install_mode_notifications
from .ipc import ApplicationController
from .state import EventStore
from .questions import QuestionRequest, question_text
from .terminal import TerminalManager
from .ui import (
    PendingPermission,
    install_api,
    make_diffs_sheet,
    make_events_sheet,
    make_permission_sheet,
    make_questions_sheet,
    make_sessions_sheet,
    make_turns_sheet,
    queue_action,
)


class ACPApplication:
    def __init__(self, payload: dict[str, Any]) -> None:
        launch = payload.get("launch", {})
        submission = payload.get("submission", {})
        if not isinstance(launch, dict) or not isinstance(submission, dict):
            raise ValueError("Invalid ACP application payload")
        argv = launch.get("argv", [])
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("Invalid ACP agent command")
        self.argv = tuple(argv)
        self.cwd = Path(str(launch.get("cwd", "")))
        if not self.cwd.is_absolute():
            raise ValueError("ACP agent cwd must be absolute")
        environment = launch.get("environment", {})
        if not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                     for k, v in environment.items()):
            raise ValueError("Invalid ACP agent environment")
        self.environment = dict(environment)
        self.auth_method = str(launch.get("auth_method", "")).strip()
        self.plugin_id = str(payload.get("plugin_id", ""))
        self.alias = str(submission.get("alias", "acp"))
        self.initial_body = str(submission.get("body", ""))
        self.session_action = str(submission.get("session_action", "new"))
        self.requested_session_id = str(submission.get("session_id", ""))
        self.additional_directories = tuple(
            Path(str(item)) for item in submission.get("additional_directories", [])
        )
        self.mcp_servers = submission.get("mcp_servers", [])
        if not isinstance(self.mcp_servers, list):
            raise ValueError("mcp_servers must be an array")

        self.store = EventStore(self.plugin_id, self.cwd)
        self.sheet: Any = None
        self.turns_sheet: Any = None
        self.live_sheet: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connection: Any = None
        self.initialize_response: Any = None
        self.session_id = ""
        self._browse_sessions = False
        self._reported_model = ""
        self.session_modes: Any = None
        self._current_mode_id = ""
        self.config_options: list[Any] = []
        self.available_commands: list[Any] = []
        self._prompt_lock: asyncio.Lock | None = None
        self._turn_task: asyncio.Task[dict[str, Any]] | None = None
        self._questions: dict[str, QuestionRequest] = {}
        self._question_changed = asyncio.Event()
        self._answer_state: dict[str, Any] | None = None
        self.questions_sheet: Any = None
        self.sessions_sheet: Any = None
        self._shutdown: asyncio.Event | None = None
        self._connected = threading.Event()
        self._failed: BaseException | None = None
        self._cancel_requested = threading.Event()
        self._operation_lock = threading.Lock()
        self._operation_cancel: threading.Event | None = None
        self._permission_lock = threading.Lock()
        self._permissions: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[Any]]] = {}
        self._refresh_lock = threading.Lock()
        self._refresh_scheduled = False
        self._refresh_dirty = False
        self._refresh_follow_tail = False
        self._last_refresh = 0.0
        roots = (self.cwd, *self.additional_directories)
        self.terminals = TerminalManager(roots, observer=self._terminal_changed)
        self._thread: threading.Thread | None = None

    # ACP client handlers -------------------------------------------------
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        _ = kwargs
        if self.session_id and session_id != self.session_id:
            return
        raw = update.model_dump(mode="json", by_alias=True, exclude_none=True)
        kind = raw.get("sessionUpdate")
        if kind == "available_commands_update":
            self.available_commands = list(getattr(update, "available_commands", []))
        elif kind == "current_mode_update":
            current = getattr(update, "current_mode_id", "")
            self._current_mode_id = current
            if self.session_modes is not None:
                self.session_modes.current_mode_id = current
        elif kind == "config_option_update":
            self.config_options = list(getattr(update, "config_options", []))
        self.store.add(update)
        self._refresh(follow_tail=True)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> RequestPermissionResponse:
        _ = kwargs
        if session_id != self.session_id:
            return RequestPermissionResponse(outcome={"outcome": "cancelled"})
        request_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        with self._permission_lock:
            self._permissions[request_id] = (loop, future)
        locations = ", ".join(
            str(getattr(location, "path", "")) for location in (getattr(tool_call, "locations", None) or [])
        )
        raw_input = getattr(tool_call, "raw_input", None)
        questions = raw_input.get("questions") if isinstance(raw_input, dict) else None
        if not isinstance(questions, list) or not questions or not all(isinstance(q, dict) for q in questions):
            questions = None
        pending = PendingPermission(
            request_id=request_id,
            title=str(getattr(tool_call, "title", None) or getattr(tool_call, "tool_call_id", "Tool call")),
            kind=str(getattr(tool_call, "kind", "") or ""),
            locations=locations,
            options=options,
            questions=questions,
            details=json.dumps(tool_call.model_dump(mode="json", by_alias=True, exclude_none=True),
                               ensure_ascii=False, indent=2),
        )
        self.store.add_status("Answers required" if questions else "Permission required", pending.details, "pending")
        self._refresh()
        if questions:
            option = next((option for option in options if option.kind == "allow_once"), None)
            if option is None:
                with self._permission_lock:
                    self._permissions.pop(request_id, None)
                raise RequestError.invalid_params({"details": "Questions require an allow_once response option"})
            self._questions[request_id] = QuestionRequest(request_id, questions, option.option_id)
            self._publish_question()
        else:
            queue_action(lambda: self._push_permission(pending))
        try:
            option_id = await future
        finally:
            with self._permission_lock:
                self._permissions.pop(request_id, None)
            if self._questions.pop(request_id, None) is not None:
                self._publish_question()
        if option_id is None:
            return RequestPermissionResponse(outcome={"outcome": "cancelled"})
        if isinstance(option_id, dict):
            return QuestionPermissionResponse(
                outcome={"outcome": "selected", "optionId": option_id["option_id"]},
                answers=option_id["answers"],
            )
        return RequestPermissionResponse(outcome={"outcome": "selected", "optionId": option_id})

    async def create_terminal(
        self, session_id: str, command: str, args: list[str] | None = None,
        env: list[Any] | None = None, cwd: str | None = None,
        output_byte_limit: int | None = None, **kwargs: Any,
    ) -> Any:
        _ = kwargs
        result = await self.terminals.create(session_id, command, args, env, cwd, output_byte_limit)
        self.store.update_terminal(result.terminal_id, "", False, "running",
                                   command=shlex.join([command, *(args or [])]))
        self._refresh()
        return result

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        _ = kwargs
        return await self.terminals.output(session_id, terminal_id)

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        _ = kwargs
        return await self.terminals.wait(session_id, terminal_id)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        _ = kwargs
        await self.terminals.kill(session_id, terminal_id)

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        _ = kwargs
        await self.terminals.release(session_id, terminal_id)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in MODE_NOTIFICATIONS:
            await self._mode_notification(params)

    async def _mode_notification(self, params: Any) -> None:
        if not isinstance(params, dict) or params.get("v", 1) != 1:
            raise RequestError.invalid_params({"details": "Expected a version 1 mode notification"})
        session_id = params.get("sessionId")
        mode_id = params.get("currentModeId")
        if not isinstance(session_id, str) or not session_id or not isinstance(mode_id, str) or not mode_id:
            raise RequestError.invalid_params({"details": "Mode notification requires sessionId and currentModeId"})
        if self.session_id and session_id != self.session_id:
            return
        # Qwen often emits this immediately after the standard session update.
        # Also support the vendor notification alone, without trusting its
        # legacyFrameSent flag as evidence that we received the standard frame.
        if mode_id == self._current_mode_id:
            return
        await self.session_update(session_id, CurrentModeUpdate(
            session_update="current_mode_update", current_mode_id=mode_id,
        ))

    def _observe_protocol(self, event: Any) -> None:
        # Older ACP agents return models, which this SDK version discards while
        # validating session responses. Preserve the reported ID at the boundary.
        if event.direction != "incoming":
            return
        result = event.message.get("result")
        if isinstance(result, dict):
            models = result.get("models")
            if isinstance(models, dict) and isinstance(models.get("currentModelId"), str):
                self._reported_model = models["currentModelId"]

    def on_connect(self, connection: Any) -> None:
        install_mode_notifications(connection, self._mode_notification)
        self.connection = connection

    # Application lifecycle ---------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="jusi-acp-runtime", daemon=True)
        self._thread.start()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._async_main())
        except BaseException as exc:
            self._failed = exc
            self.store.add_status("ACP runtime failed", f"{type(exc).__name__}: {exc}", "failed")
            if self.store.active_turn is not None:
                self.store.add({"kind": "turn_stopped", "stopReason": "failed"}, source="client")
            self._refresh()
            queue_action(self._focus_turns_sheet)
            queue_action(lambda exc=exc: _raise_exception(exc))
        finally:
            self._connected.set()

    async def _async_main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(self._handle_asyncio_exception)
        self._prompt_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()
        capabilities = ClientCapabilities(
            fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
            terminal=True,
            auth=AuthCapabilities(terminal=False),
        )
        async with spawn_agent_process(
            self,
            self.argv[0],
            *self.argv[1:],
            env={**_agent_environment(), **self.environment},
            cwd=self.cwd,
            observers=[self._observe_protocol],
        ) as (connection, process):
            self.connection = connection
            stderr_task = asyncio.create_task(self._drain_stderr(process))
            try:
                initialized = await connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=capabilities,
                    client_info=Implementation(name="jusi-acp", title="Jusi ACP", version=__version__),
                )
                if initialized.protocol_version != PROTOCOL_VERSION:
                    raise RuntimeError(
                        f"ACP protocol mismatch: agent selected {initialized.protocol_version}, client supports {PROTOCOL_VERSION}"
                    )
                self.initialize_response = initialized
                auth_names = [f"{item.id}: {item.name}" for item in initialized.auth_methods or []]
                if auth_names:
                    self.store.add_status("Authentication methods", "; ".join(auth_names))
                if self.auth_method:
                    await self._authenticate(self.auth_method)
                browse_sessions = _normalize_followup_body(self.initial_body).strip() == "/sessions"
                self._browse_sessions = browse_sessions
                if browse_sessions:
                    try:
                        await self._list_sessions()
                    except (RequestError, ValueError) as exc:
                        self.store.add_status("Session listing failed", str(exc), "failed")
                else:
                    try:
                        await self._establish_session()
                    except RequestError as exc:
                        if not initialized.auth_methods:
                            raise
                        self.store.add_status(
                            "Authentication required", f"{exc}; use /auth METHOD_ID", "blocked"
                        )
                self._connected.set()
                if self.initial_body.strip() and not browse_sessions:
                    try:
                        await self._prompt_or_command(self.initial_body, initial=True)
                    except InterruptedError:
                        self.store.add_status("Initial turn cancelled", status="cancelled")
                    except ValueError as exc:
                        self.store.add_status("Initial command rejected", str(exc), "failed")
                    except RequestError as exc:
                        self.store.add_status("Initial turn rejected", str(exc), "failed")
                await self._shutdown.wait()
                await self._close_agent_session()
            finally:
                if self._turn_task is not None and not self._turn_task.done():
                    self._turn_task.cancel()
                    await asyncio.gather(self._turn_task, return_exceptions=True)
                await self.terminals.close()
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

    async def _drain_stderr(self, process: Any) -> None:
        stream = process.stderr
        if stream is None:
            return
        retained = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            retained.extend(chunk)
            if len(retained) > 65536:
                del retained[:-65536]
            self.store.add_status("ACP agent stderr", chunk.decode("utf-8", errors="replace"), "diagnostic")
            self._refresh()
        if process.returncode not in (None, 0) and retained:
            self.store.add_status("ACP agent stderr", retained.decode("utf-8", errors="replace"), "failed")

    async def _establish_session(self) -> None:
        assert self.connection is not None
        additional = [str(path) for path in self.additional_directories]
        caps = self.initialize_response.agent_capabilities
        session_caps = caps.session_capabilities if caps else None
        if additional and (session_caps is None or session_caps.additional_directories is None):
            raise ValueError("ACP agent does not support additional session directories")
        if self.session_action == "load":
            if not caps or not caps.load_session:
                raise ValueError("ACP agent does not support session/load")
            self.session_id = self.requested_session_id
            self.store.bind_session(self.session_id, load_cache=False)
            response = await self.connection.load_session(
                cwd=str(self.cwd), session_id=self.requested_session_id,
                additional_directories=additional, mcp_servers=self.mcp_servers,
            )
            self.store.finish_replay()
            self._capture_session_options(response)
        elif self.session_action == "resume":
            if session_caps is None or session_caps.resume is None:
                raise ValueError("ACP agent does not support session/resume")
            self.session_id = self.requested_session_id
            self.store.bind_session(self.session_id, load_cache=True)
            response = await self.connection.resume_session(
                cwd=str(self.cwd), session_id=self.requested_session_id,
                additional_directories=additional, mcp_servers=self.mcp_servers,
            )
            self._capture_session_options(response)
        else:
            response = await self.connection.new_session(
                cwd=str(self.cwd), additional_directories=additional, mcp_servers=self.mcp_servers,
            )
            self.session_id = response.session_id
            self.store.bind_session(self.session_id, load_cache=False)
            self._capture_session_options(response)
        self.store.add_status("ACP session ready", self.session_id, "ready")
        self._refresh()

    async def _list_sessions(self) -> list[dict[str, Any]]:
        assert self.connection is not None
        caps = self.initialize_response.agent_capabilities
        session_caps = caps.session_capabilities if caps else None
        if session_caps is None or session_caps.list is None:
            raise ValueError("ACP agent does not support session/list")
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await self.connection.list_sessions(cwd=str(self.cwd), cursor=cursor)
            rows.extend({
                "session_id": item.session_id,
                "title": item.title or item.session_id,
                "cwd": item.cwd,
                "updated_at": item.updated_at or "",
            } for item in response.sessions)
            cursor = response.next_cursor
            if not cursor:
                break
        queue_action(lambda rows=rows: self._push_sessions(rows))
        return rows

    async def _select_session(self, session_id: str) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            raise ValueError("Cannot switch sessions while an ACP turn is running")
        if self.session_id and self.session_id != session_id:
            await self._close_agent_session()
        self.requested_session_id = session_id
        caps = self.initialize_response.agent_capabilities
        if caps and caps.load_session:
            self.session_action = "load"
        else:
            session_caps = caps.session_capabilities if caps else None
            if session_caps is None or session_caps.resume is None:
                raise ValueError("ACP agent cannot load or resume the selected session")
            self.session_action = "resume"
        await self._establish_session()
        self._browse_sessions = False
        queue_action(self._focus_turns_sheet)

    def select_session(self, session_id: str) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self._select_session(session_id), self.loop)

        def finished(result: ConcurrentFuture[Any]) -> None:
            error = result.exception()
            if error is None:
                return
            self.store.add_status(
                "Session selection failed", f"{type(error).__name__}: {error}", "failed"
            )
            self._refresh()

        future.add_done_callback(finished)

    def _capture_session_options(self, response: Any) -> None:
        self.session_modes = getattr(response, "modes", None)
        if self.session_modes is not None:
            self._current_mode_id = self.session_modes.current_mode_id
        self.config_options = list(getattr(response, "config_options", None) or [])

    async def _close_agent_session(self) -> None:
        if not self.connection or not self.session_id or not self.initialize_response:
            return
        caps = self.initialize_response.agent_capabilities
        session_caps = caps.session_capabilities if caps else None
        if session_caps is not None and session_caps.close is not None:
            try:
                await self.connection.close_session(self.session_id)
            except Exception:
                pass

    # Worker operations --------------------------------------------------
    def handle_operation(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "begin_followup":
            with self._operation_lock:
                if self._operation_cancel is not None:
                    raise ValueError("Another ACP follow-up is active")
                self._operation_cancel = threading.Event()
            return {"begun": True}
        if operation == "interrupt":
            self.cancel_initial()
            return {"requested": True}
        if operation == "complete":
            return {"items": self._completion_items(payload)}
        if operation != "followup":
            raise ValueError(f"Unsupported ACP application operation: {operation}")
        body = _normalize_followup_body(str(payload.get("body", "")))
        with self._operation_lock:
            if self._operation_cancel is None:
                self._operation_cancel = threading.Event()
        try:
            return self._submit(self._prompt_or_command(body))
        finally:
            with self._operation_lock:
                self._operation_cancel = None

    def _submit(self, coroutine: Any) -> dict[str, Any]:
        try:
            if not self._connected.wait(10):
                raise RuntimeError("ACP agent did not become ready")
            if self._failed is not None:
                raise RuntimeError(f"ACP runtime failed: {self._failed}")
            if self.loop is None or self.loop.is_closed():
                raise RuntimeError("ACP runtime loop is unavailable")
        except BaseException:
            close = getattr(coroutine, "close", None)
            if close is not None:
                close()
            raise
        future: ConcurrentFuture[dict[str, Any]] = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()

    async def _prompt_or_command(self, body: str, *, initial: bool = False) -> dict[str, Any]:
        stripped = body.strip()
        with self._operation_lock:
            cancelled = self._operation_cancel is not None and self._operation_cancel.is_set()
        if cancelled:
            raise InterruptedError("ACP turn cancelled")
        if stripped == "/cancel":
            self.cancel_initial()
            if self._turn_task is not None and not self._turn_task.done():
                try:
                    await asyncio.shield(self._turn_task)
                except InterruptedError:
                    pass
            return {"command": "cancel"}
        if self._answer_state is not None:
            return await self._answer_question(body)
        if stripped == "/sessions":
            if self._turn_task is not None and not self._turn_task.done():
                raise ValueError("Cannot list sessions while an ACP turn is running")
            self._browse_sessions = True
            rows = await self._list_sessions()
            return {"command": "sessions", "count": len(rows)}
        if self._turn_task is not None and not self._turn_task.done():
            raise ValueError("The ACP turn is still running; wait for a question or turn completion")
        if stripped.startswith("/mode "):
            if self.session_modes is None:
                raise ValueError("ACP agent does not advertise session modes")
            parts = shlex.split(stripped)
            if len(parts) != 2:
                raise ValueError("usage: /mode ID")
            mode_id = parts[1]
            await self.connection.set_session_mode(self.session_id, mode_id)
            return {"command": "mode", "mode": mode_id}
        if stripped.startswith("/config "):
            if not self.config_options:
                raise ValueError("ACP agent does not advertise session configuration options")
            parts = shlex.split(stripped)
            if len(parts) != 3:
                raise ValueError("usage: /config ID VALUE")
            value: str | bool = parts[2]
            if parts[2].lower() in {"true", "false"}:
                value = parts[2].lower() == "true"
            response = await self.connection.set_config_option(parts[1], self.session_id, value)
            self.config_options = list(response.config_options)
            return {"command": "config", "config_id": parts[1], "value": value}
        if stripped.startswith("/auth "):
            parts = shlex.split(stripped)
            if len(parts) != 2:
                raise ValueError("usage: /auth METHOD_ID")
            await self._authenticate(parts[1])
            if not self.session_id:
                if self._browse_sessions:
                    await self._list_sessions()
                else:
                    await self._establish_session()
            return {"command": "auth", "method_id": parts[1]}
        if not initial:
            self._cancel_requested.clear()
        self._turn_task = asyncio.create_task(self._prompt(body, initial=initial))
        # A paused turn outlives its Jusi operation. Consume unattended task
        # exceptions; _prompt already records failures in the turn journal.
        self._turn_task.add_done_callback(self._turn_finished)
        return await self._wait_for_turn()

    def _turn_finished(self, task: asyncio.Task[dict[str, Any]]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not isinstance(error, (InterruptedError, RequestError, ValueError)):
            self._failed = error

    async def _wait_for_turn(self) -> dict[str, Any]:
        task = self._turn_task
        assert task is not None
        while not task.done():
            if self._answer_state is not None and not self._cancel_requested.is_set():
                return dict(self._answer_state)
            self._question_changed.clear()
            changed = asyncio.create_task(self._question_changed.wait())
            try:
                await asyncio.wait((task, changed), return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
        return await asyncio.shield(task)

    def _publish_question(self) -> None:
        with self._permission_lock:
            waiting = {key for key, (_, future) in self._permissions.items() if not future.done()}
        request = next((value for key, value in self._questions.items() if key in waiting), None)
        self._answer_state = request.snapshot() if request is not None else None
        self._question_changed.set()
        state = self._answer_state
        if state is not None:
            self.store.add({"kind": "question_waiting", "title": "Answer in Jusi follow-up",
                            "text": question_text(state), "status": "awaiting_answer"}, source="client")
            queue_action(lambda: self._show_question(state))
        else:
            self.store.add({"kind": "questions_resumed", "title": "Questions finished",
                            "status": "running"}, source="client")
            queue_action(self._close_questions_sheet)
        self._refresh()

    async def _answer_question(self, body: str) -> dict[str, Any]:
        if not body.strip():
            raise ValueError("The answer is empty; write it in the cell and submit a follow-up")
        state = self._answer_state
        assert state is not None
        request = self._questions[state["request_id"]]
        with self._permission_lock:
            waiting = self._permissions.get(request.request_id)
        if waiting is None or waiting[1].done():
            raise ValueError("This question is no longer waiting for an answer")
        request.answers[str(len(request.answers))] = body
        self.store.add({"kind": "question_answer", "title": f"Answer {state['question_number']}",
                        "text": body, "question": state["question"]}, source="user")
        if len(request.answers) == len(request.questions):
            self._questions.pop(request.request_id)
            _resolve_future(waiting[1], {"option_id": request.option_id, "answers": dict(request.answers)})
        self._publish_question()
        return await self._wait_for_turn()

    async def _prompt(self, body: str, *, initial: bool) -> dict[str, Any]:
        if not self.session_id:
            raise ValueError("ACP session is not ready; authenticate first if required")
        assert self._prompt_lock is not None
        async with self._prompt_lock:
            with self._operation_lock:
                operation_cancel = self._operation_cancel
            if initial and self._cancel_requested.is_set():
                raise InterruptedError("ACP turn cancelled")
            if not initial and operation_cancel is not None and operation_cancel.is_set():
                raise InterruptedError("ACP turn cancelled")
            self.store.add({"kind": "user_prompt", "text": body, "model": self._current_model()}, source="user")
            turn = self.store.active_turn
            if turn is not None:
                turn_number = int(turn["turn"])
                queue_action(lambda: self._show_live_turn(turn_number))
            self._refresh(follow_tail=True)
            try:
                response = await self.connection.prompt(
                    self.session_id, [TextContentBlock(type="text", text=body)]
                )
            except Exception as exc:
                self.store.add({
                    "kind": "turn_error",
                    "title": "Turn failed",
                    "text": f"{type(exc).__name__}: {exc}",
                    "status": "failed",
                }, source="client")
                self.store.add({"kind": "turn_stopped", "stopReason": "failed"}, source="client")
                self._refresh()
                queue_action(self._focus_turns_sheet)
                raise
            finally:
                # Agents can stop a turn while a client request is outstanding.
                # Never leave an answer target attached to a completed turn.
                if self._questions:
                    request_ids = list(self._questions)
                    self._questions.clear()
                    with self._permission_lock:
                        futures = [self._permissions[key][1] for key in request_ids if key in self._permissions]
                    for future in futures:
                        _resolve_future(future, None)
                    self._publish_question()
            stop_reason = str(response.stop_reason)
            self.store.add({"kind": "turn_stopped", "stopReason": stop_reason}, source="agent")
            self._refresh()
            queue_action(self._focus_turns_sheet)
            cancelled = (
                self._cancel_requested.is_set()
                if initial
                else operation_cancel is not None and operation_cancel.is_set()
            ) or self._cancel_requested.is_set() or stop_reason in {"cancelled", "canceled"}
            if cancelled:
                raise InterruptedError("ACP turn cancelled")
            return {"stop_reason": stop_reason, "session_id": self.session_id}

    def _current_model(self) -> str:
        for option in self.config_options:
            if getattr(option, "category", None) == "model" or getattr(option, "id", None) == "model":
                return str(getattr(option, "current_value", "") or "")
        return self._reported_model

    async def _authenticate(self, method_id: str) -> None:
        methods = {item.id: item for item in self.initialize_response.auth_methods or []}
        method = methods.get(method_id)
        if method is None:
            raise ValueError(f"Unknown ACP authentication method {method_id!r}")
        method_type = getattr(method, "type", None)
        if method_type is not None:
            raise ValueError(f"ACP authentication method type {method_type!r} is not supported by jusi-acp 0.1")
        self.store.add_status("Authenticating", method_id, "pending")
        self._refresh()
        await self.connection.authenticate(method_id)
        self.store.add_status("Authenticated", method_id, "ready")
        self._refresh()

    def cancel_initial(self) -> None:
        self._cancel_requested.set()
        with self._operation_lock:
            if self._operation_cancel is not None:
                self._operation_cancel.set()
        self._cancel_permissions()
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._cancel()))

    async def _cancel(self) -> None:
        if self.connection is not None and self.session_id:
            await self.connection.cancel(self.session_id)

    def close(self) -> None:
        self._cancel_permissions()
        loop = self.loop
        shutdown = self._shutdown
        if loop is not None and not loop.is_closed() and shutdown is not None:
            loop.call_soon_threadsafe(shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=4)

    # UI and feedback ----------------------------------------------------
    def select_permission(self, request_id: str, option_id: str | dict[str, Any] | None) -> None:
        with self._permission_lock:
            pending = self._permissions.get(request_id)
        if pending is None:
            return
        loop, future = pending
        loop.call_soon_threadsafe(_resolve_future, future, option_id)

    def _cancel_permissions(self) -> None:
        with self._permission_lock:
            pending = list(self._permissions.values())
        for loop, future in pending:
            loop.call_soon_threadsafe(_resolve_future, future, None)

    def _push_permission(self, pending: PendingPermission) -> None:
        with self._permission_lock:
            waiting = self._permissions.get(pending.request_id)
        if waiting is None or waiting[1].done():
            return
        from visidata import vd
        vd.push(make_permission_sheet(self, pending))

    def _push_sessions(self, rows: list[dict[str, Any]]) -> None:
        from visidata import vd
        self.sessions_sheet = make_sessions_sheet(self, rows)
        vd.push(self.sessions_sheet)

    def _show_question(self, state: dict[str, Any]) -> None:
        if state is not self._answer_state:
            return
        from visidata import vd
        self._close_questions_sheet()
        self.questions_sheet = make_questions_sheet(self, state)
        vd.push(self.questions_sheet)

    def _close_questions_sheet(self) -> None:
        if self.questions_sheet is not None:
            from visidata import vd
            if self.questions_sheet in vd.sheets:
                vd.remove(self.questions_sheet)
            self.questions_sheet = None

    def _completion_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        prefix = str(payload.get("prefix", ""))
        cursor = int(payload.get("cursor_pos", len(prefix)))
        state = self._answer_state
        if state is not None:
            choices = [(str(option.get("label", "")), str(option.get("description", "")))
                       for option in state["options"]]
            choices.append(("/cancel", "Cancel the waiting turn"))
            return [{"text": label, "label": label, "detail": detail, "kind": "text",
                     "start": 0, "end": cursor}
                    for label, detail in choices if label and label.casefold().startswith(prefix.casefold())]
        start = prefix.rfind("/")
        if start < 0 or (start > 0 and not prefix[start - 1].isspace()):
            return []
        typed = prefix[start + 1:]
        commands = [("cancel", "Cancel the active ACP turn")]
        if self.initialize_response is not None:
            caps = getattr(self.initialize_response, "agent_capabilities", None)
            session_caps = caps.session_capabilities if caps else None
            if session_caps is not None and session_caps.list is not None:
                commands.append(("sessions", "Browse and continue an ACP session"))
        if self.session_modes is not None:
            commands.append(("mode", "Set the ACP session mode"))
        if self.config_options:
            commands.append(("config", "Set an ACP session configuration option"))
        if self.initialize_response is not None and self.initialize_response.auth_methods:
            commands.append(("auth", "Run an agent authentication method"))
        commands.extend(
            (item.name.lstrip("/"), _available_command_detail(item))
            for item in self.available_commands
        )
        return [{
            "text": f"/{name}", "label": f"/{name}", "detail": description,
            "kind": "command", "start": start, "end": cursor,
        } for name, description in commands if name.startswith(typed)]

    def _terminal_changed(self, terminal_id: str, output: str, truncated: bool, status: Any) -> None:
        label = ""
        if status is not None:
            label = f"exit {status.exit_code}" if status.exit_code is not None else f"signal {status.signal}"
        self.store.update_terminal(terminal_id, output, truncated, label or "running")
        self._refresh(follow_tail=True)

    def _refresh(self, *, follow_tail: bool = False) -> None:
        with self._refresh_lock:
            self._refresh_dirty = True
            self._refresh_follow_tail = self._refresh_follow_tail or follow_tail
            if self._refresh_scheduled:
                return
            self._refresh_scheduled = True
            delay = max(0.0, 0.1 - (time.monotonic() - self._last_refresh))
        if delay:
            timer = threading.Timer(delay, self._queue_refresh)
            timer.daemon = True
            timer.start()
        else:
            self._queue_refresh()

    def _queue_refresh(self) -> None:
        queue_action(self._flush_refresh)

    def _flush_refresh(self) -> None:
        with self._refresh_lock:
            follow_tail = self._refresh_follow_tail
            self._refresh_follow_tail = False
            self._refresh_dirty = False
            self._refresh_scheduled = False
            self._last_refresh = time.monotonic()
        if self.turns_sheet is not None:
            self.turns_sheet.rows = self.store.turns_snapshot()
            self.turns_sheet.recalc()
        if self.live_sheet is not None:
            should_follow_tail = follow_tail and self._live_sheet_should_follow_tail()
            turn_number = getattr(self.live_sheet, "jusi_acp_turn", None)
            if turn_number is not None:
                self.live_sheet.rows = self.store.turn_events_snapshot(int(turn_number))
            elif getattr(self.live_sheet, "jusi_acp_raw", False):
                self.live_sheet.rows = self.store.raw_snapshot()
            self.live_sheet.recalc()
            if should_follow_tail:
                self.live_sheet.cursorRowIndex = max(0, len(self.live_sheet.rows) - 1)
        with self._refresh_lock:
            dirty = self._refresh_dirty
        if dirty:
            self._refresh()

    def _live_sheet_should_follow_tail(self) -> bool:
        sheet = self.live_sheet
        if sheet is None:
            return True
        rows = getattr(sheet, "rows", [])
        if not rows:
            return True
        try:
            return int(sheet.cursorRowIndex) >= len(rows) - 1
        except (AttributeError, TypeError, ValueError):
            return False

    def _show_live_turn(self, turn_number: int) -> None:
        from visidata import vd
        self.live_sheet = make_events_sheet(
            self,
            self.store.turn_events_snapshot(turn_number),
            name=f"acp:{self.alias}:turn-{turn_number}",
        )
        self.live_sheet.jusi_acp_turn = turn_number
        self.sheet = self.live_sheet
        vd.push(self.live_sheet)

    def _focus_turns_sheet(self) -> None:
        if self.turns_sheet is None:
            return
        from visidata import vd
        self.turns_sheet.rows = self.store.turns_snapshot()
        self.turns_sheet.recalc()
        vd.push(self.turns_sheet)

    def _handle_asyncio_exception(
        self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        _ = loop
        error = context.get("exception")
        if not isinstance(error, BaseException):
            error = RuntimeError(str(context.get("message", "ACP background task failed")))
        self.store.add_status(
            "ACP background task failed",
            f"{type(error).__name__}: {error}",
            "failed",
        )
        self._refresh()
        queue_action(lambda error=error: _raise_exception(error))

    def open_event(self, row: dict[str, Any]) -> None:
        diffs = row.get("diffs", [])
        if len(diffs) > 1:
            from visidata import vd
            vd.push(make_diffs_sheet(self, diffs))
        elif diffs:
            self.open_diff(diffs[0])
        else:
            from visidata import vd, TextSheet
            detail = (str(row.get("text", "")) if row.get("group") in {"assistant", "thought"}
                      else json.dumps(row.get("raw", row), ensure_ascii=False, indent=2))
            vd.push(TextSheet(str(row.get("title", "ACP event")), source=detail.splitlines()))

    def open_diff(self, diff: dict[str, Any]) -> None:
        path = Path(str(diff.get("path", "change.txt")))
        from jusi.editor_client import show_diff
        from visidata import vd
        before = str(diff.get("old_text", ""))
        after = str(diff.get("new_text", ""))

        def deliver() -> None:
            show_diff(
                before, after,
                before_name=f"{path.name} (before)",
                after_name=f"{path.name} (after)",
                filetype=_filetype(path),
            )

        vd.execAsync(deliver, sheet=None)


class QuestionPermissionResponse(RequestPermissionResponse):
    # Qwen/GigaCode's permission extension, also used by its official IDE client.
    answers: dict[str, str]


def _agent_environment() -> dict[str, str]:
    # The SDK otherwise drops desktop/session variables needed by browser auth.
    keys = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_CURRENT_DESKTOP", "XAUTHORITY", "BROWSER", "TMPDIR")
    return {key: os.environ[key] for key in keys if key in os.environ}


class _ApplicationLogHandler(logging.Handler):
    """The SDK uses the root logger, not the asyncio exception handler."""

    def __init__(self, runtime: ACPApplication) -> None:
        super().__init__()
        self.runtime = runtime

    def emit(self, record: logging.LogRecord) -> None:
        detail = self.format(record)
        self.runtime.store.add_status("ACP diagnostic", detail, record.levelname.lower())
        self.runtime._refresh()


_ACP_HEADER = re.compile(r"^[ \t]*%%acp(?:[ \t].*)?$")


def _normalize_followup_body(body: str) -> str:
    """Remove Jusi's editable cell-magic header from a follow-up body."""
    lines = body.splitlines(keepends=True)
    if lines and _ACP_HEADER.match(lines[0].rstrip("\r\n")):
        return "".join(lines[1:])
    return body


def _resolve_future(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _raise_exception(exc: BaseException) -> None:
    raise exc.with_traceback(exc.__traceback__)


def _available_command_detail(command: Any) -> str:
    description = str(getattr(command, "description", "") or "")
    command_input = getattr(command, "input", None)
    root = getattr(command_input, "root", None)
    hint = str(getattr(root, "hint", "") or "")
    if description and hint:
        return f"{description} — input: {hint}"
    return description or (f"input: {hint}" if hint else "Agent command")


def _filetype(path: Path) -> str:
    suffixes = {".py": "python", ".js": "javascript", ".ts": "typescript", ".lua": "lua",
                ".rs": "rust", ".go": "go", ".md": "markdown", ".json": "json", ".toml": "toml"}
    return suffixes.get(path.suffix.lower(), "")


def run_application(payload_path: Path, socket_path: str) -> int:
    try:
        with payload_path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
    finally:
        payload_path.unlink(missing_ok=True)
    if not isinstance(payload, dict):
        raise ValueError("ACP application payload must be an object")
    runtime = ACPApplication(payload)
    controller = ApplicationController(socket_path, runtime.handle_operation)
    controller.start()
    from jusi.visidata_support import initialize_visidata
    vd = initialize_visidata(open_name="acp.txt", open_filetype="text")
    install_api()
    vd._jusi_acp_runtime = runtime
    runtime.turns_sheet = make_turns_sheet(runtime)
    runtime.live_sheet = make_events_sheet(runtime, [], name=f"acp:{runtime.alias}:session")
    runtime.live_sheet.jusi_acp_raw = True
    runtime.sheet = runtime.live_sheet
    queue_action(runtime.start)
    root_logger = logging.getLogger()
    previous_handlers = root_logger.handlers[:]
    root_logger.handlers = [_ApplicationLogHandler(runtime)]
    try:
        vd.run(runtime.turns_sheet, runtime.live_sheet)
    finally:
        runtime.close()
        root_logger.handlers = previous_handlers
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m jusi_acp.application PAYLOAD CONTROL_SOCKET")
    raise SystemExit(run_application(Path(sys.argv[1]), sys.argv[2]))
