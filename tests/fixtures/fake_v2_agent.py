"""Wire-level v2 fixture, intentionally independent of SDK response models."""
import asyncio
import base64
import json
import sys

pending = {}
serial = 0


def send(value):
    print(json.dumps(value), flush=True)


def update(kind, **fields):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
        'sessionId': 'v2-session', 'update': {'sessionUpdate': kind, **fields}}})


async def request(key, **params):
    future = asyncio.get_running_loop().create_future()
    pending[key] = future
    send({'jsonrpc': '2.0', 'id': key, 'method': 'elicitation/create', 'params': params})
    return await future


async def background(body):
    update('state_update', state='running')
    update('plan_update', plan={'type': 'items', 'planId': 'plan-1', 'entries': [
        {'content': 'Background checks', 'priority': 'high', 'status': 'in_progress'}]})
    update('terminal_update', terminalId='terminal-1', command='tests', cwd='/tmp')
    update('state_update', state='idle', stopReason='end_turn')
    await asyncio.sleep(.05)
    if body == 'questions':
        first = asyncio.create_task(request('first', mode='form', sessionId='v2-session', message='First question\nMore context',
            requestedSchema={'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}))
        await asyncio.sleep(.02)
        second = asyncio.create_task(request('second', mode='form', sessionId='v2-session', toolCallId='worker', message='Background question',
            requestedSchema={'type': 'object', 'properties': {'count': {'type': 'integer', 'minimum': 1}}, 'required': ['count']}))
        result = await first
        update('agent_message', messageId='answer-first', content=[{'type': 'text', 'text': json.dumps(result)}])
        result = await second
        update('agent_message', messageId='answer-second', content=[{'type': 'text', 'text': json.dumps(result)}])
    update('terminal_output_chunk', terminalId='terminal-1', data=base64.b64encode('late ✓\n'.encode()).decode())
    update('terminal_update', terminalId='terminal-1', exitStatus={'exitCode': 0})
    update('plan_update', plan={'type': 'items', 'planId': 'plan-1', 'entries': [
        {'content': 'Background checks', 'priority': 'high', 'status': 'completed'}]})
    update('agent_message', messageId='late-message', content=[{'type': 'text', 'text': 'Background finished'}])


async def main():
    global serial
    tasks = set()
    while line := await asyncio.to_thread(sys.stdin.readline):
        message = json.loads(line)
        if 'method' not in message:
            future = pending.pop(message['id'], None)
            if future is not None:
                future.set_result(message.get('result'))
            continue
        method = message['method']
        params = message.get('params', {})
        result = {}
        if method == 'initialize':
            assert params['protocolVersion'] == 2
            result = {'protocolVersion': 2, 'info': {'name': 'fixture', 'version': '1'}, 'capabilities': {'session': {}}}
        elif method == 'session/new':
            result = {'sessionId': 'v2-session'}
        elif method == 'session/list':
            result = {'sessions': [{'sessionId': 'v2-session', 'cwd': '/tmp', 'title': 'V2 work'}]}
        elif method == 'session/resume':
            if params.get('replayFrom'):
                update('agent_message', messageId='replayed', content=[{'type': 'text', 'text': 'Replayed once'}])
        elif method == 'session/prompt':
            serial += 1
            message_id = f'user-{serial}'
            update('user_message', messageId=message_id, content=params['prompt'])
            result = {'messageId': message_id}
            task = asyncio.create_task(background(params['prompt'][0]['text']))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        elif method == 'session/cancel':
            update('state_update', state='idle', stopReason='cancelled')
        if 'id' in message:
            send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})

asyncio.run(main())
