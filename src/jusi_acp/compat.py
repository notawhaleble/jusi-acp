"""Narrow routing compatibility for the pinned ACP 0.12 SDK."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from acp.router import MessageRouter, Route

# Upstream uses qwen; also accept the spelling reported by GigaCode clients.
MODE_NOTIFICATIONS = frozenset({
    "qwen/notify/session/mode-update",
    "quen/notify/session/mode-update",
})


def install_mode_notifications(
    connection: Any, handler: Callable[[Any], Awaitable[None]]
) -> None:
    # The SDK only forwards underscore-prefixed names to ext_notification.
    # It exposes no registration hook on ClientSideConnection in 0.12, so keep
    # this version-specific access isolated. Modify this connection's router,
    # never the SDK class or its global routing tables.
    router = connection._conn._handler
    if not isinstance(router, MessageRouter):
        raise RuntimeError("Unsupported ACP SDK notification router")
    for method in MODE_NOTIFICATIONS:
        router.add_route(Route(method=method, func=handler, kind="notification"))
