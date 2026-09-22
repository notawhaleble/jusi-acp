"""VisiData presentation and feedback surfaces for the shared ACP client."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import threading
from typing import Any, Callable

_ACTIONS: deque[Callable[[], object]] = deque()
_ACTIONS_LOCK = threading.Lock()
_INSTALLED = False


def queue_action(action: Callable[[], object]) -> None:
    with _ACTIONS_LOCK:
        _ACTIONS.append(action)


def drain_actions() -> None:
    """Run a bounded snapshot on the drawing thread, never through command replay."""
    from visidata import vd
    with _ACTIONS_LOCK:
        actions = list(_ACTIONS)
        _ACTIONS.clear()
    for action in actions:
        try:
            action()
        except Exception as exc:
            vd.exceptionCaught(exc)


def install_api() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from visidata import vd

    # ACP uses an independent asyncio thread, invisible to unfinishedThreads.
    # Keep curses polling; ungetch from another thread cannot wake a blocked read.
    vd.timeouts_before_idle = -1
    original_draw = vd.draw_all
    original_getkeystroke = vd.getkeystroke

    def draw_all() -> None:
        drain_actions()
        original_draw()

    vd.draw_all = draw_all

    def getkeystroke(scr: Any, sheet: Any) -> Any:
        # mainloop captures its sheet before draw_all. A queued push during
        # drawing must not route the first key to the previously visible sheet.
        if sheet is not vd.activeSheet:
            return ""
        return original_getkeystroke(scr, sheet)

    vd.getkeystroke = getkeystroke
    _INSTALLED = True


@dataclass
class PendingPermission:
    request_id: str
    title: str
    kind: str
    locations: str
    options: list[Any]
    details: str = ""
    questions: list[dict[str, Any]] | None = None


def make_events_sheet(
    runtime: Any, rows: list[dict[str, Any]] | None = None, *, name: str | None = None
) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPEventsSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "ACP event"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> Any:  # type: ignore[override]
            _ = rowidx
            return runtime.open_event(row)

    sheet = ACPEventsSheet(
        name or f"acp:{runtime.alias}",
        rows=runtime.store.raw_snapshot() if rows is None else rows,
        columns=[
            ItemColumn("type", width=20),
            ItemColumn("title", width=24),
            ItemColumn("text", width=80),
            ItemColumn("status", width=12),
            ItemColumn("tool", width=22),
            ItemColumn("command", width=45),
            ItemColumn("input", width=60),
            ItemColumn("time", width=20),
            ItemColumn("source", width=8),
        ],
    )
    sheet.addCommand(
        "c", "jusi-acp-cancel", "vd._jusi_acp_runtime.cancel_initial()",
        "cancel active ACP turn",
    )
    sheet.runtime = runtime
    return sheet


def make_turns_sheet(runtime: Any) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPTurnsSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "ACP turn"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> Any:  # type: ignore[override]
            _ = rowidx
            turn_number = int(row.get("turn", 0))
            return make_events_sheet(
                runtime,
                runtime.store.turn_events_snapshot(turn_number),
                name=f"acp:{runtime.alias}:turn-{turn_number}",
            )

    sheet = ACPTurnsSheet(
        f"acp_turns:{runtime.alias}",
        rows=runtime.store.turns_snapshot(),
        columns=[
            ItemColumn("turn", width=8),
            ItemColumn("model", width=24),
            ItemColumn("status", width=14),
            ItemColumn("prompt", width=40),
            ItemColumn("reply", width=60),
            ItemColumn("time", width=20),
        ],
    )
    sheet.runtime = runtime
    return sheet


def make_sessions_sheet(runtime: Any, rows: list[dict[str, Any]]) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPSessionsSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "ACP session"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> None:  # type: ignore[override]
            _ = rowidx
            runtime.select_session(str(row["session_id"]))

    sheet = ACPSessionsSheet(
        f"acp_sessions:{runtime.alias}",
        rows=rows,
        columns=[
            ItemColumn("title", width=50),
            ItemColumn("updated_at", width=24),
            ItemColumn("cwd", width=60),
            ItemColumn("session_id", width=40),
        ],
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
        "details": pending.details,
    } for option in pending.options]

    class PermissionSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "permission choice"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> None:  # type: ignore[override]
            _ = rowidx
            runtime.select_permission(pending.request_id, str(row["option_id"]))
            from visidata import vd
            vd.quit(self)

        def show_details(self) -> None:
            from visidata import vd, TextSheet
            vd.push(TextSheet(pending.title, source=pending.details.splitlines()))

    sheet = PermissionSheet(
        f"permission:{pending.title}",
        rows=rows,
        columns=[
            ItemColumn("name", width=28),
            ItemColumn("kind", width=16),
            ItemColumn("tool", width=40),
            ItemColumn("details", width=80),
            ItemColumn("locations", width=60),
        ],
    )
    sheet.addCommand(
        "q", "jusi-acp-cancel-permission",
        "vd._jusi_acp_runtime.select_permission(sheet.jusi_acp_permission_id, None); vd.quit(sheet)",
        "deny the pending permission and close this sheet",
    )
    sheet.addCommand("d", "jusi-acp-permission-details", "sheet.show_details()", "read complete tool request")
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


def make_questions_sheet(runtime: Any, state: dict[str, Any]) -> Any:
    from visidata import TextSheet
    from .questions import question_text

    sheet = TextSheet(
        f"Answer in Jusi follow-up ({state['question_number']}/{state['question_count']})",
        source=question_text(state).splitlines(),
    )
    sheet.options.wrap = True
    sheet.addCommand("c", "jusi-acp-cancel-waiting-turn", "sheet.runtime.cancel_initial()",
                     "cancel the waiting ACP turn")
    sheet.runtime = runtime
    return sheet
