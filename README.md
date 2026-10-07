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

Enter on a session loads its history into session Activity. Later Jusi follow-ups
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

## Session presentation

The default view is session Activity. Messages, tools, plans, terminals, and
requests belong to the session, independently of foreground prompt completion.
Background updates remain visible while the agent is idle. Streaming content is
consolidated by entity ID; significant tool/terminal status and plan changes also
produce activity notices. The raw journal retains the original updates.

Activity commands:

| Key | View/action |
| --- | --- |
| Enter | Inspect the selected message, tool, plan, or terminal |
| P | Plans, with current entries |
| T | Agent-owned terminals and live read-only transcripts |
| W | Tool calls |
| Q | Inspect pending questions in arrival order |
| D | Session-wide agent-reported changes |
| S | Session state, metadata, configuration, usage |
| d | Diffs for the selected tool |
| c | Request cancellation of session work |

Views do not automatically return to a turns summary when foreground work ends.
Browsing the queue does not change the answer target. Tool and terminal views
refresh while open. Terminal output is rendered as sanitized text, never executed
as escape sequences in the hosting terminal. V2 terminals are display-only:
there are no input, resize, kill, or process-control actions. V1 retains its
client-owned, bounded subprocess execution methods.

ACP remains authoritative for conversation context. The local journal is a
read-only presentation cache, not agent memory. Historical v1 turn projections
remain available internally for compatibility; new session views do not depend
on them. Context usage and cumulative costs are session state, not inferred
per-prompt totals. Unknown content and update payloads remain in diagnostics/raw
history; media playback is not provided by the terminal UI.

## Questions and elicitation

Questions from the foreground agent and background work share one FIFO queue.
A request containing several questions keeps those questions together in its
original order. The active question occupies the entire VisiData window, with
its multiline body, choices, source where available, and pending-request count.
New arrivals do not replace it or reset its scroll position.

Write substantial answers in the notebook cell in Vim and submit the normal
Jusi follow-up. Each follow-up answers the displayed question. Text, indentation,
and multiline content are preserved. Option labels are available in completion.
While a question is active, slash-prefixed text is answer text except `/cancel`.
Closing or inspecting a view does not answer or cancel a request.

Qwen/GigaCode `rawInput.questions` requests retain their existing permission
extension response format. Their answers are collected and delivered together.
Independent pending requests can be answered without waiting for unrelated work.

ACP v2 elicitation supports:

- **Form:** fields are presented sequentially. Text uses normal follow-ups;
  numeric, boolean, and multi-select answers use JSON values. Invalid answers do
  not advance the queue. Optional fields can be omitted with `s`. After the fields
  are complete, review the entire response and press Enter to submit. `e` opens
  a VisiData field sheet for short edits; it does not create a Vim buffer.
- **URL:** the focused view shows the full URL and destination host. Enter opens
  the configured browser only after explicit consent. Accepting the URL means
  opening it, not completion of the external workflow. Completion notifications
  are tracked separately.

For elicitation, `D` declines the request and `x` cancels it. Form responses can
be edited before submission. Forms must not be used to collect credentials;
external authorization belongs in URL mode. No browser page or credential content
is read by this client.

If the active request is withdrawn, its view remains as a notice. Follow-up text
is rejected rather than redirected to another request. Enter acknowledges the
withdrawal and advances the queue. Once the queue is empty, ordinary follow-ups
are prompts again. Requests arriving while a user is composing a prompt cannot
be correlated with editor drafts by the current Jusi contract; check the focused
question view before submission.

## Protocol versions

The default remains the published v1 SDK (`agent-client-protocol 0.12.x`). ACP v2
is opt-in because both the protocol and Python SDK surface are experimental.
Install the pinned upstream SDK through the extra:

```sh
pip install 'jusi-acp[v2]'
# From this checkout:
pip install -e '.[dev,v2]'
```

The exact provider selects v2 in its launch declaration:

```python
return AgentLaunch(("my-agent", "--acp"), cwd, protocol_version=2)
```

The v2 extra pins upstream commit
`9d07d7871ef4b220b8507e15fc4b1560f0950a64`. That commit still identifies itself as
0.12.1; checking the package version alone cannot establish v2 availability.
The v2 adapter requires `acp.experimental.v2` and fails visibly if it is missing.
The configured version is initialized explicitly. There is no automatic downgrade
or agent restart; use the default `protocol_version=1` for v1 agents.

For v2, successful follow-up completion means **the message was accepted**, not
that foreground or background work finished. Messages reconcile with the
agent-generated ID. Foreground state and errors arrive independently through
`state_update`. The client does not fabricate prompt ownership for background
updates. In v1, the existing prompt-response completion behavior remains.

Use `/cancel` or `c` for session cancellation. `:JusiInterrupt` can interrupt an
active Jusi operation; after v2 acceptance there is no such operation to target.
This package does not extend Jusi core with application-owned work publication.

V2 supports message/tool upserts and explicit null clears, streamed tool content,
plans by ID, terminal byte chunks and replacement snapshots, permission subjects,
structured diff operations and patch display, configuration/command/metadata/usage
updates, and full replay through `session/resume`. `--load` maps to resume with
`replayFrom: {type: "start"}`; `--resume` requests no replay. Supplied stdio MCP
configuration gains its v2 type discriminator; deprecated SSE configuration is
rejected. Authentication through `/auth METHOD_ID` uses `auth/login` in v2.

Client terminal authentication, provider-specific worker trees, plan editing,
remote ACP transports, and other unstable extension surfaces are not advertised.
A worker's source is shown only when the agent supplies it. Plan entry identity is
not inferred across revisions; baseline plans replace their full entries list.
V2 diffs are shown as supplied patches rather than reconstructing absent complete
file contents; v1 text diffs retain native read-only Vim diff delivery.

## Development

For prompts, expected UI behavior, and provider-dependent scenarios, see the
[ACP v2 manual testing guide](docs/manual-testing-v2.md).

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,v2]'
.venv/bin/pytest
```

The real notebook integration test uses a sibling `jusi` frontend checkout or
`JUSI_NVIM_ROOT`, together with the installed Python backend. It edits cells
through Neovim and verifies follow-up serialization, answer history, cancellation,
same-client reuse, and editor diff delivery. V2 tests include a separate JSON-RPC
subprocess fixture that accepts prompts before emitting background activity and
concurrent elicitation requests.

To collect recent target-side diagnostics:

```sh
python -m jusi_acp.diagnostics
```

Diagnostics may contain agent-supplied private content. Qwen's prompt-suggestion
notification remains advisory: it neither submits a prompt nor edits the notebook.
