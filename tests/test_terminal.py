from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jusi_acp.terminal import TerminalManager


def test_terminal_lifecycle_and_workspace_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = TerminalManager((tmp_path,))
        created = await manager.create(
            "session", "sh", ["-c", "printf 'abcdef'"], None, str(tmp_path), 4
        )
        exited = await manager.wait("session", created.terminal_id)
        output = await manager.output("session", created.terminal_id)
        assert exited.exit_code == 0
        assert output.output == "cdef"
        assert output.truncated is True
        with pytest.raises(LookupError):
            await manager.output("other", created.terminal_id)
        await manager.release("session", created.terminal_id)
        await manager.close()
    asyncio.run(scenario())


def test_terminal_rejects_cwd_outside_roots(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "root"
        root.mkdir()
        manager = TerminalManager((root,))
        with pytest.raises(ValueError, match="outside"):
            await manager.create("session", "true", [], None, str(tmp_path), 10)
    asyncio.run(scenario())
