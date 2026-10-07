"""Isolate the opt-in upstream experimental SDK from the stable v1 client."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any


def sdk():
    try:
        from acp.experimental import v2
    except ImportError as exc:
        raise RuntimeError('ACP v2 requires the pinned experimental SDK; install jusi-acp[v2]') from exc
    return v2


class Client:
    def __init__(self, application: Any) -> None:
        self.application = application

    async def session_update(self, **params: Any) -> None:
        await self.application.session_update(**params)

    async def request_permission(self, session_id: str, title: str, options: list[Any],
                                 description: str | None = None, subject: Any = None, **kwargs: Any) -> Any:
        return await self.application.request_permission_v2(session_id, title, options, description, subject)

    async def create_elicitation(self, **params: Any) -> Any:
        return await self.application.create_elicitation(**params)

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        self.application.complete_elicitation(elicitation_id)


class Connection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def initialize(self, **kwargs: Any) -> Any:
        schema = sdk().schema
        response = await self.connection.initialize(protocol_version=2,
            info=schema.Implementation(name='jusi-acp', version=kwargs['client_info'].version),
            capabilities=schema.ClientCapabilities(elicitation={'form': {}, 'url': {}}))
        caps = response.capabilities
        session = getattr(caps, 'session', None)
        if session is None:
            raise ValueError('ACP v2 agent does not advertise sessions')
        session_caps = SimpleNamespace(list={}, resume={}, close={},
            additional_directories=getattr(session, 'additional_directories', None),
            delete=getattr(session, 'delete', None))
        return SimpleNamespace(protocol_version=2, auth_methods=[SimpleNamespace(
            id=method.method_id, name=method.name, type=method.type) for method in response.auth_methods or []],
            agent_capabilities=SimpleNamespace(load_session=True, session_capabilities=session_caps),
            raw=response)

    async def prompt(self, session_id: str, prompt: list[Any]) -> Any:
        blocks = [sdk().schema.TextContentBlock(type='text', text=block.text) for block in prompt]
        return await self.connection.prompt(session_id, blocks)

    def _environment(self, params: dict[str, Any]) -> dict[str, Any]:
        servers = []
        for original in params.get('mcp_servers') or []:
            server = dict(original)
            server.setdefault('type', 'stdio')
            if server['type'] == 'sse':
                raise ValueError('ACP v2 does not support the deprecated SSE MCP transport')
            servers.append(server)
        return {**params, 'mcp_servers': servers}

    async def new_session(self, **kwargs: Any) -> Any:
        return await self.connection.new_session(**self._environment(kwargs))

    async def resume_session(self, **kwargs: Any) -> Any:
        return await self.connection.resume_session(**self._environment(kwargs))

    async def load_session(self, **kwargs: Any) -> Any:
        return await self.resume_session(**kwargs, replay_from={'type': 'start'})

    async def cancel(self, session_id: str) -> None:
        await self.connection.cancel_session(session_id)

    async def authenticate(self, method_id: str) -> Any:
        return await self.connection.login(method_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


@asynccontextmanager
async def spawn(application: Any, command: str, *args: str, **kwargs: Any):
    from acp.transports import spawn_stdio_transport
    v2 = sdk()
    observers = kwargs.pop('observers', [])
    async with spawn_stdio_transport(command, *args, **kwargs) as (reader, writer, process):
        async with v2.connect_to_agent(Client(application), writer, reader, observers=observers) as connection:
            yield Connection(connection), process
