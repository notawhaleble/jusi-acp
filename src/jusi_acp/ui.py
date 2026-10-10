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

    def getkeystroke(scr: Any, sheet: Any = None) -> Any:
        # mainloop captures its sheet before draw_all. A queued push during
        # drawing must not route the first key to the previously visible sheet.
        if sheet is not None and sheet is not vd.activeSheet:
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
    sheet.addCommand(
        "d", "jusi-acp-event-diffs", "sheet.runtime.open_event_diffs(cursorRow)",
        "open diffs supplied for this event",
    )
    sheet.runtime = runtime
    return session_commands(sheet)


def make_turns_sheet(runtime: Any) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPTurnsSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "ACP turn"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> Any:  # type: ignore[override]
            _ = rowidx
            if row.get("activity_id"):
                sheet = make_events_sheet(runtime, runtime.store.activity_snapshot(row["activity_id"]),
                                          name="Unassigned session activity")
                sheet.jusi_acp_activity = row["activity_id"]
                return sheet
            turn_number = int(row.get("turn", 0))
            runtime.store.mark_read(turn_number)
            runtime._refresh()
            sheet = make_events_sheet(
                runtime,
                runtime.store.turn_events_snapshot(turn_number),
                name=f"acp:{runtime.alias}:turn-{turn_number}",
            )
            sheet.jusi_acp_turn = turn_number
            return sheet

    sheet = ACPTurnsSheet(
        f"acp_turns:{runtime.alias}",
        rows=runtime.store.timeline_snapshot(),
        columns=[
            ItemColumn("turn", width=8),
            ItemColumn("updates", width=8),
            ItemColumn("prompt", width=40),
            ItemColumn("reply", width=60),
            ItemColumn("model", width=24),
            ItemColumn("status", width=14),
            ItemColumn("changes", width=8),
            ItemColumn("time", width=20),
            ItemColumn("total_tokens", width=14, type=int),
            ItemColumn("context_used", width=14, type=int),
            ItemColumn("session_cost", width=14, type=float),
            ItemColumn("currency", width=10),
            ItemColumn("input_tokens", width=14, type=int),
            ItemColumn("output_tokens", width=14, type=int),
            ItemColumn("thought_tokens", width=15, type=int),
            ItemColumn("cached_read_tokens", width=19, type=int),
            ItemColumn("cached_write_tokens", width=20, type=int),
            ItemColumn("context_size", width=14, type=int),
        ],
    )
    sheet.addCommand("P", "jusi-acp-plan-history", "sheet.runtime.open_plan_history(cursorRow)",
                     "show plan revisions received during this turn")
    sheet.addCommand(
        "d", "jusi-acp-turn-diffs", "sheet.runtime.open_turn_diffs(cursorRow)",
        "open files changed during this turn",
    )
    sheet.runtime = runtime
    return session_commands(sheet)


def make_models_sheet(runtime: Any, rows: list[dict[str, Any]]) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPModelsSheet(Sheet):
        rowtype = "ACP model"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> None:
            runtime.select_model(str(row["model_id"]), self)

    sheet = ACPModelsSheet(
        f"acp_models:{runtime.alias}", rows=rows,
        columns=[ItemColumn("current", width=8), ItemColumn("name", width=35),
                 ItemColumn("model_id", width=45), ItemColumn("description", width=70)],
    )
    sheet.runtime = runtime
    sheet.session_id = runtime.session_id
    sheet.cursorRowIndex = next((i for i, row in enumerate(rows) if row["current"]), 0)
    return session_commands(sheet)


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
    return session_commands(sheet)


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
    sheet.runtime = runtime
    return session_commands(sheet)


def make_diffs_sheet(
    runtime: Any, diffs: list[dict[str, Any]], *, name: str = "ACP changes"
) -> Any:
    from visidata import ItemColumn, Sheet

    class ACPDiffsSheet(Sheet):  # type: ignore[misc, valid-type]
        rowtype = "ACP diff"

        def openRow(self, row: dict[str, Any], rowidx: int | None = None) -> None:  # type: ignore[override]
            _ = rowidx
            runtime.open_diff(row)

    return ACPDiffsSheet(
        name,
        rows=diffs,
        columns=[
            ItemColumn("path", width=60),
            ItemColumn("change", width=10),
            ItemColumn("revision", width=9),
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
    if state.get("kind") == "url" or state.get("confirm"):
        sheet.source = (state["question"] + "\n\nEnter: acknowledge · x: decline · q: close view (keeps waiting)").splitlines()
        sheet.answer_state = state
        sheet.addCommand("Enter", "jusi-acp-accept-elicitation", 'sheet.runtime.submit_ui_answer(sheet.answer_state, "/accept")', "acknowledge this request")
        sheet.addCommand("x", "jusi-acp-decline-elicitation", 'sheet.runtime.submit_ui_answer(sheet.answer_state, "/decline")', "decline this request")
    sheet.addCommand("c", "jusi-acp-cancel-waiting-turn", "sheet.runtime.cancel_initial()",
                     "cancel the waiting ACP turn")
    sheet.runtime = runtime
    return session_commands(sheet)


def session_commands(sheet: Any, *, editable: bool = False) -> Any:
    if not editable:
        sheet.addCommand("e", "jusi-acp-readonly", 'fail("This view is read-only; write in your Jusi follow-up")', "read-only presentation")
    for key, name, method, help_text in [
        ('p', 'plan', 'open_plan()', 'show current session plan'),
        ('t', 'terminals', 'open_terminals()', 'monitor client-owned terminals'),
        ('w', 'pending', 'open_pending()', 'show pending requests'),
        ('a', 'active-question', 'reopen_question()', 'return to the active question'),
    ]:
        sheet.addCommand(key, 'jusi-acp-' + name, 'sheet.runtime.' + method, help_text)
    return sheet


def make_plan_sheet(runtime: Any, revisions: list[dict[str, Any]] | None = None) -> Any:
    from visidata import ItemColumn, Sheet
    rows = runtime.store.plan_snapshot() if revisions is None else [
        dict(entry, revision=revision['revision'], time=revision['time'])
        for revision in revisions for entry in (revision['entries'] or [{'content': '(empty plan)'}])]
    sheet = Sheet('ACP session plan' if revisions is None else 'ACP turn plan history', rows=rows,
                  columns=[ItemColumn('status', width=14), ItemColumn('priority', width=10),
                           ItemColumn('content', width=80), ItemColumn('revision', width=10),
                           ItemColumn('time', width=24)])
    sheet.runtime = runtime
    sheet.options.wrap = True
    sheet.options.default_height = 3
    return session_commands(sheet)


def make_terminals_sheet(runtime: Any) -> Any:
    from visidata import ItemColumn, Sheet
    class TerminalsSheet(Sheet):
        def openRow(self, row, rowidx=None):
            runtime.open_terminal_output(row['terminal_id'])
    sheet = TerminalsSheet('ACP terminals', rows=runtime.store.terminals_snapshot(), columns=[
        ItemColumn('command', width=60), ItemColumn('status', width=16),
        ItemColumn('text', width=80), ItemColumn('terminal_id', width=38)])
    sheet.runtime = runtime
    return session_commands(sheet)


def make_pending_sheet(runtime: Any) -> Any:
    from visidata import ItemColumn, Sheet
    class PendingSheet(Sheet):
        def openRow(self, row, rowidx=None):
            # Browsing another request never changes the follow-up answer target.
            from visidata import TextSheet, vd
            vd.push(TextSheet('Pending request (preview only)', source=row['question'].splitlines()))
    sheet = PendingSheet('ACP pending requests (arrival order)', rows=runtime.pending_snapshot(), columns=[
        ItemColumn('position', width=10), ItemColumn('source', width=35), ItemColumn('kind', width=14),
        ItemColumn('question', width=90), ItemColumn('question_number', width=10), ItemColumn('question_count', width=10)])
    sheet.runtime = runtime
    return session_commands(sheet)


def make_form_field_sheet(runtime: Any, state: dict[str, Any]) -> Any:
    import json
    from visidata import Sheet, ItemColumn
    schema = state['schema']
    default = schema.get('default', '')
    value = default if isinstance(default, str) else json.dumps(default)
    class FormFieldSheet(Sheet):
        def openRow(self, row, rowidx=None):
            runtime.submit_ui_answer(state, str(row['value']))
    sheet = FormFieldSheet('ACP form field · edit value with e, Enter to submit', rows=[{
        'field': state['field'], 'value': value, 'question': state['question'],
        'choices': ', '.join(option['label'] for option in state['options']),
        'required': state['required'],
    }], columns=[ItemColumn('field', width=20), ItemColumn('value', width=30),
                ItemColumn('question', width=70), ItemColumn('choices', width=40), ItemColumn('required', width=10)])
    sheet.cursorVisibleColIndex = 1
    sheet.options.wrap = True
    sheet.options.default_height = 10
    sheet.runtime = runtime
    sheet.addCommand('e', 'jusi-acp-edit-form-value',
                     'columns[1].setValues([cursorRow], editCell(1))', 'edit this short field value')
    sheet.addCommand('x', 'jusi-acp-decline-form', 'sheet.runtime.submit_ui_answer(sheet.answer_state, "/decline")', 'decline this request')
    sheet.answer_state = state
    return session_commands(sheet, editable=True)
