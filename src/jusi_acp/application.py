"""Shared interactive ACP client application hosted in a Jusi terminal surface."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
import json
import os
from pathlib import Path
import shlex
import sys
import threading
from typing import Any
from uuid import uuid4

from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process
from acp.schema import (
    AuthCapabilities,
    ClientCapabilities,
    FileSystemCapabilities,
    Implementation,
    RequestPermissionResponse,
    TextContentBlock,
)

from . import __version__
from .ipc import ApplicationController
from .state import EventStore
from .terminal import TerminalManager
from .ui import (
    PendingPermission,
    install_api,
    make_diffs_sheet,
    make_events_sheet,
    make_permission_sheet,
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
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connection: Any = None
        self.initialize_response: Any = None
        self.session_id = ""
        self.session_modes: Any = None
        self.config_options: list[Any] = []
        self.available_commands: list[Any] = []
        self._prompt_lock: asyncio.Lock | None = None
        self._shutdown: asyncio.Event | None = None
        self._connected = threading.Event()
        self._failed: BaseException | None = None
        self._cancel_requested = threading.Event()
        self._operation_lock = threading.Lock()
        self._operation_cancel: threading.Event | None = None
        self._permission_lock = threading.Lock()
        self._permissions: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[Any]]] = {}
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
            if self.session_modes is not None:
                self.session_modes.current_mode_id = current
        elif kind == "config_option_update":
            self.config_options = list(getattr(update, "config_options", []))
        self.store.add(update)
        self._refresh()

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
        pending = PendingPermission(
            request_id=request_id,
            title=str(getattr(tool_call, "title", None) or getattr(tool_call, "tool_call_id", "Tool call")),
            kind=str(getattr(tool_call, "kind", "") or ""),
            locations=locations,
            options=options,
        )
        self.store.add_status("Permission required", pending.title, "pending")
        queue_action(lambda: self._push_permission(pending))
        try:
            option_id = await future
        finally:
            with self._permission_lock:
                self._permissions.pop(request_id, None)
        if option_id is None:
            return RequestPermissionResponse(outcome={"outcome": "cancelled"})
        return RequestPermissionResponse(outcome={"outcome": "selected", "optionId": option_id})

    async def create_terminal(
        self, session_id: str, command: str, args: list[str] | None = None,
        env: list[Any] | None = None, cwd: str | None = None,
        output_byte_limit: int | None = None, **kwargs: Any,
    ) -> Any:
        _ = kwargs
        result = await self.terminals.create(session_id, command, args, env, cwd, output_byte_limit)
        self.store.update_terminal(result.terminal_id, "", False, "running")
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
        _ = method, params

    def on_connect(self, connection: Any) -> None:
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
            self._refresh()
        finally:
            self._connected.set()

    async def _async_main(self) -> None:
        self.loop = asyncio.get_running_loop()
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
            env=self.environment,
            cwd=self.cwd,
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
                try:
                    await self._establish_session()
                except RequestError as exc:
                    if not initialized.auth_methods:
                        raise
                    self.store.add_status(
                        "Authentication required", f"{exc}; use /auth METHOD_ID", "blocked"
                    )
                self._connected.set()
                if self.initial_body.strip():
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

    def _capture_session_options(self, response: Any) -> None:
        self.session_modes = getattr(response, "modes", None)
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
        body = str(payload.get("body", ""))
        with self._operation_lock:
            if self._operation_cancel is None:
                self._operation_cancel = threading.Event()
        try:
            return self._submit(self._prompt_or_command(body))
        finally:
            with self._operation_lock:
                self._operation_cancel = None

    def _submit(self, coroutine: Any) -> dict[str, Any]:
        if not self._connected.wait(10):
            raise RuntimeError("ACP agent did not become ready")
        if self._failed is not None:
            raise RuntimeError(f"ACP runtime failed: {self._failed}")
        if self.loop is None:
            raise RuntimeError("ACP runtime loop is unavailable")
        future: ConcurrentFuture[dict[str, Any]] = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()

    async def _prompt_or_command(self, body: str, *, initial: bool = False) -> dict[str, Any]:
        stripped = body.strip()
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
            await self.connection.set_config_option(parts[1], self.session_id, value)
            return {"command": "config", "config_id": parts[1], "value": value}
        if stripped.startswith("/auth "):
            parts = shlex.split(stripped)
            if len(parts) != 2:
                raise ValueError("usage: /auth METHOD_ID")
            await self._authenticate(parts[1])
            if not self.session_id:
                await self._establish_session()
            return {"command": "auth", "method_id": parts[1]}
        if stripped == "/cancel":
            await self._cancel()
            return {"command": "cancel"}
        return await self._prompt(body, initial=initial)

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
            self.store.add({"kind": "user_prompt", "text": body}, source="user")
            self._refresh()
            response = await self.connection.prompt(
                self.session_id, [TextContentBlock(type="text", text=body)]
            )
            stop_reason = str(response.stop_reason)
            self.store.add({"kind": "turn_stopped", "stopReason": stop_reason}, source="agent")
            self._refresh()
            cancelled = (
                self._cancel_requested.is_set()
                if initial
                else operation_cancel is not None and operation_cancel.is_set()
            ) or stop_reason in {"cancelled", "canceled"}
            if cancelled:
                raise InterruptedError("ACP turn cancelled")
            return {"stop_reason": stop_reason, "session_id": self.session_id}

    async def _authenticate(self, method_id: str) -> None:
        methods = {item.id: item for item in self.initialize_response.auth_methods or []}
        method = methods.get(method_id)
        if method is None:
            raise ValueError(f"Unknown ACP authentication method {method_id!r}")
        method_type = getattr(method, "type", None)
        if method_type is not None:
            raise ValueError(f"ACP authentication method type {method_type!r} is not supported by jusi-acp 0.1")
        await self.connection.authenticate(method_id)
        self.store.add_status("Authenticated", method_id, "ready")

    def cancel_initial(self) -> None:
        self._cancel_requested.set()
        with self._operation_lock:
            if self._operation_cancel is not None:
                self._operation_cancel.set()
        self._cancel_permissions()
        loop = self.loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._cancel()))

    async def _cancel(self) -> None:
        if self.connection is not None and self.session_id:
            await self.connection.cancel(self.session_id)

    def close(self) -> None:
        self._cancel_permissions()
        loop = self.loop
        shutdown = self._shutdown
        if loop is not None and shutdown is not None:
            loop.call_soon_threadsafe(shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=4)

    # UI and feedback ----------------------------------------------------
    def select_permission(self, request_id: str, option_id: str | None) -> None:
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
        from visidata import vd
        vd.push(make_permission_sheet(self, pending))

    def _completion_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        prefix = str(payload.get("prefix", ""))
        cursor = int(payload.get("cursor_pos", len(prefix)))
        start = prefix.rfind("/")
        if start < 0 or (start > 0 and not prefix[start - 1].isspace()):
            return []
        typed = prefix[start + 1:]
        commands = [("cancel", "Cancel the active ACP turn")]
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
        self._refresh()

    def _refresh(self) -> None:
        if self.sheet is None:
            return
        def refresh() -> None:
            try:
                self.sheet.rows = self.store.rows
                self.sheet.recalc()
            except Exception:
                pass
        queue_action(refresh)

    def open_event(self, row: dict[str, Any]) -> None:
        diffs = row.get("diffs", [])
        if len(diffs) > 1:
            from visidata import vd
            vd.push(make_diffs_sheet(self, diffs))
        elif diffs:
            self.open_diff(diffs[0])

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


def _resolve_future(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


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
    runtime.sheet = make_events_sheet(runtime)
    runtime.start()
    try:
        vd.run(runtime.sheet)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m jusi_acp.application PAYLOAD CONTROL_SOCKET")
    raise SystemExit(run_application(Path(sys.argv[1]), sys.argv[2]))
