"""Target-side project path completion; selecting a file never attaches content."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

_TOKEN = re.compile(r'(?:^|\s)(?P<token>@(?:\\[^\n]|[^\s])*|\.{1,2}/(?:\\[^\n]|[^\s])*)$')
_EXCLUDED = {'.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__'}


class PathCompleter:
    def __init__(self, roots: tuple[Path, ...]) -> None:
        self.roots = roots
        self._cached: list[str] = []
        self._expires = 0.0

    def complete(self, prefix: str, cursor: int) -> list[dict[str, Any]] | None:
        match = _TOKEN.search(prefix)
        if match is None:
            return None
        token = match['token']
        typed = token[1:] if token.startswith('@') else token
        typed = re.sub(r'\\(.)', r'\1', typed)
        normalized = typed.removeprefix('./').casefold()
        directory, _, name = normalized.rpartition('/')
        paths = self._paths()
        candidates = []
        for path in paths:
            parent, _, basename = path.rstrip('/').casefold().rpartition('/')
            if parent == directory and name in basename:
                candidates.append(path)
        candidates.sort(key=lambda p: (not p.casefold().startswith(normalized), p.casefold()))
        items = []
        for path in candidates[:100]:
            if token.startswith('@'):
                text = '@' + ('./' if typed.startswith('./') else '') + path
            else:
                text = path if path.startswith('../') else './' + path
            text = text.replace('\\', '\\\\').replace(' ', '\\ ')
            items.append({'text': text, 'label': path, 'detail': 'Project directory' if path.endswith('/') else 'Project file path',
                          'kind': 'text', 'start': match.start('token'), 'end': cursor})
        return items

    def _paths(self) -> list[str]:
        if time.monotonic() < self._expires:
            return self._cached
        paths: set[str] = set()
        for root in self.roots:
            try:
                result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                                         '--exclude-standard', '--', '.'], capture_output=True, timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                names = result.stdout.decode('utf-8', errors='replace').split('\0')
            else:
                names = []
                for base, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED and not d.startswith('.'))
                    names.extend(str((Path(base) / f).relative_to(root)) for f in files)
                    if len(names) >= 20000:
                        break
            for name in names[:20000]:
                if not name:
                    continue
                target = root / name
                if not target.is_file():
                    continue
                relative = os.path.relpath(target, self.roots[0])
                paths.add(relative)
                parent = Path(relative).parent
                while str(parent) not in {'.', '..'}:
                    paths.add(str(parent) + '/')
                    parent = parent.parent
        self._cached = sorted(paths)
        self._expires = time.monotonic() + 2
        return self._cached
