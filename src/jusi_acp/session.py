"""Session-owned projections. Caller serializes access; the journal owns history."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Any


def transcript(data: bytes) -> str:
    """Render inert text, never terminal control strings in the hosting terminal."""
    text = data.decode('utf-8', errors='replace')
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)', '', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]?', '', text)
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) < 160))


@dataclass
class SessionProjection:
    entities: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    activity: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    terminal_bytes: dict[str, bytes] = field(default_factory=dict)
    _legacy_message: int = 0
    _last_kind: str = ''

    def apply(self, row: dict[str, Any]) -> None:
        raw, kind = row['raw'], row['type']
        category, identity = '', ''
        if kind in {'user_message', 'agent_message', 'agent_thought', 'user_message_chunk',
                    'agent_message_chunk', 'agent_thought_chunk', 'user_prompt'}:
            category = 'message'
            identity = raw.get('messageId', '')
            if not identity:
                if kind != self._last_kind or not kind.endswith('_chunk'):
                    self._legacy_message += 1
                identity = f'local:{self._legacy_message}'
        elif kind in {'tool_call', 'tool_call_update', 'tool_call_content_chunk'}:
            category, identity = 'tool', raw.get('toolCallId', '')
        elif kind in {'plan', 'plan_update'}:
            category, identity = 'plan', (raw.get('plan') or {}).get('planId', 'legacy-plan')
        elif kind in {'terminal', 'terminal_update', 'terminal_output_chunk'}:
            category, identity = 'terminal', raw.get('terminalId', '')
        self._last_kind = kind
        if kind in {'state_update', 'usage_update', 'session_info_update', 'config_option_update',
                    'available_commands_update'}:
            self.state[kind] = deepcopy(raw)
        if not category or not identity:
            self.activity.append(deepcopy(row))
            return
        key = category, identity
        entity = self.entities.get(key)
        fresh = entity is None
        if fresh:
            entity = deepcopy(row)
            entity.update(category=category, identity=identity, raw={}, text='')
            self.entities[key] = entity
            self.activity.append(entity)
        assert entity is not None
        previous_status = entity.get('status')
        previous_text = entity.get('text')
        value = entity['raw']
        if category == 'plan':
            value.update(deepcopy(raw.get('plan', raw)))
            entity['title'] = f'Plan {identity}'
            entries = value.get('entries') or []
            entity['text'] = '\n'.join(f"{entry.get('status', '')}: {entry.get('content', '')}" for entry in entries)
            entity['status'] = f"{sum(e.get('status') == 'completed' for e in entries)}/{len(entries)} completed"
        elif category == 'terminal':
            if kind == 'terminal':
                value.update(deepcopy(raw))
                self.terminal_bytes[identity] = raw.get('output', '').encode('utf-8')
            elif kind == 'terminal_update':
                value.update(deepcopy(raw))
                if 'output' in raw:
                    output = raw['output']
                    self.terminal_bytes[identity] = base64.b64decode(output['data'], validate=True) if output else b''
            else:
                chunk = base64.b64decode(raw['data'], validate=True)
                self.terminal_bytes[identity] = self.terminal_bytes.get(identity, b'') + chunk
            entity['text'] = transcript(self.terminal_bytes.get(identity, b''))
            entity['command'] = value.get('command') or ''
            entity['cwd'] = value.get('cwd') or ''
            entity['title'] = f'Terminal {identity}'
            entity['status'] = value.get('status') or (
                'exited: ' + str(value['exitStatus']) if value.get('exitStatus') is not None else 'exit not reported')
            entity['terminal_id'] = identity
        else:
            if kind.endswith('_chunk'):
                content = raw.get('content')
                if content is not None:
                    value.setdefault('content', [])
                    if value['content'] is None:
                        value['content'] = []
                    value['content'].append(deepcopy(content))
                value.update({k: deepcopy(v) for k, v in raw.items() if k != 'content'})
            else:
                value.update(deepcopy(raw))
            entity['title'] = value.get('title') or row.get('title') or identity
            entity['text'] = content_text(value.get('content')) or str(value.get('text') or '')
            entity['status'] = value.get('status') or ''
            entity['tool'] = value.get('name') or value.get('kind') or ''
            entity['input'] = str(value.get('rawInput') or '')
            entity['tool_call_id'] = value.get('toolCallId') or ''
            from .state import _diffs
            entity['diffs'] = _diffs(value)
        entity['time'] = row.get('time', '')
        if not fresh and category in {'tool', 'plan', 'terminal'} and (previous_status != entity['status'] or (category == 'plan' and previous_text != entity['text'])):
            notice = deepcopy(row)
            notice.update(title=entity['title'], text=entity['status'], category=category,
                          identity=identity, status=entity['status'], type='activity_update')
            self.activity.append(notice)

    def snapshot(self, category: str | None = None) -> list[dict[str, Any]]:
        return deepcopy(self.activity if category is None else [
            entity for (kind, _), entity in self.entities.items() if kind == category
        ])


def content_text(content: Any) -> str:
    if isinstance(content, dict):
        if content.get('type') == 'text':
            return str(content.get('text', ''))
        return content_text(content.get('content'))
    if isinstance(content, list):
        return ''.join(content_text(item) for item in content)
    return ''
