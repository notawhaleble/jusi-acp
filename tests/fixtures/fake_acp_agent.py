from __future__ import annotations

import asyncio
import json
import os
import sys

from acp import PROTOCOL_VERSION, run_agent
from acp.helpers import update_agent_message_text, update_user_message_text
from acp.schema import (
    AgentCapabilities,
    CurrentModeUpdate,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    NewSessionResponse,
    PromptResponse,
    SessionCapabilities,
    SessionInfo,
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
                session_capabilities=SessionCapabilities(
                    list={}, resume={}, close={}, additional_directories={}
                ),
            ),
            auth_methods=[{"id": "browser", "name": "Browser"}],
            agent_info=Implementation(name="fake", title="Fake ACP Agent", version="1"),
        )

    async def authenticate(self, method_id, **kwargs):
        assert method_id == "browser"
        assert os.environ.get("BROWSER") == "fixture-browser"
        print("Authentication completed", file=sys.stderr, flush=True)
        return {}

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):  # type: ignore[no-untyped-def]
        _ = cwd, additional_directories, mcp_servers, kwargs
        if os.environ.get("JUSI_FIXTURE_UNIQUE_SESSIONS"):
            from uuid import uuid4
            return NewSessionResponse(session_id=uuid4().hex)
        return NewSessionResponse(session_id="fake-session")

    async def list_sessions(self, cwd=None, cursor=None, **kwargs):  # type: ignore[no-untyped-def]
        _ = cursor, kwargs
        return ListSessionsResponse(sessions=[SessionInfo(
            session_id="old-session", cwd=cwd or os.getcwd(), title="Previous work",
            updated_at="2026-09-20T10:00:00Z",
        )])

    async def load_session(self, cwd, session_id, mcp_servers=None, additional_directories=None, **kwargs):  # type: ignore[no-untyped-def]
        _ = cwd, mcp_servers, additional_directories, kwargs
        await self.client.session_update(session_id, update_user_message_text("old prompt"))
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
        text = prompt[0].text.strip()
        if text.startswith("questions"):
            self.cancelled.clear()
            questions = [{
                "question": "Which project type?", "header": "Project",
                "options": [{"label": "Library", "description": "Reusable package"},
                            {"label": "Application", "description": "Runnable program"}],
            }]
            if text == "questions-many":
                questions.append({"question": "Any implementation details?", "options": []})
            for round_number in range(2 if text == "questions-again" else 1):
                response = await self.client._conn.send_request("session/request_permission", {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": f"question-{round_number}", "title": "Choose project type",
                        "kind": "other", "status": "pending", "rawInput": {"questions": questions}},
                    "options": [{"optionId": "proceed_once", "name": "Proceed", "kind": "allow_once"},
                                {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"}],
                })
                if response["outcome"]["outcome"] == "cancelled":
                    return PromptResponse(stop_reason="cancelled")
                await self.client.session_update(session_id, update_agent_message_text(
                    "ANSWERS:" + json.dumps(response.get("answers", {}), ensure_ascii=False)))
            if text == "questions-wait":
                await self.client.session_update(session_id, update_agent_message_text("AFTER-ANSWERS"))
                await self.cancelled.wait()
                return PromptResponse(stop_reason="cancelled")
            return PromptResponse(stop_reason="end_turn")
        if text.startswith("mode-notification"):
            method = ("quen" if text.endswith("-quen") else "qwen") + "/notify/session/mode-update"
            if "standalone" not in text:
                await self.client.session_update(session_id, CurrentModeUpdate(
                    session_update="current_mode_update", current_mode_id="plan"))
            await self.client._conn.send_notification(method, {
                "v": 1, "sessionId": session_id, "currentModeId": "plan", "legacyFrameSent": True,
            })
            # A request round-trip orders this test behind notification dispatch.
            terminal = await self.client.create_terminal(session_id, "true")
            await self.client.wait_for_terminal_exit(session_id, terminal.terminal_id)
            await self.client.release_terminal(session_id, terminal.terminal_id)
            return PromptResponse(stop_reason="end_turn")
        if text == "ui-idle":
            await asyncio.sleep(2)
            await self.client.session_update(session_id, update_agent_message_text("AUTOMATIC-STREAM"))
            await asyncio.sleep(2)
            await self.client._conn.send_notification("fixture/unknown", {})
            await asyncio.sleep(0.2)
            return PromptResponse(stop_reason="end_turn")
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
