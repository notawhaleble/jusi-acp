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

To browse sessions without putting an ID in the magic header, start a cell with:

```python
%%acp work
/sessions
```

Enter on a session loads its history into the turns sheet. Later Jusi follow-ups
continue that selected session.
While loading, follow-ups and further selections are rejected with a wait message.
A failed load preserves the previous session and its displayed history.
The agent must advertise `session/list` and either `session/load` or `session/resume`.

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
- `/sessions`, `/mode ID`, `/config ID VALUE`, and `/cancel` are family commands.
- Initial bodies and later follow-ups use the same family-command dispatcher.
- ACP `available_commands_update` entries appear in completion and in the event
  sheet with their descriptions and optional free-text input hints. Invoking an
  advertised slash command sends its complete text as a regular ACP prompt;
  command-specific behavior and output remain agent-owned.
- Press `d` on a tool event to open its ACP-provided diff. Press `d` on a turn
  to browse all files changed during that turn, then Enter to open one through
  Jusi's acknowledged, remote-safe read-only Vim diff action. Continuous edits
  to one path are composed; discontinuous revisions remain separate.
- ACP remains authoritative for conversation context. The family stores a
  normalized read-only presentation cache so resume-only sessions retain useful
  review history.
- The live event view groups adjacent assistant and thought chunks, consolidates
  tool updates by call ID, and updates terminal output in place while retaining
  every raw ACP update in the durable journal.
- Completed prompts appear as one row each in a turns sheet. Enter opens that
  turn's grouped event view; turn completion returns focus to the summary.

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

The notebook follow-up integration test uses a sibling `jusi` frontend checkout,
or the path supplied in `JUSI_NVIM_ROOT`, together with the installed Python
backend. It edits notebook cells through Neovim and checks the real serialized
follow-up lane, answer history, and same-client reuse.

### Turn review and questions

The turns sheet records the model ID reported by the agent, from either model
configuration or older ACP session model metadata. It stays blank when the
agent does not report a model; it is not inferred from the executable name.
Tool events show the tool name/kind, command, and raw input when supplied.
The reply column contains the final agent message from each turn, and `changes`
counts paths for which the agent supplied ACP diff content. Enter opens the full
event details. An empty changes view means the agent supplied no diffs; it does
not prove that no files changed.

Qwen/GigaCode question requests carried in `rawInput.questions` pause the agent
and show the current question and choices in a read-only view. Type your answer
in the notebook cell in Vim and submit it using the usual Jusi follow-up action.
Each follow-up answers one question. Write the option label (available in cell
completion) or any free-form, multiline answer; text and whitespace are preserved.
For multiple-choice questions, describe all your choices in the answer. An empty
answer is rejected without advancing to the next question.

When a question arrives, the current Jusi operation returns successfully with
`status: awaiting_answer`, freeing the follow-up lane while keeping the ACP
request pending. After the last answer, that same ACP turn resumes, and the
answer follow-up stays active until the turn finishes or asks another question.
Answers remain in the original turn's event history and in normal Jusi follow-up
history; they never become separate agent prompts. Once the turn finishes, the
next follow-up starts a new prompt as usual.

Submit `/cancel` from the cell to cancel a waiting turn, including any partially
answered question set. Closing the question view just hides it; it does not
answer or cancel anything. While waiting, all other cell text is an answer,
including slash-prefixed text. During resumed work, use `:JusiInterrupt` as usual.
While waiting for input there is no active Jusi operation to interrupt, so use
`/cancel` (or `c` in the question view). No text entry in VisiData is required.

Ordinary tool permission requests retain their approval choices, with `d`
exposing the full request payload. Questions use the
[Qwen permission extension](https://github.com/QwenLM/qwen-code/blob/main/packages/vscode-ide-companion/src/services/acpConnection.ts),
not generic ACP elicitation.

ACP UI updates run from the drawing thread with a bounded curses polling interval,
including while the terminal has no focus. Recoverable SDK protocol errors stay
in event diagnostics; runtime failures use VisiData's Ctrl-E error history. Agent
stderr appears as labelled diagnostic events, including during authentication.
Browser launcher environment variables are preserved, and provider environment
overrides take precedence.

Qwen's `qwen/notify/session/prompt-suggestion` notification is accepted as an
advisory `Suggested follow-up` journal event. It never submits a prompt or edits
the notebook, and does not replace the completed turn's reply.

To collect the last ten saved ACP diagnostics on the target machine, run
`python -m jusi_acp.diagnostics` using the same Python environment as Jusi.
It prints package versions and diagnostic messages, including the method and
parameters of a failed notification when captured. Check that output before
sharing it: agent-supplied notification parameters may contain private data.
