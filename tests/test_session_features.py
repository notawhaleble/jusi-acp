from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest
from acp.schema import ElicitationFormSessionMode, ElicitationUrlSessionMode, PermissionOption, ToolCallUpdate
from jusi.protocol import validate_completion

from jusi_acp.application import ACPApplication
from jusi_acp.state import EventStore
from test_application import _payload


def test_late_events_keep_known_ownership_and_group_unassigned(tmp_path, monkeypatch):
    monkeypatch.setenv('JUSI_STATE_HOME', str(tmp_path / 'state'))
    store = EventStore('test', tmp_path)
    store.bind_session('session', load_cache=False)
    store.add({'kind': 'user_prompt', 'text': 'first'})
    store.add({'sessionUpdate': 'tool_call', 'toolCallId': 'worker', 'title': 'Worker', 'status': 'in_progress'})
    store.add({'sessionUpdate': 'agent_message_chunk', 'messageId': 'reply', 'content': {'type': 'text', 'text': 'First'}})
    store.add({'kind': 'turn_stopped', 'stopReason': 'end_turn'})
    store.add({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Unattributed'}})
    store.add({'sessionUpdate': 'plan', 'entries': [{'content': 'background', 'status': 'pending'}]})
    store.add({'kind': 'user_prompt', 'text': 'second'})
    store.add({'sessionUpdate': 'tool_call_update', 'toolCallId': 'worker', 'status': 'completed'})
    store.add({'sessionUpdate': 'agent_message_chunk', 'messageId': 'reply', 'content': {'type': 'text', 'text': ' continued'}})
    store.add({'kind': 'turn_stopped', 'stopReason': 'end_turn'})
    assert store.turns_snapshot()[0]['reply'] == 'First continued'
    assert store.turns_snapshot()[0]['updates'] == 2
    assert store.turns_snapshot()[1]['reply'] == ''
    timeline = store.timeline_snapshot()
    assert len(timeline) == 3
    assert timeline[1]['prompt'] == 'Unassigned session activity'
    assert len(store.activity_snapshot(timeline[1]['activity_id'])) == 2
    assert next(r for r in store.turn_events_snapshot(1) if r['tool_call_id'])['status'] == 'completed'
    cached = EventStore('test', tmp_path)
    cached.bind_session('session', load_cache=True)
    assert cached.timeline_snapshot() == timeline
    assert cached.plan_snapshot() == store.plan_snapshot()


def test_plan_is_replaced_and_turn_revisions_are_snapshots(tmp_path):
    store = EventStore('test', tmp_path, persist=False)
    store.add({'kind': 'user_prompt', 'text': 'plan'})
    store.add({'sessionUpdate': 'plan', 'entries': [{'content': 'one', 'status': 'pending'}]})
    store.add({'sessionUpdate': 'plan', 'entries': [{'content': 'one', 'status': 'completed'}]})
    store.add({'kind': 'turn_stopped', 'stopReason': 'end_turn'})
    store.add({'sessionUpdate': 'plan', 'entries': []})
    assert store.plan_snapshot() == []
    revisions = store.plan_snapshot(1)
    assert [r['entries'][0]['status'] for r in revisions] == ['pending', 'completed']
    assert len(store.timeline_snapshot()) == 2


def test_terminal_stays_with_originating_turn_and_does_not_overwrite_tool(tmp_path):
    store = EventStore('test', tmp_path, persist=False)
    store.add({'kind': 'user_prompt', 'text': 'start process'})
    store.update_terminal('term', '', False, 'running', command='build')
    store.add({'sessionUpdate': 'tool_call', 'toolCallId': 'build', 'content': [{'type': 'terminal', 'terminalId': 'term'}]})
    store.add({'kind': 'turn_stopped', 'stopReason': 'end_turn'})
    store.add({'kind': 'user_prompt', 'text': 'unrelated'})
    store.update_terminal('term', 'done', True, 'exit 0')
    assert store.terminals_snapshot()[0]['command'] == 'build'
    assert store.terminals_snapshot()[0]['text'] == 'done'
    assert len([r for r in store.raw_snapshot() if r['type'] == 'tool_call']) == 1
    assert len([r for r in store.turn_events_snapshot(1) if r['type'] == 'tool_call' and r['tool_call_id'] == 'build']) == 1
    assert not any(r.get('terminal_id') for r in store.turn_events_snapshot(2))


def _question(app, name, count=1):
    return app.request_permission('session', ToolCallUpdate(
        tool_call_id=name, title=name,
        raw_input={'questions': [{'question': f'{name} {n}', 'options': []} for n in range(count)]}),
        [PermissionOption(option_id='yes', kind='allow_once', name='Answer')])


def test_concurrent_idle_questions_remain_fifo_and_preserve_active_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = 'session'
    async def run():
        first = asyncio.create_task(_question(app, 'first', 2))
        await asyncio.sleep(0)
        active = app._answer_state
        second = asyncio.create_task(_question(app, 'worker'))
        await asyncio.sleep(0)
        assert app._answer_state is active
        assert active['pending_count'] == 1
        next_state = await app._prompt_or_command('α\n multiline')
        assert next_state['question'] == 'first 1'
        next_state = await app._prompt_or_command('second answer')
        assert next_state['question'] == 'worker 0'
        assert (await first).answers == {'0': 'α\n multiline', '1': 'second answer'}
        assert await app._prompt_or_command('worker answer') == {'status': 'ready', 'session_id': 'session'}
        assert (await second).answers == {'0': 'worker answer'}
        assert not app.store.turns_snapshot()
    asyncio.run(run())


def test_withdrawn_question_does_not_redirect_text_to_next_request(tmp_path, monkeypatch):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = 'session'
    async def run():
        first = asyncio.create_task(_question(app, 'first'))
        second = asyncio.create_task(_question(app, 'second'))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        with pytest.raises(ValueError, match='withdrawn'):
            await app._prompt_or_command('old answer')
        assert not second.done()
        assert not app._questions[app._answer_state['request_id']].answers
        assert (await app._prompt_or_command('/next'))['question'] == 'second 0'
        await app._prompt_or_command('new answer')
        assert (await second).answers == {'0': 'new answer'}
    asyncio.run(run())


def test_elicitation_validates_types_preserves_text_and_supports_optional_fields(tmp_path, monkeypatch):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = 'session'
    async def run():
        request = asyncio.create_task(app.create_elicitation('Configure', ElicitationFormSessionMode(
            session_id='session', requested_schema={'type': 'object', 'properties': {
                'count': {'type': 'integer', 'minimum': 1, 'maximum': 3},
                'notes': {'type': 'string', 'minLength': 2},
                'optional': {'type': 'boolean'},
            }, 'required': ['count', 'notes']})))
        await asyncio.sleep(0)
        for invalid in ['true', '0', '4', 'word']:
            with pytest.raises(ValueError):
                await app._prompt_or_command(invalid)
        assert app._answer_state['field'] == 'count'
        await app._prompt_or_command('2')
        await app._prompt_or_command('  α\nlong answer  ')
        await app._prompt_or_command('/skip')
        response = await request
        assert response.action == 'accept'
        assert response.content == {'count': 2, 'notes': '  α\nlong answer  '}
        assert not app._permissions
    asyncio.run(run())


@pytest.mark.parametrize('command, action', [('/accept', 'accept'), ('/decline', 'decline'), ('/dismiss', 'cancel')])
def test_url_elicitation_has_explicit_acknowledgement(tmp_path, monkeypatch, command, action):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = 'session'
    async def run():
        request = asyncio.create_task(app.create_elicitation('Visit this URL', ElicitationUrlSessionMode(
            session_id='session', elicitation_id='login', url='https://example.com/login')))
        await asyncio.sleep(0)
        assert 'https://example.com/login' in app._answer_state['question']
        await app._prompt_or_command(command)
        assert (await request).action == action
        await app.complete_elicitation('login')
        assert app.store.raw_snapshot()[-1]['title'] == 'Elicitation completed'
    asyncio.run(run())


def test_path_completion_respects_gitignore_ranges_and_pending_answers(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('ignored/\n')
    (tmp_path / 'ignored').mkdir()
    (tmp_path / 'ignored/secret.txt').write_text('hidden')
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/α file.py').write_text('data')
    app = ACPApplication(_payload(tmp_path))
    app._answer_state = {'options': []}
    prefix = 'Explain α\n@src/α'
    result = app.handle_operation('complete', {'prefix': prefix, 'cursor_pos': len(prefix), 'body': prefix + 'SUFFIX'})
    assert validate_completion(result, len(prefix)) == result
    item = result['items'][0]
    assert item['text'] == '@src/α\\ file.py'
    assert prefix[:item['start']] + item['text'] + 'SUFFIX' == 'Explain α\n@src/α\\ file.pySUFFIX'
    assert not any('ignored' in item['text'] for item in app.handle_operation('complete', {'prefix': '@'})['items'])
    assert app.path_completer.complete('@src/α\\ file.py', 15)[0]['text'] == '@src/α\\ file.py'
    assert app.path_completer.complete('email@example.com', 17) is None
    assert app.path_completer.complete('', 0) is None
    assert app.path_completer.complete('Look ./src/', 11)[0]['text'] == './src/α\\ file.py'


def test_reply_preview_returns_to_its_turn_and_does_not_accumulate(tmp_path, monkeypatch):
    from visidata import vd
    from jusi_acp.ui import make_turns_sheet
    app = ACPApplication(_payload(tmp_path))
    monkeypatch.setattr(vd, 'sheets', [])
    monkeypatch.setattr(vd, 'push', lambda sheet: vd.sheets.insert(0, sheet) if sheet not in vd.sheets else None)
    monkeypatch.setattr(vd, 'remove', lambda sheet: vd.sheets.remove(sheet))
    app.turns_sheet = make_turns_sheet(app)
    for number in (1, 2):
        app.store.add({'kind': 'user_prompt', 'text': str(number)})
        app._show_live_turn(number)
        assert app.reply_sheet is None
        app.store.add({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Final\nreply'}})
        app.store.add({'kind': 'turn_stopped', 'stopReason': 'end_turn'})
        app._show_completed_turn(number)
        assert vd.sheets[0] is app.reply_sheet
        assert app.reply_sheet.source == ['Final', 'reply']
        assert app.turns_sheet.cursorRow['turn'] == number
        assert len(vd.sheets) == 2
    vd.remove(app.reply_sheet)
    assert vd.sheets[0] is app.turns_sheet


def test_real_v1_wire_elicitation_and_background_requests(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv('JUSI_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.start()
    try:
        state = app.handle_operation('followup', {'body': 'elicitation-form'})
        assert state['kind'] == 'form'
        assert state['field'] == 'count'
        state = app.handle_operation('followup', {'body': '2'})
        assert state['field'] == 'notes'
        assert app.handle_operation('followup', {'body': '  multiline\nα  '})['stop_reason'] == 'end_turn'
        assert '"count": 2' in app.store.turns_snapshot()[0]['reply']
        state = app.handle_operation('followup', {'body': 'elicitation-url'})
        assert state['kind'] == 'url'
        assert app.handle_operation('followup', {'body': '/accept'})['stop_reason'] == 'end_turn'
        assert app.store.turns_snapshot()[1]['reply'] == 'URL:accept'
        assert app.handle_operation('followup', {'body': 'background'})['stop_reason'] == 'end_turn'
        deadline = time.monotonic() + 5
        while app._answer_state is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app._answer_state['question'] == 'Background follow-up?'
        assert app._turn_task.done()
        assert app.store.plan_snapshot()[0]['status'] == 'completed'
        assert app.store.turns_snapshot()[2]['updates'] > 0
        assert len(app.store.turns_snapshot()) == 3
        assert app.handle_operation('followup', {'body': 'Proceed'})['status'] == 'ready'
        assert app.handle_operation('followup', {'body': 'healthy'})['stop_reason'] == 'end_turn'
    finally:
        app.close()


def test_permissions_share_fifo_with_questions(tmp_path, monkeypatch):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = 'session'
    async def run():
        permission = asyncio.create_task(app.request_permission('session', ToolCallUpdate(
            tool_call_id='approval', title='Allow command?'), [PermissionOption(
                option_id='allow', kind='allow_once', name='Allow')]))
        await asyncio.sleep(0)
        active = app._answer_state
        question = asyncio.create_task(_question(app, 'later'))
        await asyncio.sleep(0)
        assert app._answer_state is active and active['kind'] == 'permission'
        assert active['pending_count'] == 1
        with pytest.raises(ValueError, match='VisiData'):
            await app._prompt_or_command('unrelated text')
        app.select_permission(active['request_id'], 'allow')
        assert (await permission).outcome.option_id == 'allow'
        assert app._answer_state['question'] == 'later 0'
        await app._prompt_or_command('answer')
        await question
    asyncio.run(run())


def test_idle_terminal_finish_preserves_owner_after_cache_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('JUSI_STATE_HOME', str(tmp_path / 'state'))
    store = EventStore('test', tmp_path)
    store.bind_session('terminal-cache', load_cache=False)
    store.add({'kind': 'user_prompt', 'text': 'start'})
    store.update_terminal('term', '', False, 'running', command='sleep')
    store.add({'kind': 'turn_stopped', 'stopReason': 'end_turn'})
    store.update_terminal('term', 'finished', False, 'exit 0')
    replay = EventStore('test', tmp_path)
    replay.bind_session('terminal-cache', load_cache=True)
    assert len(replay.timeline_snapshot()) == 1
    assert replay.terminals_snapshot()[0]['text'] == 'finished'
    assert replay.turn_events_snapshot(1)[1]['text'] == 'finished'
