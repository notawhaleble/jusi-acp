# Manual testing: ACP v2

Use a scratch project as the ACP alias path. Replace `work` below with your alias.
Start the first prompt in a `%%acp work` cell; submit subsequent prompts and
answers through the usual Jusi follow-up action on that same client.

## Before starting

1. Install this checkout into the Python environment Jusi uses:
   `python -m pip install -e '.[v2]'`.
2. The exact provider must construct `AgentLaunch(..., protocol_version=2)` and
   its agent must actually support ACP v2. Installing the extra alone does not
   change a provider's launch configuration.
3. Fully restart Jusi. Start a session, then press `S` from Activity. Verify
   `protocolVersion` is `2`. If initialization fails or the version is `1`, stop
   here: this is not yet a v2 test.

The prompts below request agent behavior. They cannot force an agent to emit
specific ACP notifications. Record a feature as **not exercised** if the agent
does not expose it over ACP. A question written in ordinary assistant prose is
not structured elicitation, and a shell process started with `&` does not by
itself establish an ACP background worker.

## 1. Acceptance, streaming, and foreground completion

Prompt:

```text
Run a harmless Python command that prints "tick 1" through "tick 10", one line
per second, flushing each line immediately. Wait for the command to finish and
then summarize its output. Do not modify any files.
```

Expected:

- The Jusi follow-up operation can finish after the agent accepts the message,
  while the agent continues processing.
- Activity continues receiving output after that operation finishes.
- `S` shows the latest `state_update`: normally `running` during foreground
  work and `idle` after it ends. Close and reopen `S` to get a fresh snapshot.
- The user message is shown once; acceptance is a separate submission event.
- Activity stays open rather than switching to a turns summary.

Do not equate the notebook operation's completion with the agent's final answer.
An agent may buffer output, so the ticks are not a guarantee of visible streaming.

## 2. Terminal monitor

Use the same prompt. From Activity, press `T`, select the terminal, then Enter.

Expected if the agent emits terminal display updates:

- The terminal list shows its ID, command, working directory when reported,
  output, and reported exit status.
- The transcript updates while open. At the bottom it follows new output;
  scrolling back lets you inspect earlier output.
- There is no command input or process-control interface inside the monitor.

If output appears only in a tool call, inspect it with `W`. This means the agent
has not exercised the dedicated terminal display surface.

## 3. A plan that survives individual prompts

Prompt:

```text
Publish a three-step plan through your structured planning capability:
1. Inspect the scratch directory.
2. Create v2-demo.txt containing "first version".
3. Review the result.
Complete only step 1, leave the other steps pending, and report back.
```

Press `P` from Activity; Enter opens the plan's current entries. Then follow up:

```text
Continue the existing plan. Complete steps 2 and 3 and update that same plan.
```

Expected if the agent supplies a stable plan ID:

- The plan remains visible after the first foreground operation ends.
- The second operation updates the same plan, with completed entry statuses.
- Plan updates also remain discoverable in Activity.

If the agent generates a new plan ID, the client shows a second plan. Baseline
plan entries have no individual task IDs; the client does not infer identity
across arbitrary rewrites. Plan editing is outside this implementation.

## 4. Focused questions and form validation

Prompt:

```text
Use structured form elicitation to ask me for:
- a project description, as a string;
- a retry count, as an integer from 1 to 5;
- whether to enable verbose output, as a boolean.
Collect these in one request. Do not proceed until I submit the form.
Do not ask solely in ordinary chat prose.
```

Expected:

1. The entire VisiData window shows the current field's question and context.
2. Submit this description through Jusi follow-up:

   ```text
   A small demonstration project.
     Preserve this indentation and this second line.
   ```

3. The next field appears. Submit `0`: validation should reject it, retaining
   the same question. Submit `3`: it should advance.
4. Submit `true` for the boolean field.
5. A complete-response review appears. The form is still pending.
6. Press `e` to inspect/edit short field values in VisiData. Close that field
   sheet with `q` to return to review. Press Enter there to submit the form.

Long answers stay in notebook follow-ups. `D` declines elicitation; `x` cancels
it. An optional field, if provided by the agent, can be omitted with `s`.

Qwen-style question requests use a different response format: they advance after
each answer and deliver the answer set after the last question, without the
generic form-review stage. They are useful compatibility tests but do not prove
that `elicitation/create` was used.

## 5. Concurrent questions from background work

Prompt:

```text
If you support independent background workers, start two read-only workers.
Worker A should ask me a structured question about the desired output format.
Worker B should ask me a structured question about the desired level of detail.
Have both requests outstanding before either receives an answer. Explain if
your agent cannot expose concurrent requests this way.
```

Expected when both requests are actually outstanding:

- The first received question remains the full-window active question.
- The header reports another waiting request. The later arrival does not
  replace the body or reset the current question's scroll position.
- `Q` opens the pending-request overview in arrival order. Browsing that view
  does not change the answer target. Close it with `q` to return.
- Answer the first question through Jusi follow-up. If it is a form, review and
  submit it. The second request then becomes the focused question.
- You can answer the second request while work associated with the first
  continues. Source labels appear only when supplied/associated by the agent.

If the agent asks the questions serially, this exercises the familiar sequence
but not concurrent queueing. No universal worker tree is implemented.

## 6. Background updates while foreground work is idle

Prompt:

```text
If supported, start a background worker that waits about 15 seconds and then
reports "background check complete". Return control to me before it finishes,
keeping that worker attached to this session.
```

Expected if the agent exposes this behavior:

- Foreground work becomes idle while the background work continues.
- Its later message/tool/plan/terminal updates appear in this same session.
- You do not have to reopen a completed turn to see them.
- An idle state does not imply that every background operation has finished.

Optionally submit a new short prompt during the wait, after checking that no
question is pending. The agent determines whether it accepts input while busy;
v2 does not guarantee concurrent prompt acceptance.

## 7. Changes and cancellation

Prompt:

```text
Change v2-demo.txt to contain "second version" and report the change through
your structured diff capability.
```

Press `D` from Activity to inspect session changes, or `d` on the tool that
reported the diff. Expect the affected path and an agent-provided patch when
available. V2 patch display does not reconstruct complete before/after files.

Then prompt:

```text
Run the harmless ten-tick command again, this time for 30 seconds.
```

While it runs, submit `/cancel` or use `c` from Activity. Expect a cancellation
request and, when confirmed by the agent, an idle state with a cancelled stop
reason. Try a new prompt afterward to verify session reuse.

Use these session controls after prompt acceptance. `:JusiInterrupt` targets an
active Jusi operation; it does not target independent session work after that
operation has already completed. Individual background-worker cancellation is
not implemented, and session cancellation does not promise to terminate every
background process.

## 8. Replay and URL elicitation

For replay, finish/resolve current work, submit `/sessions`, and select the
session with Enter. Retained history is displayed in Activity. Alternatively,
copy the session ID from `S` and start a new cell:

```text
%%acp work --load SESSION_ID
Summarize the work already done.
```

`--load` requests replay. `--resume` reattaches without requesting it. The agent
determines what history it retains and replays; absent historical terminals or
plans are not by themselves proof of a client problem.

For URL elicitation, use a provider/MCP integration with a real browser
authorization flow. Ask it to connect the integration through structured URL
elicitation. Expect a focused request with the full URL and host, no browser
opening before consent, and Enter to open the browser. Acceptance and external
completion are separate. `D` and `x` allow declining/cancelling. A normal chat
link does not exercise this feature.

## Recording results

For each scenario, record **passed**, **failed**, or **not exercised by agent**.
For a failure, include the prompt, protocol version, visible state, whether the
Jusi operation had completed, and the unexpected behavior. Use
`python -m jusi_acp.diagnostics` for recent errors in the Jusi target environment.

Some wire behaviors cannot reliably be requested in natural language, including
explicit null clears, terminal snapshot replacement, request withdrawal, and
the ordering of acceptance versus message notifications. The deterministic
coverage for these is in `tests/test_v2.py`:

```sh
.venv/bin/pytest -q tests/test_v2.py
```

The fixture's `questions` trigger deliberately produces two concurrent requests;
its other prompts produce late updates after idle. These are fixture controls,
not commands a real agent is expected to recognize.
