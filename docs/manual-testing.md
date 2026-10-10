# Testing the ACP v1 session experience

Install this checkout in the Python environment used by your Jusi backend, then
restart the notebook kernel/client. This uses published dependencies:

```sh
python -m pip install -e /path/to/jusi-acp
```

If that environment still has the experimental SDK from the earlier v2 branch,
replace it once:

```sh
python -m pip install --force-reinstall --no-deps 'agent-client-protocol==0.12.1'
```

Use your configured Qwen Code alias. Agent behavior varies: a natural-language
request cannot force an agent to emit a particular ACP notification or RPC.

## 1. Paths and reply preview

Execute an empty cell:

```text
%%acp YOUR_ALIAS
```

Now write `Explain @` in that cell and press Tab. Choose a project file. Expect a
path with the leading `@` preserved. Submit the completed prompt through
your normal follow-up action.

When the prompt finishes, the client automatically displays the final assistant
message in a wrapped text sheet. Focus stays in Vim's notebook. You can immediately
write another follow-up. Alternatively focus the client and press `q`: the turns
sheet appears with this prompt's row selected. Enter drills into its events;
Ctrl-O on a message cell opens the usual Vim representation.

Try another prompt. It should reuse the same client, and previews should not pile
up into a stack of old replies. A prompt without an assistant message simply
returns to its turn row.

If Qwen supplies a suggested follow-up after completion, press `q` to return to
turns. The suggestion must not add an Unassigned session activity row. Enter the
prompt's turn to find the `Suggested follow-up` event; the reply stays unchanged.

Submit `/model` through the notebook follow-up flow. Expect a VisiData picker of
the agent's advertised models, with `*` on the current model. Select another row
and press Enter: the picker closes with a model status message. The next actual
prompt records the selected model. `/model` itself must not create a turn.
Pressing `q` instead cancels selection. Long-form `/model ID ...` commands still
go to the agent, preserving its special flags and one-off prompt syntax.

## 2. Questions

Prompt:

> Before doing anything, use your question tool to ask me which area to inspect
> and what detail I want. Ask for these as two questions in one request.

Expect one full-screen question at a time, including its complete multiline body
and choices. Answer in your notebook and submit a follow-up. The second question
should replace the first. Long answers and indentation should survive unchanged.
Closing the question with `q` does not answer it; `a` from the turns view returns
to it. `w` shows the optional queue overview without changing the answer target.

If the agent can start concurrent workers that ask questions, ask two workers to
ask you for input. The later request should increase the queue count without
replacing the active question or resetting its scroll position. Finish all
questions in the first request before seeing the next request.

`/cancel` cancels outstanding requests and the active prompt. Start a new prompt
afterwards to check that the session remains usable.

## 3. Plans

Prompt:

> Create a three-step plan to inspect this project's architecture, inspect it,
> and update the plan as you complete each step. Do not modify files.

Use `p` in the client to inspect the latest reported session plan. You should see
current entry statuses. After completion, close the reply, select the turn, and
press `P` to inspect its plan revisions. A new plan replaces the old current plan;
it does not append a second set of tasks. If Qwen only describes a plan in prose,
there is no structured plan to display.

## 4. Terminals

Prompt:

> If you support the ACP client terminal service, use it to run a command that
> prints a line each second for five seconds, then exits successfully.

Press `t` to open the terminal monitor and Enter to open output. Expect output to
update and the status to end at `exit 0`. Closing the output view must not kill the
process. Scroll away from the bottom during a longer command to check that updates
do not move your reading position.

Qwen may execute shell commands internally. Those are visible as its reported tool
calls, but cannot be monitored through the client terminal service unless Qwen
actually uses that service.

The locally tested Qwen Code 0.25.0 build accepted a four-second foreground shell
loop with terminal capability advertised, but emitted only pending, in-progress,
and completed shell tool events. Output appeared in the completed event; there
were no `terminal/create` calls or intermediate output frames. For this build,
inspect the shell tool's event in its turn; `t` remains empty. Its vendor
`qwen/status/session/tasks` endpoint is available for background task metadata,
including output-file paths. Monitoring those tasks would require a separate
Qwen adapter and is not implemented by the client terminal service.

## 5. Background events

If your Qwen build supports background work, ask it to launch a worker, return an
initial response, and report the result later. Expect the initial response to stay
open. Known tool updates should modify the originating turn and increase its
`updates` count. Uncorrelated idle events should appear in a compact **Unassigned
session activity** row, not be added to the latest reply. Enter opens that row.

Start another prompt while the worker runs. Updates carrying the original tool ID
must still appear under the original turn. An idle question should open the usual
question view and remain answerable even though the original prompt has finished.

## 6. Elicitation

If your agent exposes elicitation, ask it to request a short integer field and a
long text answer through a form. The integer should allow a short VisiData cell
edit (`e`, then Enter to submit). The long answer should use the notebook follow-up.
An invalid number must not advance the form. `/decline` and `/dismiss` end only that
elicitation. URL requests display a link and require explicit acknowledgement.

Qwen's `rawInput.questions` extension and standard elicitation are separate
protocol mechanisms. Support for the first does not guarantee support for the
second. The client advertises both form and URL elicitation, but the released SDK
still labels elicitation routes as unstable v1 extensions.

## Deterministic developer checks

The test agent provides reproducible cases without requiring a particular Qwen
build. Run from this checkout:

```sh
.venv/bin/python -m pytest -q
```

The tests cover late known and unassigned events, replacement plans and replay,
FIFO questions, withdrawn-answer rejection, typed form validation, URL actions,
and published-SDK wire exchanges. With Neovim and a sibling `jusi` checkout (or
`JUSI_NVIM_ROOT`), they also exercise the actual notebook follow-up lane, native
path completion popup, form cell edit, automatic reply preview, plan view, terminal
monitor, and background question handling.

Attention checks require Jusi core 1.0.5 or later and the matching Neovim plugin.
An approval or question should add a badge without changing editor focus. Close
its view with `q`: the badge must remain. Reopen with `a`, then answer or cancel:
the badge must clear. Multi-step questions retain one attention ID.

Background completion notices come from a tracked tool still running when its
originating turn ends, or Qwen/GigaCode's explicit background-task completion
metadata. Two messages (`background_task_completed` and
`background_notification`) for the same task must produce one notice; a matching
tool completion must not add another. A normal response, foreground tool, or
session history replay must stay silent. During a background
question, `:JusiAttention!` must dismiss completion notices while preserving the
unanswered request. The real notebook fixture exercises these transitions.
