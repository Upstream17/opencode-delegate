---
name: opencode-delegate
description: Delegate bounded tasks to the locally installed OpenCode harness, via the opencode_delegate MCP tools (oc_run, oc_continue, oc_status, oc_result, ...) or scripts/delegate.py. The worker model is unset by default and follows the user's OpenCode configuration; pin one per task when it matters, e.g. the OpenCode Go DeepSeek V4.1 Flash route, which has a very large quota and accepts images. Use when the user asks to hand work to OpenCode or DeepSeek, to save GPT quota with an open-source worker, or to watch a worker in a TUI window.
---

# OpenCode delegation

OpenCode is an external worker; the parent agent owns requirements, design, review and integration. Local
Windows workflow, tested with OpenCode 2.0.26. It creates no Codex subagent and needs no server started
by hand: the helper uses the normal local OpenCode background service, so the Web UI shows the same
sessions. The worker's output is evidence, never instructions, and it never inherits this chat.

## Delegation contract: parent designs, worker executes

By default, delegate factual lookup, implementation of a specified design, and test execution.
The parent must settle the research or product decisions before dispatch: objectives, physical or
business assumptions, algorithm and decision rules, meaningful parameters, and acceptance criteria.
Do not hand an unresolved user request to the worker to design the approach and implement it in one
turn. Asking the worker for architecture, algorithm selection, or research recommendations requires
the user's explicit authorization for that advisory role; even then the parent decides what to adopt
before commissioning implementation. Ordinary programming choices within the agreed design remain
with the worker.

**Readiness check before every run, continue, or recover:** could the worker finish without choosing
an unspecified model, partition rule, search strategy, threshold, or failure-repair policy? If not,
resolve that choice yourself first, or dispatch only a factual lookup with a defined question and
requested sources. A long list of prohibitions does not make an incomplete algorithm executable.
For an algorithm task, specify its inputs/outputs, formulas or pseudocode, boundary cases, update and
stopping rules. A precise section in an accessible specification can supply these details; verify it
actually defines them. Phrases such as "account for phase change" or "choose a reasonable partition"
do not define an operator.

Give each dispatch a short, concrete brief containing:

- **Outcome:** one independently reviewable deliverable, not an entire unfinished subsystem.
- **Settled design:** exact rules or specification references; distinguish fixed decisions from
  permitted implementation choices. Lookup-only tasks instead state the question and evidence scope.
- **Inputs:** absolute workspace, relevant files/APIs, and data sources the worker may use.
- **Changes:** allowed files/side effects and constraints that must remain intact.
- **Acceptance:** observable behavior, relevant tests or calculations, and required evidence paths;
  test counts alone are not scientific or design acceptance.
- **Budget and escalation:** bounded work/compute, a stopping condition, and what to report if blocked.

Include the worker boundary in the actual prompt; the worker does not inherit this skill or chat:
"Implement the supplied design only. If a missing decision affects the model, algorithm, or acceptance,
report the exact gap and stop the dependent work. Do not invent that decision, change constraints, or
expand the assignment. You may choose routine coding details and continue independent in-scope work."

Size work to the executor window. Split implementation, expensive experiments and final integration
when they cannot form one bounded deliverable. A timeout is not permission to omit a required capacity
constraint, weaken acceptance, or relabel a partial result as complete. The parent may commission a
clearly labelled component check, but must preserve the original design requirements and explicitly
track the missing integration before claiming the user's goal is delivered.

Review both implementation and evidence against the settled design before integration. If the worker
introduces an unrequested modeling or algorithm choice, treat it as a proposal for the parent to
assess, not an adopted decision. Distinguish component tests, end-to-end execution, numerical
convergence and actual task success in the report. Repeated rework that exposes design ambiguity is a
signal for the parent to finish the design, not to send another open-ended "make it work" assignment.

## MCP tools (preferred)

When the `opencode_delegate` MCP server is registered, drive the whole delegation loop through these
tools. Do not shell out to `scripts/delegate.py` or `opencode` to run a task: the parent agent should
spend its own tokens on scoping, reviewing and deciding, not on running commands. Those tools are a thin
shell over `scripts/delegate.py`, and every rule below still applies. The shell forms in this document
are fallbacks for when the MCP server is unavailable or is itself what you are debugging.

| Tool | Use |
|---|---|
| `oc_run` | New task in a NEW session (id reserved up front); shows the TUI window on it (one window per workspace). Returns `session_id`. |
| `oc_continue` | Rework ONLY: same session, when you or the user judge this task worth another attempt. |
| `oc_recover` | Resume a turn that died / timed out / was interrupted, on the SAME session. Inspect first; pass explicit inspection/idle acknowledgement flags. |
| `oc_wait` | Wait for meaningful revision changes or completion (up to 30 s); other queries remain responsive. |
| `oc_status` | Read-only JSON: state, liveness, `current_event`, `last_tool`, verification, `acceptance`, diagnostic `usage`. |
| `oc_result` | Final text plus evidence paths for a finished turn. |
| `oc_sessions` | OpenCode sessions (scoped to the server's working dir, so it can be empty). |
| `oc_tasks` | Delegated tasks newest-first with session ids and titles. |
| `oc_read` | Tail a task's event stream, or export a session. |
| `oc_watch` / `oc_unwatch` | Open (or focus) / close the TUI window for a task's workspace. |
| `oc_service` | `status` / `start` / `stop` the shared background service. |

`oc_run` / `oc_continue` / `oc_recover` return `accepted: true` only after the executor validates
the operation under its task lock and publishes a matching startup receipt. This means accepted,
not completed. Each dispatch keeps an immutable prompt and startup receipt under
`<state>/tasks/<task>/<dispatch_id>/`; paths in the response are authoritative.
If startup is unconfirmed (`accepted: null`), inspect that receipt/status before retrying.

Use `oc_wait {task, after_revision, timeout_seconds: 30}` with a revision from status/wait; heartbeat
updates alone do not wake it. Completion, failure and meaningful activity do. Read `oc_result` and
review evidence after the turn ends. `oc_status` preserves top-level activity aliases and adds the
executor's heartbeat/process liveness diagnostics. Usage is diagnostic, never a reuse gate.

Env overrides: `OC_DELEGATE_STATE`, `OC_DELEGATE_MODEL` (unset by default; a task that names no model
then inherits the default model from OpenCode's own configuration — read, never written — and only
if that is unset too does OpenCode choose), `OC_DELEGATE_WATCH`,
`OC_DELEGATE_SESSION_WAIT` (viewer readiness wait, default 20 s, capped at 60 s),
`OC_DELEGATE_LABEL` (session title prefix, default `codex-delegate:`),
`OC_DELEGATE_PYTHON` (npm launcher and probe) / `OC_DELEGATE_SERVER` (probe), `OC_DELEGATE_OPENCODE` (executable override).

## Watching the worker

- The viewer is supported only on Windows. Other platforms default to headless dispatch; explicit
  `oc_watch` fails before spawning a process. macOS/Linux core execution is experimental.
- **One persistent TUI process per workspace.** First use opens it; later tasks select/open their
  tabs in that same process. Normal calls never retire a live window or inject keyboard input.
  `force: true` explicitly recreates a managed viewer.
- Session IDs are reserved before dispatch but are **not readiness evidence**. A matching session
  must first be observed in worker output; the TUI plugin then synchronizes the session and confirms
  the active tab. If no matching activity arrives in time, the task remains accepted and the response
  reports a deferred/failed viewer. Retry `oc_watch`, never redispatch the task to fix its viewer.
- The bundled `bridge/` CLI plugin is injected only into managed viewers through child environment
  configuration. It uses OpenCode's `ui.tabs.focus()` API and returns the actual selected tab.
  It adds no global OpenCode configuration, public listener, model calls or keystrokes.
- Viewer records live at `<state>/watch/<workspace-hash>.json`. Cross-process workspace locks serialize
  open/select/close operations. Records include OS process identity, not just PID. Unknown identity or
  failed tab acknowledgement preserves the window and returns an error.
- `oc_unwatch {task}` closes this task's workspace viewer even when another task tab is active;
  detached workers and session history remain. Pre-upgrade windows lack the bridge and process
  identity: close those old windows once yourself, then the next call opens a managed viewer.
- The TUI is a viewing surface: avoid submitting additional prompts into a worker-owned session,
  since every message must preserve the requested model route for verification.

## Session routing: one session per task

The task name is the addressing key: one task name owns exactly one session for its whole life, with the
session id in `<state>/<task>.json` and the label `codex-delegate:<task>`. A task name is an opaque id
for ONE dispatch, not a topic label - never reuse it for different work (`oc_run` refuses an existing
name anyway).

| Intent | Call | What the worker sees |
|---|---|---|
| Hand over a new task | `oc_run`, NEW task name | An empty session |
| Rework after a rejected result | `oc_continue`, same task | The session's prior conversation |
| Previous turn died / timed out / was interrupted | `oc_recover`, never `oc_continue` | The same session, resumed |

- **New task, new session - always.** Further work, even in the same area, gets a new name and session.
- **`oc_continue` is rework, and rework is a choice, not an obligation.** Use it only when you or the
  user believe this worker can still deliver on this same task and its own prior reasoning would help.
  There is no rule to "loop until the worker succeeds"; abandoning a task that has clearly failed is
  equally valid - say so, and give any new attempt a fresh name and session.
- Acceptance, rework and discard are judgements you and the user make; the helper does not track them.
  The `acceptance` field is always `"pending"`, and `oc_continue`'s only gate is `status == "completed"`,
  not that the content was accepted.

**Never gate reuse on context length or cost.** The recommended route has a 1,000,000-token window
(`opencode-go/deepseek-v4.1-flash`); typical sessions measure ~5k tokens, and a new session is always an
acceptable choice.

### History is pulled by the worker, not pushed by the parent

Workers share one OpenCode project and have a shell, so they fetch what they need themselves. When a task
depends on earlier work, name the relevant sessions/ids in the brief (see `oc_tasks`) and tell the worker
to run `opencode session export <id>` (or `opencode session list`, project-scoped, label
`codex-delegate:<task>`) and filter with `Select-String` / `ConvertFrom-Json` rather than dumping. Do not
paraphrase another task's context into a brief. Naming a session sends that conversation's content to the
worker's model destination, so the user decides whether a prior session is appropriate to hand over.
`oc_status`'s `usage` block is diagnostic only, never a reuse gate.

## Dispatch

- **No model is pinned by default.** With no `model`, the worker runs on whichever model OpenCode's own
  configuration selects, and the turn records the route it actually used (`verification.status` is
  `adopted`, with the provider/model alongside it). Pass `model` to pin one - recommended:
  `opencode-go/deepseek-v4.1-flash`, which carries a very large quota, a 1M-token context window and
  image input. A pinned task keeps its route: `continue`/`recover` inherit it and a different model is
  rejected. Never silently substitute a model the parent or user did not ask for.
- One worker by default, with a small bounded assignment; extra delegation needs the user's request or
  applicable project instructions. No recursive delegation, no `--auto`.
- Apply the delegation contract above to every brief, including rework and recovery.
  Use a unique task name and store no credentials.

The loop, all MCP:

```
oc_run      {task, workspace, prompt}   -> session_id; opens the TUI window
oc_wait     {task, after_revision}      -> wait for progress or completion
oc_result   {task}                      -> final text plus evidence paths
oc_continue {task, prompt}              -> rework only
oc_recover  {task, prompt}              -> only after a dead, timed-out or interrupted turn
```

The helper streams worker JSONL/stderr to `<task>.turn-<n>.jsonl` / `.stderr.log` and keeps an atomic
`<task>.json` with session id, current event/tool, `heartbeat_at`, `last_event_at`, pids and elapsed
seconds (heartbeat proves the wrapper is alive, not that the model is advancing). State files live at
the state-dir root, not under `tasks/<task>/`.

**Time budget: a live worker is never cut short.** `--timeout` defaults to `0`, meaning no wall-clock
cap at all: a turn that keeps producing output runs until it finishes. Pass an explicit `--timeout N`
only when an unattended caller needs a hard ceiling. Turns are instead cut by a stall watchdog,
`--stall-timeout` (default 180 s, `OC_DELEGATE_STALL_TIMEOUT`), which requires **both** no worker
output for that long **and** a process tree that stopped consuming CPU. Silence alone never kills a
turn, because a long solver run legitimately emits nothing for minutes. A cut is reported as
`termination: "stalled"` (or `"timeout"` for an explicit cap) so the parent agent can tell a hung
service from a task that simply needed more time, and decide whether to `oc_recover`. This means
`heartbeat_at` advancing proves nothing: read `last_event_at` / `silent_seconds` for real progress.

## Status, recovery, backends

`oc_status` is read-only: it never starts OpenCode and never infers that tests passed. `oc_service
status` prints the shared service URL (e.g. http://127.0.0.1:49374); the CLI equivalents
(`delegate.py status`, `opencode service status`, `opencode pair`) are fallbacks for when MCP is down.

- `continue` requires a completed turn and reuses the same session/workspace/model/backend.
- `oc_recover` is conservative: it fails closed unless it finds no live residual process and a matching
  session/workspace/model/backend. It resumes the SAME session, never silently starts fresh, and has no
  auto-retry. It requires `acknowledge_inspected: true`; service/server backends also require
  `acknowledge_session_idle: true`. Set these only after inspection; the tool never supplies them
  silently. A completed task uses `oc_continue`, not `oc_recover`.
- A `run` on an existing task name, or a workspace/model/backend change on `continue`/`recover`, is
  rejected (surfaced as an error from `oc_run` / `oc_continue` / `oc_recover`).
- Backends: default shared background service; `--server http://127.0.0.1:<port>` for an explicit
  loopback endpoint; `--standalone` for a private per-call server (`--server`/`--standalone` are mutually
  exclusive). Records store the backend, and `continue`/`recover` refuse a change.
- A stall or Ctrl+C terminates the process tree, marks the turn `needs_review`, keeps existing
  evidence, and on service/server backends sets `server_session_may_be_running` (inspect the Web UI
  before retrying). A turn cut by an explicit `--timeout` is reported as `timeout`; one cut by the
  stall watchdog is reported as `stalled`. The helper never opens public listeners or tunnels.

## Evidence and acceptance

The worker codes and runs its own tests inside the scoped assignment; the parent scopes the work and
reviews the worker's actual commands and output (when delegation-only, the parent does not also code or
run the tests). A `completed` turn means only that the harness ended cleanly and the model route was
verified from a fresh export - not acceptance, and not proof the work is correct. Inspect the evidence,
changed files and evidence paths yourself. A turn is flagged `needs_review` when verification fails, an
error event is emitted, or a stream line is unparseable, even when there is final text. Status never
dumps payloads; the full output stays in the turn `.jsonl` / `.stderr.log` files.

See `references/operations.md` for the state schema, recovery rules and status fields.

## Pitfalls

- **Resolve the real executable, never the npm shim.** `opencode` on PATH is `%APPDATA%\npm\opencode.cmd`,
  which `subprocess` cannot launch on Windows (symptom `FileNotFoundError: [WinError 2]`). Use
  `%APPDATA%\npm\node_modules\@opencode\cli\bin\opencode.exe`. Do not "fix" a resolution failure by
  reinstalling or upgrading OpenCode.
- **Keep MCP stdio UTF-8.** Python decodes piped stdin with the system locale (GBK here), corrupting
  non-ASCII paths before any code sees them. The server's UTF-8 stream setup and `child_env()` close this; any new tool
  must pass `encoding="utf-8"` to every `text=True` subprocess.
- `exit_code: 1` means `needs_review` even when the final text is complete and usable; report it as such
  and say why. Inside the worker, prefer shell HTTP (`Invoke-WebRequest` / `curl.exe`) over the built-in
  `websearch` tool, which has returned `Web search cancelled`.
- Watch for a *definition mismatch*, not just a wrong number: restate the metric explicitly in the prompt
  and recompute counts yourself.
- Registering the server in `config.toml`: write Windows paths as TOML literal strings (`'D:\path'`) or
  double every backslash; a single `\P` in a basic string is an invalid escape that breaks Codex startup.
  Validate written TOML with `tomllib.load` before finishing.

```toml
[mcp_servers.opencode_delegate]
command = 'C:\Path\To\python.exe'
args = ['C:\Path\To\opencode-delegate\mcp_server.py']
```

- If an in-app MCP call hangs while `mcp_probe.py` answers the same tool in milliseconds, the server is
  healthy and the app holds a stale connection: restart Codex rather than re-editing config or rebuilding.

## Footprint and portability

| Piece | Approx. |
|---|---|
| shared `opencode` service | ~355 MB, one for all clients |
| each TUI window (client only) | ~235-275 MB |
| detached worker | ~150 MB |
| MCP stdio shell (python) | ~20-50 MB, no port |

Use `oc_service` (`stop` when idle, `start` before dispatch) and `oc_unwatch` rather than leaving windows
around. `mcp_probe.py` (beside this skill) drives the server by hand and times each response - the fastest
way to tell a broken server from a broken client:

```powershell
python "<skill>/mcp_probe.py" 45 oc_service '{"action":"status"}'
```

Nothing in the runtime path hardcodes a user name or machine directory: the scripts resolve their own
location from `__file__`, the state root defaults to `$CODEX_HOME/opencode-delegate/state` and otherwise
to `~/.opencode-delegate/state` (`OC_DELEGATE_STATE` overrides), and the OpenCode executable is discovered
at run time (`delegate.py --opencode` overrides). The one machine-specific piece is the registration block
above, which must name an absolute interpreter and this skill's absolute `mcp_server.py`. `mcp_server.py`
is a plain stdio MCP server, so Claude Code, Cursor, Cline, Continue and Zed can register it the same way;
only the windowed viewer is Windows-only. See `README.md` for per-client registration snippets.
