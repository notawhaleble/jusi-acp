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
- ACP filesystem and terminal-auth capabilities are not advertised.
- Form and URL elicitation are supported using the published v1 SDK. The SDK
  classifies these routes as unstable; this does not enable ACP v2 or require
  a Git dependency.
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
  turn's grouped event view. Completion selects that turn and opens its last
  assistant message as a wrapped VisiData text sheet. Close it with `q` to return
  to the selected turn. Notebook focus stays where it is; a new prompt replaces
  the automatic preview. Pending questions take priority.

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

Usage is presented by the ACP family whenever the agent reports it. The turns
sheet shows `total_tokens`, `input_tokens`, `output_tokens`, `thought_tokens`,
`cached_read_tokens`, and `cached_write_tokens` from the final prompt response,
plus the latest `context_used`, `context_size`, cumulative `session_cost`, and
`currency` from ACP `usage_update` notifications. Zero is displayed as zero;
unreported values stay blank, and a new turn does not inherit old usage. These
snapshots survive presentation-cache reloads. Enter on a turn opens its events;
Enter on a usage or stop event exposes the original payload and metadata.
Usage columns follow the existing turn review columns; scroll horizontally to
see the full breakdown.

Context usage is the number of tokens currently in the context window, not
tokens consumed by a turn. Cost is cumulative for the session. Final response
token usage is experimental and is displayed exactly as reported: ACP's
[usage scope ambiguity](https://github.com/agentclientprotocol/agent-client-protocol/issues/1860)
means agents may report per-turn or cumulative token counts. The family does not
sum them or infer deltas. Provider-specific usage hidden in extension metadata
needs an explicit provider mapping; arbitrary metadata is preserved without
guessing its meaning.

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
next follow-up starts a new prompt unless a session request is still pending.
Requests can arrive after prompt completion and remain answerable until resolved
or cancelled; prompt completion alone does not cancel them.

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


## Session activity on ACP v1

The default sheet remains prompt-centric: one row per user prompt, with Enter
for its grouped events and Ctrl-O on a cell for the usual Vim representation.
Known tool, terminal, and message IDs route late updates to their original group,
even while another prompt runs. The `updates` column counts late updates; opening
the turn clears its indicator. A message ID is only correlated after its first
appearance; its value is never interpreted as a turn identifier.

Events arriving idle without a known owner appear together in an **Unassigned
session activity** row between prompts. Enter opens that group. During a prompt,
otherwise uncorrelated events are grouped by their arrival during that prompt;
this is presentation context, not proof of worker ownership. Raw updates remain
available in the journal. No worker identity is inferred from message wording
or arbitrary extension metadata.

From the turns, events, or reply views:

| Key | View |
| --- | --- |
| `p` | Current session plan |
| `P` on a turn | Plan revisions received in that turn |
| `t` | Client-owned terminal monitor; Enter opens live output |
| `w` | Pending requests in arrival order; Enter previews without retargeting answers |
| `a` | Return to the active full-screen question |

`/plan`, `/terminals`, and `/pending` also open these views from a follow-up when
no question is waiting. Closing a view does not stop work. Terminal output follows
the end while you are at the bottom; scrolling up keeps your position. Output
limits and truncation indicators come from the client terminal service. Commands
run internally by the agent do not appear here unless it uses ACP terminal calls.

Each standard plan update replaces the current plan in full, including an empty
plan. Historical snapshots stay with the turn or unassigned group where they
arrived. ACP v1 does not supply standard task/plan identities for independent
worker plans, so the client presents one current session plan.

## Pending requests and elicitation

Questions, approvals, and elicitations share one FIFO queue. A request containing
several questions keeps its place until those questions are answered. The main
view shows the full current question; its title shows the source and the number
of other queued requests. Later arrivals do not replace it or reset scrolling.

Elicitation is an agent request for user-provided information. Form elicitation
uses the same follow-up flow for free text. Short structured fields have an
editable value cell: `e` edits it and Enter submits it. They can also be answered
from the notebook. Booleans use `true`/`false`, numbers use JSON numbers, and
multi-select fields use a JSON array of strings. Invalid answers leave the same
field active. Optional fields support `/skip`, supplied defaults support
`/default`, and `/empty` supplies an empty string. `/decline` declines an
elicitation; `/dismiss` cancels only that elicitation. URL elicitation displays
the URL for you to open; Enter or `/accept` acknowledges it without claiming that
external work is complete. Agent completion notifications are recorded separately.

If the active request is withdrawn, further answer text is rejected rather than
sent to the next request. Submit `/next` to acknowledge the withdrawal and show
the next question. `/cancel` still cancels the session's pending requests and
active prompt. There are no additional Vim answer buffers.

## Project path completion

In a live ACP cell, type `@` and invoke normal Jusi completion (Tab). For example,
`Explain @application` offers matching paths across the configured project.
`@src/` narrows to that directory. Selecting a candidate inserts a plain relative
path such as `./src/jusi_acp/application.py`: **the `@` is removed and no file is
attached or read into the prompt**. Paths beginning `./` or `../` continue to
complete, and spaces are backslash-escaped. Other prompt text, including text
after the cursor, is preserved. Completion also works while answering questions.

The target-side session working directory and configured additional directories
are searched. Git projects respect Git ignore rules for untracked files; tracked
files remain available. Outside Git, common generated directories are excluded.
Results are bounded and cached briefly so completion does not scan on every key.

Jusi currently requires a live client before completing a plugin cell. To complete
your first prompt, execute an empty `%%acp ALIAS` cell first, then write the prompt
as a follow-up. No agent prompt is sent by that empty initialization.

See [the manual testing guide](docs/manual-testing.md) for prompts and expected
behavior, including features that depend on what the agent actually reports.
