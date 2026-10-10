from __future__ import annotations

import asyncio
import json
import os
import sys

from acp import PROTOCOL_VERSION, run_agent
from acp.helpers import update_agent_message_text, update_user_message_text
from acp.schema import (
    AgentCapabilities,
    ElicitationFormSessionMode,
    ElicitationUrlSessionMode,
    CurrentModeUpdate,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    NewSessionResponse,
    PromptResponse,
    SessionCapabilities,
    SessionInfo,
    ToolCallUpdate,
    Usage,
    UsageUpdate,
)


class FakeAgent:
    def __init__(self) -> None:
        self.client = None
        self.cancelled = asyncio.Event()
        self.background_tasks = set()
        self.model = "fixture-alpha"

    def config_options(self):
        return [{"id": "model", "category": "model", "name": "Model", "type": "select",
                 "currentValue": self.model, "options": [
                     {"value": "fixture-alpha", "name": "Alpha model"},
                     {"value": "fixture-beta", "name": "Beta model"}]}]

    async def set_config_option(self, config_id, session_id, value, **kwargs):
        assert config_id == "model" and value in {"fixture-alpha", "fixture-beta"}
        self.model = value
        return {"configOptions": self.config_options()}

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
        session_id = "fake-session"
        if os.environ.get("JUSI_FIXTURE_UNIQUE_SESSIONS"):
            from uuid import uuid4
            session_id = uuid4().hex
        return NewSessionResponse(session_id=session_id, config_options=(
            self.config_options() if os.environ.get("JUSI_FIXTURE_MODELS") else None))

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
        if text == "elicitation-form":
            result = await self.client.create_elicitation("Configure the task", ElicitationFormSessionMode(
                session_id=session_id, requested_schema={"type": "object", "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                    "notes": {"type": "string"}}, "required": ["count", "notes"]}))
            await self.client.session_update(session_id, update_agent_message_text(
                "FORM:" + json.dumps(result.model_dump(mode="json", exclude_none=True), ensure_ascii=False)))
            return PromptResponse(stop_reason="end_turn")
        if text == "elicitation-url":
            result = await self.client.create_elicitation("Open the example page", ElicitationUrlSessionMode(
                session_id=session_id, elicitation_id="example", url="https://example.com/"))
            if result.action == "accept":
                await self.client.complete_elicitation("example")
            await self.client.session_update(session_id, update_agent_message_text("URL:" + result.action))
            return PromptResponse(stop_reason="end_turn")
        if text == "background-metadata":
            # Qwen completes the launch tool, then sends two metadata-bearing
            # messages for each finished task in later background activity.
            for index in range(2):
                await self.client.session_update(session_id, {
                    "sessionUpdate": "tool_call", "toolCallId": f"metadata-launch-{index}",
                    "title": "Launch background agent", "status": "in_progress", "kind": "other"})
                await self.client.session_update(session_id, {
                    "sessionUpdate": "tool_call_update", "toolCallId": f"metadata-launch-{index}",
                    "status": "completed", "rawOutput": {"executionMode": "background", "status": "background"}})

            async def metadata_background():
                await asyncio.sleep(0.5)
                await self.client.session_update(session_id, update_agent_message_text("Background agent completed."))
                for index in range(2):
                    for source in ("background_task_completed", "background_notification"):
                        await self.client.session_update(session_id, {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Background agent completed."},
                            "_meta": {"source": source, "qwenDiscreteMessage": True, "backgroundTask": {
                                "taskId": f"metadata-task-{index}", "toolUseId": f"metadata-launch-{index}",
                                "sourceTurnId": session_id + "########1", "status": "completed", "kind": "agent"}}})
            task = asyncio.create_task(metadata_background())
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
            await self.client.session_update(session_id, update_agent_message_text("Background agents started"))
            return PromptResponse(stop_reason="end_turn")
        if text == "background":
            await self.client.session_update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "worker-bg",
                "title": "Background worker", "kind": "other", "status": "in_progress"})
            await self.client.session_update(session_id, {"sessionUpdate": "plan", "entries": [
                {"content": "Background work", "priority": "medium", "status": "in_progress"}]})
            async def background():
                await asyncio.sleep(0.5)
                await self.client.session_update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "worker-bg",
                    "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": "Late worker result"}}]})
                for source in ("background_task_completed", "background_notification"):
                    await self.client.session_update(session_id, {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Background worker completed."},
                        "_meta": {"source": source, "backgroundTask": {
                            "taskId": "worker-task", "toolUseId": "worker-bg", "status": "completed"}}})
                await self.client.session_update(session_id, update_agent_message_text("Unassigned background message"))
                await self.client.session_update(session_id, {"sessionUpdate": "plan", "entries": [
                    {"content": "Background work", "priority": "medium", "status": "completed"}]})
                response = await self.client._conn.send_request("session/request_permission", {
                    "sessionId": session_id, "toolCall": {"toolCallId": "worker-bg", "title": "Background question",
                    "rawInput": {"questions": [{"question": "Background follow-up?", "options": []}]}},
                    "options": [{"optionId": "yes", "name": "Answer", "kind": "allow_once"}]})
                await self.client.session_update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "worker-bg",
                    "content": [{"type": "content", "content": {"type": "text", "text": "BG:" + json.dumps(response.get("answers"))}}]})
            task = asyncio.create_task(background())
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
            await self.client.session_update(session_id, update_agent_message_text("Background started"))
            return PromptResponse(stop_reason="end_turn")
        if text == "usage":
            await self.client.session_update(session_id, UsageUpdate(
                session_update="usage_update", used=120, size=200000,
                cost={"amount": 0.025, "currency": "USD"},
            ))
            return PromptResponse(stop_reason="end_turn", usage=Usage(
                total_tokens=150, input_tokens=100, output_tokens=25,
                thought_tokens=5, cached_read_tokens=20, cached_write_tokens=0,
                field_meta={"extra": "preserved"},
            ))
        if text in {"prompt-suggestion", "prompt-suggestion-prefixed"}:
            method = "qwen/notify/session/prompt-suggestion"
            if text.endswith("-prefixed"):
                method = "_" + method
            await self.client.session_update(session_id, update_agent_message_text("Finished"))
            await self.client._conn.send_notification(method, {
                "v": 1, "sessionId": session_id,
                "suggestion": "Add tests", "promptId": session_id + "########1",
            })
            terminal = await self.client.create_terminal(session_id, "true")
            await self.client.wait_for_terminal_exit(session_id, terminal.terminal_id)
            await self.client.release_terminal(session_id, terminal.terminal_id)
            return PromptResponse(stop_reason="end_turn")
        if text == "diff":
            update = ToolCallUpdate(
                tool_call_id="edit-1", kind="edit", status="completed", title="Edit example.py",
                content=[{"type": "diff", "path": "example.py",
                          "oldText": "before\n", "newText": "after α\n"}],
            )
            payload = update.model_dump(mode="json", by_alias=True, exclude_none=True)
            payload["sessionUpdate"] = "tool_call_update"
            await self.client.session_update(session_id, payload)
            await self.client.session_update(session_id, update_agent_message_text("Updated example.py"))
            return PromptResponse(stop_reason="end_turn")
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
