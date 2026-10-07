from __future__ import annotations
import asyncio
import base64
from pathlib import Path
import time

import pytest
from jusi_acp.application import ACPApplication
from jusi_acp.state import EventStore
from test_application import _payload


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(.01)


def test_session_entities_survive_idle_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('JUSI_STATE_HOME', str(tmp_path / 'state'))
    store = EventStore('fixture', tmp_path)
    store.bind_session('session', load_cache=False)
    store.add({'sessionUpdate': 'state_update', 'state': 'idle', 'stopReason': 'end_turn'})
    store.add({'sessionUpdate': 'agent_message_chunk', 'messageId': 'm', 'content': {'type': 'text', 'text': 'hello'}})
    store.add({'sessionUpdate': 'agent_message', 'messageId': 'm', 'content': None})
    store.add({'sessionUpdate': 'agent_message_chunk', 'messageId': 'm', 'content': {'type': 'text', 'text': 'corrected'}})
    store.add({'sessionUpdate': 'tool_call_update', 'toolCallId': 't', 'title': 'Original', 'rawInput': {'a': 1}})
    store.add({'sessionUpdate': 'tool_call_update', 'toolCallId': 't', 'title': None, 'status': 'completed'})
    assert store.activity_snapshot('message')[0]['text'] == 'corrected'
    assert store.activity_snapshot('tool')[0]['raw']['title'] is None
    assert store.activity_snapshot('tool')[0]['raw']['rawInput'] == {'a': 1}
    assert store.active_turn is None
    reloaded = EventStore('fixture', tmp_path)
    reloaded.bind_session('session', load_cache=True)
    assert [{k: v for k, v in r.items() if k != "cached"} for r in reloaded.activity_snapshot()] == [
        {k: v for k, v in r.items() if k != "cached"} for r in store.activity_snapshot()]


def test_terminal_bytes_and_replacement_are_independent(tmp_path):
    store = EventStore('fixture', tmp_path, persist=False)
    for chunk in [b'\x1b[31m', b'\xe2', b'\x9c\x93', b'\x1b[0m\n']:
        store.add({'sessionUpdate': 'terminal_output_chunk', 'terminalId': 't', 'data': base64.b64encode(chunk).decode()})
    assert store.activity_snapshot('terminal')[0]['text'] == '✓\n'
    store.add({'sessionUpdate': 'terminal_update', 'terminalId': 't', 'output': {'data': base64.b64encode(b'replaced').decode()}})
    assert store.activity_snapshot('terminal')[0]['text'] == 'replaced'
    store.add({'sessionUpdate': 'terminal_update', 'terminalId': 't', 'output': None})
    assert store.activity_snapshot('terminal')[0]['text'] == ''


def v2_app(tmp_path, monkeypatch):
    pytest.importorskip('acp.experimental.v2')
    monkeypatch.setenv('JUSI_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    payload = _payload(tmp_path)
    payload['launch']['protocol_version'] = 2
    payload['launch']['argv'][1] = str(Path(__file__).parent / 'fixtures' / 'fake_v2_agent.py')
    app = ACPApplication(payload)
    app.start()
    assert app._connected.wait(10)
    assert app._failed is None
    return app


def test_v2_prompt_acceptance_background_updates_and_resume(tmp_path, monkeypatch):
    app = v2_app(tmp_path, monkeypatch)
    try:
        result = app.handle_operation('followup', {'body': 'work'})
        assert result['status'] == 'accepted'
        wait_until(lambda: any(row['text'] == 'Background finished' for row in app.store.activity_snapshot('message')))
        assert len([r for r in app.store.activity_snapshot('message') if r['identity'] == result['message_id']]) == 1
        assert app.store.activity_snapshot('terminal')[0]['text'] == 'late ✓\n'
        assert app.store.activity_snapshot('plan')[0]['status'] == '1/1 completed'
        assert app.store.session.state['state_update']['state'] == 'idle'
        app._submit(app._select_session('v2-session'))
        assert [r['text'] for r in app.store.activity_snapshot('message')] == ['Replayed once']
    finally:
        app.close()


def test_v2_concurrent_elicitation_queue_and_review(tmp_path, monkeypatch):
    app = v2_app(tmp_path, monkeypatch)
    try:
        app.handle_operation('followup', {'body': 'questions'})
        wait_until(lambda: len(app._questions) == 2)
        first = app._answer_state['request_id']
        assert app._answer_state['pending_requests'] == 1
        assert 'First question' in app._answer_state['question']
        result = app.handle_operation('followup', {'body': '  multiline\nanswer  '})
        assert result['status'] == 'review'
        app._submit(app._question_action('accept', first))
        second = app._answer_state['request_id']
        assert second != first
        assert 'Background question' in app._answer_state['question']
        with pytest.raises(ValueError):
            app.handle_operation('followup', {'body': '0'})
        assert app._answer_state['request_id'] == second
        assert app.handle_operation('followup', {'body': '3'})['status'] == 'review'
        app._submit(app._question_action('accept', second))
        wait_until(lambda: any(r['identity'] == 'answer-second' for r in app.store.activity_snapshot('message')))
        assert app._answer_state is None
    finally:
        app.close()


def test_withdrawn_request_cannot_redirect_answer(tmp_path, monkeypatch):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = 's'
    async def run():
        first = asyncio.create_task(app.create_elicitation('first', 'form', session_id='s',
            requested_schema={'type': 'object', 'properties': {'text': {'type': 'string'}}}))
        await asyncio.sleep(0)
        second = asyncio.create_task(app.create_elicitation('second', 'form', session_id='s',
            requested_schema={'type': 'object', 'properties': {'text': {'type': 'string'}}}))
        await asyncio.sleep(0)
        identity = app._answer_state['request_id']
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert app._answer_state['status'] == 'withdrawn'
        with pytest.raises(ValueError, match='withdrawn'):
            await app._answer_question('do not redirect')
        await app._question_action('accept', identity)
        assert 'second' in app._answer_state['question']
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
    asyncio.run(run())


def test_url_requires_explicit_consent_and_completion_is_separate(tmp_path, monkeypatch):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    opened = []
    monkeypatch.setattr('webbrowser.open', lambda url: opened.append(url) or True)
    app = ACPApplication(_payload(tmp_path))
    app.session_id = 's'
    async def run():
        task = asyncio.create_task(app.create_elicitation('Authorize', 'url', session_id='s',
            url='https://example.com/authorize', elicitation_id='auth'))
        await asyncio.sleep(0)
        assert not opened
        identity = app._answer_state['request_id']
        with pytest.raises(ValueError, match='confirm'):
            await app._answer_question('yes')
        await app._question_action('accept', identity)
        assert await task == {'action': 'accept'}
        assert opened == ['https://example.com/authorize']
        assert 'auth' in app._url_elicitations
        app.complete_elicitation('unrelated')
        assert 'auth' in app._url_elicitations
        app.complete_elicitation('auth')
        assert not app._url_elicitations
    asyncio.run(run())


def test_form_cell_edits_do_not_skip_unanswered_fields(tmp_path, monkeypatch):
    monkeypatch.setattr('jusi_acp.application.queue_action', lambda action: None)
    app = ACPApplication(_payload(tmp_path))
    async def run():
        task = asyncio.create_task(app.create_elicitation('Details', 'form', request_id=10,
            requested_schema={'type': 'object', 'properties': {
                'first': {'type': 'string'}, 'second': {'type': 'string'}, 'optional': {'type': 'string'}},
                'required': ['first', 'second']}))
        await asyncio.sleep(0)
        key = app._answer_state['request_id']
        app._questions[key].answers['second'] = 'short edit'
        app._publish_question()
        assert app._answer_state['question_number'] == 1
        await app._answer_question('long answer\nfrom notebook')
        assert app._answer_state['question_number'] == 3
        await app._question_action('skip', key)
        assert app._answer_state['status'] == 'review'
        await app._question_action('accept', key)
        assert (await task)['content'] == {'first': 'long answer\nfrom notebook', 'second': 'short edit'}
    asyncio.run(run())


def test_sdk_null_patch_is_not_dropped(tmp_path):
    schema = pytest.importorskip('acp.experimental.v2.schema')
    store = EventStore('fixture', tmp_path, persist=False)
    store.add(schema.SessionToolCallUpdate(tool_call_id='t', title='Before'))
    store.add(schema.SessionToolCallUpdate(tool_call_id='t', title=None))
    assert store.activity_snapshot('tool')[0]['raw']['title'] is None


def test_v2_structured_diff_preserves_patch_and_operation(tmp_path):
    store = EventStore('fixture', tmp_path, persist=False)
    store.add({'sessionUpdate': 'tool_call_update', 'toolCallId': 't', 'content': [{
        'type': 'diff', 'changes': [{'operation': 'move', 'oldPath': '/a', 'path': '/b'}],
        'patch': {'format': 'git_patch', 'text': 'diff --git a/a b/b\nrename from a\nrename to b'}}]})
    diff = store.activity_snapshot('tool')[0]['diffs'][0]
    assert diff['change'] == 'move'
    assert diff['old_path'] == '/a'
    assert diff['patch']['text'].startswith('diff --git')
