# jusi-acp

`jusi-acp` is the shared Agent Client Protocol family for Jusi 1.0. It gives
independently installed ACP agent providers one `%%acp` notebook experience:
structured VisiData events, durable follow-ups, exact cancellation, permission
choices, client-owned command execution, session controls, completion, and
editor-native copy/open/diff actions.

This package is a family library, not an exact provider. Installing it alone
does not add a Jusi catalog entry. An exact package such as a future
`jusi-gigacode` depends on `jusi-acp` and supplies its own catalog entry,
attesting kernel module, launch validation, and worker factory.

## Configuration

Jusi loads `~/.jusi/jusi.toml` at the kernel target. ACP uses the standard
`[magic.alias]` layout:

```toml
[acp.work]
provider = "gigacode"
path = "/work/project"
additional_directories = ["../shared"]
model = "provider-owned-model-name"
```

`provider`, `path`, `additional_directories`, and `mcp_servers` are common
family keys. The selected exact provider validates the complete table and owns
the remaining keys. Configuration is frozen for one notebook runtime; use a
full Jusi restart to reload it.

```python
%%acp work
Inspect the project and implement the requested change.
```

Resume syntax is explicit:

```python
%%acp work --load SESSION_ID
Continue after replaying agent history.
```

```python
%%acp work --resume SESSION_ID
Continue without requesting history replay.
```

## Exact provider integration

The exact package advertises the shared claim and wraps its identity around the
family kernel adapter:

```python
# catalog.py
from jusi_acp import family_claim

def catalog_entry():
    return {
        "plugin_id": "gigacode",
        "plugin_version": __version__,
        "distribution": "jusi-gigacode",
        "families": [family_claim()],
        "kernel_extensions": ["jusi_gigacode.kernel"],
        "worker_entry_point": "jusi_gigacode.worker:create_worker",
        "media_types": ["text/x-ansi"],
        "interaction": "terminal_interactive",
    }
```

```python
# kernel.py
from jusi_acp import KernelProviderAdapter

_adapter = KernelProviderAdapter("gigacode", __version__)
jusi_kernel_adapter_v1 = _adapter.manifest
configure_jusi_runtime_v1 = _adapter.configure
load_ipython_extension = _adapter.load_ipython_extension
```

```python
# worker.py
from pathlib import Path
from jusi_acp import ACPWorker, AgentLaunch, ProviderSpec

def resolve_launch(config, cwd: Path):
    return AgentLaunch(("gigacode", "--acp"), cwd)

PROVIDER = ProviderSpec("gigacode", __version__, resolve_launch)

def create_worker(context):
    return ACPWorker(context, PROVIDER)
```

The provider must use the same ID and version in its catalog, kernel adapter,
and `ProviderSpec`. It must not register `%%acp` itself or store live sessions
globally.

## Shared behavior

- ACP v1 over the official Python SDK.
- Python 3.10 or newer.
- `execute`, `followup`, `complete`, `interrupt`, and `editor_actions` for every
  exact provider claiming the family.
- Markdown syntax and indentation in ACP prompt cells.
- ACP terminal methods run bounded, non-interactive subprocesses at the target.
- ACP filesystem, elicitation, and terminal-auth capabilities are not
  advertised in the initial release.
- Agent-driven authentication is available through `/auth METHOD_ID` or an
  exact provider's configured `AgentLaunch.auth_method`.
- `/mode ID`, `/config ID VALUE`, and `/cancel` are family commands.
- Initial bodies and later follow-ups use the same family-command dispatcher.
- ACP `available_commands_update` entries appear in completion and in the event
  sheet with their descriptions and optional free-text input hints. Invoking an
  advertised slash command sends its complete text as a regular ACP prompt;
  command-specific behavior and output remain agent-owned.
- Diff content opens through Jusi's acknowledged, remote-safe read-only diff
  action.
- ACP remains authoritative for conversation context. The family stores a
  normalized read-only presentation cache so resume-only sessions retain useful
  review history.

The first turn begins inside the terminal application after Jusi creates the
client. It is cancellable with the application's `c` command or `/cancel`, but
is not published as a Jusi client operation. Later follow-ups are exact Jusi
operations and support `:JusiInterrupt` without closing the session.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
