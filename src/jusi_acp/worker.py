"""Reusable exact-provider Jusi worker for ACP agents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from jusi.plugin_api import OperationInterrupted, OperationRejected, WorkerResult, terminal_surface

from .ipc import WorkerApplicationBridge
from .provider import ProviderSpec


class ACPWorker:
    def __init__(self, context: Any, provider: ProviderSpec) -> None:
        self.context = context
        self.provider = provider
        if getattr(context, "plugin_id", provider.plugin_id) != provider.plugin_id:
            raise ValueError("ACP worker context does not match exact provider")
        self.runtime_directory: Path | None = None
        self.bridge: WorkerApplicationBridge | None = None

    def handle(self, operation: str, payload: dict[str, Any]) -> WorkerResult:
        if operation == "execute":
            return self._execute(payload)
        if operation == "editor_action":
            raise OperationRejected(
                "Use copy/open/diff actions inside the ACP application", reason="unsupported"
            )
        if operation not in {"followup", "complete"}:
            raise OperationRejected(f"Unsupported ACP operation: {operation}", reason="unsupported")
        response = self._required_bridge().request(operation, payload)
        if response.get("ok") is True:
            result = response.get("result", {})
            return WorkerResult(dict(result) if isinstance(result, dict) else {"accepted": True})
        error = str(response.get("error", "fatal"))
        message = str(response.get("message", "ACP application operation failed"))
        if error == "interrupted":
            raise OperationInterrupted(message)
        if error == "rejected":
            raise OperationRejected(message, reason="invalid_request")
        raise RuntimeError(message)

    def _execute(self, payload: dict[str, Any]) -> WorkerResult:
        if self.runtime_directory is not None:
            raise OperationRejected("ACP client already initialized", reason="conflict")
        cwd = Path(str(payload.get("cwd", ""))).expanduser()
        if not cwd.is_absolute() or not cwd.is_dir():
            raise OperationRejected("ACP alias path is not an existing absolute directory", reason="invalid_request")
        configuration = payload.get("configuration", {})
        if not isinstance(configuration, dict):
            raise OperationRejected("ACP provider configuration must be an object", reason="invalid_request")
        try:
            launch = self.provider.resolve_launch(configuration, cwd)
        except (LookupError, OSError, TypeError, ValueError) as exc:
            raise OperationRejected(f"Invalid {self.provider.plugin_id} configuration: {exc}", reason="invalid_request") from exc

        self.runtime_directory = Path(tempfile.mkdtemp(prefix="jusi-acp-", dir="/tmp"))
        self.runtime_directory.chmod(0o700)
        payload_path = self.runtime_directory / "payload.json"
        socket_path = str(self.runtime_directory / "control.sock")
        application_payload = {
            "protocol_version": 1,
            "plugin_id": self.provider.plugin_id,
            "plugin_version": self.provider.plugin_version,
            "launch": launch.to_json(),
            "submission": {
                key: payload.get(key)
                for key in (
                    "alias", "body", "cwd", "additional_directories", "mcp_servers",
                    "session_action", "session_id",
                )
            },
        }
        try:
            fd = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(application_payload, stream, ensure_ascii=False)
            self.bridge = WorkerApplicationBridge(socket_path)
            return WorkerResult(
                {"accepted": True},
                (terminal_surface(
                    "acp_application",
                    (sys.executable, "-m", "jusi_acp.application", str(payload_path), socket_path),
                    cwd=str(cwd),
                    environment_overrides={"TERM": os.environ.get("JUSI_ACP_TERM", "").strip() or "xterm-256color"},
                    signal=True,
                ),),
            )
        except BaseException:
            self.close()
            raise

    def interrupt(self) -> None:
        bridge = self.bridge
        if bridge is not None:
            bridge.interrupt()

    def _required_bridge(self) -> WorkerApplicationBridge:
        if self.bridge is None:
            raise RuntimeError("ACP application was not initialized")
        return self.bridge

    def close(self) -> None:
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None
        if self.runtime_directory is not None:
            shutil.rmtree(self.runtime_directory, ignore_errors=True)
            self.runtime_directory = None
