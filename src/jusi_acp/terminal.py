"""ACP client-owned non-interactive terminal service."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
from typing import Any, Callable
from uuid import uuid4

from acp.schema import (
    CreateTerminalResponse,
    EnvVariable,
    TerminalExitStatus,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)


@dataclass
class _Terminal:
    session_id: str
    process: asyncio.subprocess.Process
    limit: int
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    reader_task: asyncio.Task[None] | None = None

    def append(self, chunk: bytes) -> None:
        self.output.extend(chunk)
        if len(self.output) > self.limit:
            del self.output[:len(self.output) - self.limit]
            while self.output and self.output[0] & 0xC0 == 0x80:
                del self.output[0]
            self.truncated = True

    def text(self) -> str:
        return bytes(self.output).decode("utf-8", errors="replace")


TerminalObserver = Callable[[str, str, bool, TerminalExitStatus | None], None]


class TerminalManager:
    def __init__(self, roots: tuple[Path, ...], observer: TerminalObserver | None = None) -> None:
        self.roots = tuple(root.resolve() for root in roots)
        self.observer = observer
        self._terminals: dict[str, _Terminal] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        session_id: str,
        command: str,
        args: list[str] | None,
        env: list[EnvVariable] | None,
        cwd: str | None,
        output_byte_limit: int | None,
    ) -> CreateTerminalResponse:
        target = Path(cwd).resolve() if cwd else self.roots[0]
        if not any(target == root or root in target.parents for root in self.roots):
            raise ValueError("ACP terminal cwd lies outside configured workspace roots")
        if not target.is_dir():
            raise ValueError("ACP terminal cwd is not a directory")
        limit = 1024 * 1024 if output_byte_limit is None else output_byte_limit
        if not 1 <= limit <= 64 * 1024 * 1024:
            raise ValueError("ACP terminal outputByteLimit must be between 1 byte and 64 MiB")
        child_env = dict(os.environ)
        for item in env or []:
            child_env[item.name] = item.value
        process = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=str(target),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        terminal_id = f"term-{uuid4().hex}"
        terminal = _Terminal(session_id=session_id, process=process, limit=limit)
        terminal.reader_task = asyncio.create_task(self._read_output(terminal_id, terminal))
        async with self._lock:
            self._terminals[terminal_id] = terminal
        return CreateTerminalResponse(terminal_id=terminal_id)

    async def _read_output(self, terminal_id: str, terminal: _Terminal) -> None:
        assert terminal.process.stdout is not None
        while True:
            chunk = await terminal.process.stdout.read(65536)
            if not chunk:
                # EOF can become visible a scheduling tick before asyncio has
                # published the child's return code.
                await terminal.process.wait()
                if self.observer is not None:
                    self.observer(terminal_id, terminal.text(), terminal.truncated, _exit_status(terminal.process))
                return
            terminal.append(chunk)
            if self.observer is not None:
                self.observer(terminal_id, terminal.text(), terminal.truncated, None)

    async def output(self, session_id: str, terminal_id: str) -> TerminalOutputResponse:
        terminal = await self._get(session_id, terminal_id)
        status = _exit_status(terminal.process) if terminal.process.returncode is not None else None
        return TerminalOutputResponse(output=terminal.text(), truncated=terminal.truncated, exit_status=status)

    async def wait(self, session_id: str, terminal_id: str) -> WaitForTerminalExitResponse:
        terminal = await self._get(session_id, terminal_id)
        await terminal.process.wait()
        if terminal.reader_task is not None:
            await terminal.reader_task
        status = _exit_status(terminal.process)
        return WaitForTerminalExitResponse(exit_code=status.exit_code, signal=status.signal)

    async def kill(self, session_id: str, terminal_id: str) -> None:
        terminal = await self._get(session_id, terminal_id)
        await _terminate(terminal.process)

    async def release(self, session_id: str, terminal_id: str) -> None:
        async with self._lock:
            terminal = self._terminals.get(terminal_id)
            if terminal is None or terminal.session_id != session_id:
                raise LookupError("Unknown ACP terminal")
            del self._terminals[terminal_id]
        await _terminate(terminal.process)
        if terminal.reader_task is not None:
            await terminal.reader_task

    async def close(self) -> None:
        async with self._lock:
            terminals = list(self._terminals.values())
            self._terminals.clear()
        await asyncio.gather(*(_terminate(item.process) for item in terminals), return_exceptions=True)
        await asyncio.gather(*(item.reader_task for item in terminals if item.reader_task), return_exceptions=True)

    async def _get(self, session_id: str, terminal_id: str) -> _Terminal:
        async with self._lock:
            terminal = self._terminals.get(terminal_id)
        if terminal is None or terminal.session_id != session_id:
            raise LookupError("Unknown ACP terminal")
        return terminal


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def _exit_status(process: asyncio.subprocess.Process) -> TerminalExitStatus:
    returncode = process.returncode
    if returncode is None:
        raise RuntimeError("ACP terminal is still running")
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        return TerminalExitStatus(exit_code=None, signal=name)
    return TerminalExitStatus(exit_code=returncode, signal=None)
