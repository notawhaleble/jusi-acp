"""VisiData presentation and feedback surfaces for the shared ACP client."""
from __future__ import annotations

import curses
from dataclasses import dataclass
from collections import deque
import threading
from typing import Any, Callable

_ACTIONS: deque[Callable[[], object]] = deque()
_INSTALLED = False


def queue_action(action: Callable[[], object]) -> None:
    _ACTIONS.append(action)
    try:
        from visidata import vd
        vd.queueCommand("jusi-acp-run-pending-action")
    except Exception:
        pass
    try:
        curses.ungetch(curses.KEY_RESIZE)
    except Exception:
        pass


def install_api() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from visidata import BaseSheet

    @BaseSheet.command("", "jusi-acp-run-pending-action", "run pending ACP UI actions", replay=False)
    def _run(sheet: Any) -> None:
        _ = sheet
        while _ACTIONS:
            _ACTIONS.popleft()()

    _INSTALLED = True


@dataclass
class PendingPermission:
    request_id: str
    title: str
    kind: str
    locations: str
    options: list[Any]


def make_events_sheet(runtime: Any) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPEventsSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "ACP event"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> Any:  # type: ignore[override]
            _ = rowidx
            return runtime.open_event(row)

    sheet = ACPEventsSheet(
        f"acp:{runtime.alias}",
        rows=runtime.store.rows,
        columns=[
            ItemColumn("time", width=20),
            ItemColumn("source", width=8),
            ItemColumn("type", width=25),
            ItemColumn("title", width=32),
            ItemColumn("status", width=12),
            ItemColumn("text", width=80),
        ],
    )
    sheet.addCommand(
        "c", "jusi-acp-cancel", "vd._jusi_acp_runtime.cancel_initial()",
        "cancel active ACP turn",
    )
    sheet.runtime = runtime
    return sheet


def make_permission_sheet(runtime: Any, pending: PendingPermission) -> Any:
    from visidata import ItemColumn, Sheet

    rows = [{
        "name": option.name,
        "kind": option.kind,
        "option_id": option.option_id,
        "tool": pending.title,
        "locations": pending.locations,
    } for option in pending.options]

    class PermissionSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "permission choice"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> None:  # type: ignore[override]
            _ = rowidx
            runtime.select_permission(pending.request_id, str(row["option_id"]))
            from visidata import vd
            vd.quit(self)

    sheet = PermissionSheet(
        f"permission:{pending.title}",
        rows=rows,
        columns=[
            ItemColumn("name", width=28),
            ItemColumn("kind", width=16),
            ItemColumn("tool", width=40),
            ItemColumn("locations", width=60),
        ],
    )
    sheet.addCommand(
        "q", "jusi-acp-cancel-permission",
        "vd._jusi_acp_runtime.select_permission(sheet.jusi_acp_permission_id, None); vd.quit(sheet)",
        "deny the pending permission and close this sheet",
    )
    sheet.jusi_acp_permission_id = pending.request_id
    return sheet


def make_diffs_sheet(runtime: Any, diffs: list[dict[str, Any]]) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPDiffsSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "ACP diff"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> None:  # type: ignore[override]
            _ = rowidx
            runtime.open_diff(row)

    return ACPDiffsSheet(
        "ACP changes",
        rows=diffs,
        columns=[
            ItemColumn("path", width=60),
            ItemColumn("old_text", width=40),
            ItemColumn("new_text", width=40),
        ],
    )
