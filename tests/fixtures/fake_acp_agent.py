from __future__ import annotations

import asyncio

from acp import PROTOCOL_VERSION, run_agent
from acp.helpers import update_agent_message_text
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    SessionCapabilities,
)


class FakeAgent:
    def __init__(self) -> None:
        self.client = None
        self.cancelled = asyncio.Event()

    def on_connect(self, client) -> None:  # type: ignore[no-untyped-def]
        self.client = client

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kwargs):  # type: ignore[no-untyped-def]
        _ = client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=min(protocol_version, PROTOCOL_VERSION),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                session_capabilities=SessionCapabilities(resume={}, close={}, additional_directories={}),
            ),
            agent_info=Implementation(name="fake", title="Fake ACP Agent", version="1"),
        )

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):  # type: ignore[no-untyped-def]
        _ = cwd, additional_directories, mcp_servers, kwargs
        return NewSessionResponse(session_id="fake-session")

    async def load_session(self, cwd, session_id, mcp_servers=None, additional_directories=None, **kwargs):  # type: ignore[no-untyped-def]
        _ = cwd, mcp_servers, additional_directories, kwargs
        await self.client.session_update(session_id, update_agent_message_text("replayed"))
        return {}

    async def resume_session(self, session_id, cwd, additional_directories=None, mcp_servers=None, **kwargs):  # type: ignore[no-untyped-def]
        _ = session_id, cwd, additional_directories, mcp_servers, kwargs
        return {}

    async def close_session(self, session_id, **kwargs):  # type: ignore[no-untyped-def]
        _ = session_id, kwargs
        return {}

    async def prompt(self, session_id, prompt, **kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        text = prompt[0].text
        if text == "wait":
            self.cancelled.clear()
            await self.cancelled.wait()
            return PromptResponse(stop_reason="cancelled")
        if text == "terminal":
            terminal = await self.client.create_terminal(
                session_id, "sh", ["-c", "printf terminal-ok"], output_byte_limit=1024
            )
            status = await self.client.wait_for_terminal_exit(session_id, terminal.terminal_id)
            output = await self.client.terminal_output(session_id, terminal.terminal_id)
            await self.client.release_terminal(session_id, terminal.terminal_id)
            await self.client.session_update(
                session_id,
                update_agent_message_text(f"{output.output}:{status.exit_code}"),
            )
        else:
            await self.client.session_update(session_id, update_agent_message_text(f"echo:{text}"))
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id, **kwargs):  # type: ignore[no-untyped-def]
        _ = session_id, kwargs
        self.cancelled.set()


asyncio.run(run_agent(FakeAgent()))
