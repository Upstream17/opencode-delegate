# Operational reference

Extra detail for the `opencode-delegate` helper. Keep it local; it contains no credentials.

## Task state schema (`<state-dir>/<task>.json`)

- `task`, `workspace`, `model`, `backend` (`{"kind": "service"|"server"|"standalone", "server": url|null}`)
- `session_id` (authoritative; set only once for a new run and never overwritten), `turn`, `status`
  (`running` | `completed` | `needs_review`), `acceptance` (`pending` until the parent decides)
- Live fields, cleared at the start of each turn: `heartbeat_at` (wrapper alive), `last_event_at`
  (activity received), `silent_seconds` (age of `last_event_at`; the real progress signal, since
  `heartbeat_at` only proves the wrapper is alive), `observed_session_id` (diagnostic id seen in the
  stream), `current_event`, `last_tool`, `event_count`, `stderr_bytes`, `wrapper_pid`, `child_pid`,
  `elapsed_seconds`, `events_path`, `stderr_path`, `exit_code`, `text`, `error`, `verification`,
  `verified_model`, `needs_review`, `event_errors`, `parse_errors`, `interrupted`,
  `server_session_may_be_running`.
- `termination` (`completed` | `stalled` | `timeout`) is how the harness ended the worker process, and
  is independent of `status`: a worker that exits non-zero on its own is `termination: completed` with
  `status: needs_review`. `limits` (`timeout`, `stall_timeout`) records the budget in force for the
  turn, so a record explains its own outcome without the original command line.
- `turns`: compact summaries of finished turns (`status`, bounded last text, `error`,
  `verified_model`, `verification`, `exit_code`, `acceptance`, `interrupted`,
  `server_session_may_be_running`, evidence paths). Previous-turn evidence is retained here; live
  fields never carry a stale `verified_model`.

Atomic writes use a `.tmp` file plus replace, so a reader never sees a partial state file. Live
updates also refresh `updated_at`. Summaries are bounded and exclude tool payloads, arguments and
credentials. Only the last text event is kept as the final result (bounded); full logs stay on disk.

## Outcome rules

- `completed` requires: exit code 0, no `error` events, no unparseable stream lines, exactly one
  session id (matching the authoritative id on a continuation), non-empty final text, and a fresh
  `session export` whose routes all match the requested provider/model (and variant when one is
  requested). The actual 2.0.26 stream can end with `step_finish reason=tool-calls` plus a final
  text event and no `step_finish(stop)`; no stop event is required.
- `completed` means harness + model verification only, never that tests passed. `acceptance` stays
  `pending`.
- Any stall, explicit timeout, Ctrl+C, non-zero exit, error event, session mismatch/mix, unparseable
  line, missing session/text, or verification mismatch produces `needs_review` and preserves evidence.
  A process exit code alone is never treated as proof that tests passed.
- A turn cut by the harness records `termination` (`stalled` or `timeout`; `completed` when the worker
  ended on its own) and `silent_seconds`. A parent agent must distinguish these: `timeout` means an
  explicit `--timeout` cap was reached, `stalled` means the worker produced nothing and used no CPU.
  `limits` records the `timeout` / `stall_timeout` in force, so the same record explains why it ended.
  Silence alone is never a stall: the stall watchdog also requires the process tree to be CPU-idle,
  because long solver runs emit nothing for minutes (a `completed` turn in the reference workload
  showed a 304 s gap in its own event stream).
- A reader/callback failure is surfaced as a `needs_review` turn; the child tree is terminated and
  reaped on any post-spawn exception.
- Export verification retries only *structural* failures — `json.JSONDecodeError`, a non-object
  document, a malformed `messages` list — up to `MODEL_EXPORT_ATTEMPTS` (4) times with linear
  backoff. Route-level problems (incomplete/absent route) and non-zero exits are semantic: they are
  reported on the first attempt with their historical statuses, because a complete export does not
  gain a route by being fetched again. Every verification dict carries `export_attempts`.
  `OC_DELEGATE_EXPORT_RETRY_DELAY` overrides the 0.5 s backoff unit (tests set `0`). Verification
  requests `session export --sanitize`; sanitizing redacts transcript and file data but leaves
  `messages[].model` intact (measured: identical route, 3376 -> 1630 bytes), so a smaller payload
  cannot cost route evidence.

## Backends

- Default: shared background service (`opencode service status` prints its loopback URL), so the
  local Web UI sees the same sessions.
- `--server <url>`: explicit loopback `http(s)` root URL only. Paths, queries, fragments, embedded
  credentials and out-of-range ports are rejected, and only the URL string is stored. A recorded
  server URL is re-validated before every continuation.
- `--standalone`: private per-call server; mutually exclusive with `--server`.
- Legacy records without a `backend` field are treated as `standalone`; unknown backend kinds are
  errors. Backend changes are refused on `continue`/`recover`.

## Recovery and locks

- A `.lock` file contains `{pid, token, created_at}`. The holder only removes its own token; another
  owner's lock is never silently unlinked.
- The record is loaded and validated only while the exclusive lock is held, avoiding a check/use
  race. `run` no longer checks state before locking.
- `recover` clears a lock only when the owner is confirmed dead and no recorded child/wrapper is
  alive; an unverifiable legacy lock fails closed unless the record's processes are confirmed gone.
- `recover` requires `--acknowledge-inspected` plus, for the default shared service or `--server`
  backend, `--acknowledge-session-idle` (operator/parent confirms the server session is idle 鈥?killing
  this client does not stop it). A live residual child/wrapper process blocks recovery on ALL
  backends, including `--standalone`. It resumes the same session; no automatic retry exists.
- Error/stall/explicit-timeout/Ctrl+C on a service/server backend sets `server_session_may_be_running`. Inspect the
  session via the Web UI before retrying; the helper does not claim it stopped.

## MCP orchestration and viewer bridge (0.4.0)

- `mcp_server.py` is the concurrent stdio protocol shell. `scripts/runtime.py` orchestrates dispatch
  and viewers; execution rules and status formatting remain in `scripts/delegate.py`.
- All MCP inputs are validated before file/CLI operations. Task names follow the executor's rules.
  Per-task dispatch locks protect immutable prompt creation and startup receipt correlation; the
  executor's existing task lock remains authoritative. Receipt errors are returned as tool errors.
  Accepted tasks remain accepted if the cosmetic viewer fails.
- Detached workers use `stdin=DEVNULL`; they never inherit or consume the MCP stdio pipe.
  Windows atomic replacement retries transient reader/antivirus sharing conflicts with a bounded
  deadline. Temporary state filenames are unique to each write.
- `oc_status` shares `status_snapshot()` with CLI status. `revision` ignores heartbeat-only updates.
  `oc_wait` supports at most 30 s per call. Bounded threaded calls keep ping/discovery responsive.
- Managed viewers load `bridge/tui.ts` through `OPENCODE_CLI_CONFIG_CONTENT`. Bridge configuration,
  token, workspace and directory are inherited only by that viewer. No persistent config is changed.
- Under `<state>/bridge/<workspace-hash>/`, atomic `request.json` carries a unique request id, viewer
  token, workspace and session id. `reply.json` echoes the id/token and observed active tab. The plugin
  rejects workspace mismatches, synchronizes sessions, and uses `context.ui.tabs.focus()`.
  `ready.json` records plugin heartbeat/tab state for diagnosis. Public tool output omits the token.
- `<state>/watch/<workspace-hash>.json` stores viewer PID, OS creation time, executable path and token.
  Only an exact live identity may be terminated. A failed close preserves the registry. Workspace
  OS locks cover all viewer operations across MCP processes. Normal selection failures never reopen
  or close the existing viewer. An explicit `force` is required to recreate it.
- Each dispatch stores prompt/startup receipt in `<state>/tasks/<task>/<dispatch-id>/`, and wrapper
  stdout/stderr in `<state>/logs/<dispatch-id>.*.log`. Turn evidence paths retain their old shape.
- `oc_tasks` supports offset/limit pagination. `oc_read` bounds tail lines and output size; session
  export reports truncation. `oc_service stop` refuses recorded running service-backed tasks.
- Old task-keyed watcher records are not adopted using PID alone. Close pre-upgrade viewers once.
  Existing task/session state and evidence are compatible and need no migration.

## Release portability (0.4.1)

- A task that names no model resolves in this order: `OC_DELEGATE_MODEL`, then the default model in
  OpenCode's own configuration, then nothing. The configured default is read by running
  `opencode debug config` inside the task workspace, so a project `opencode.json` overrides the
  global config exactly as a real run would; the file is never written to. `oc_run` reports which
  layer supplied the route as `model_source` (`requested`, `OC_DELEGATE_MODEL`, `opencode-config`,
  `recorded`, or null). Any read failure means "unknown" and the task runs unpinned as before. The
  read is never cached, so editing the OpenCode default takes effect on the next dispatch instead of
  needing a server restart.
- Node probes Python >= 3.9 with a five-second timeout per candidate, including explicit overrides.
  Both probes and the stdio server use `windowsHide`; only the managed TUI opens a console.
- Non-Windows viewer requests fail before reading task state or spawning children. Headless dispatch
  is the default there. POSIX CLI children use new sessions/process groups; timeout cleanup kills
  their owned group. Detached descendants that create another session are outside this mechanism.
- Both pinned and adopted model verification reject incomplete provider/model identities and
  malformed message containers. No valid identity means `unverifiable`, never successful adoption.
- npm uses an exact file allowlist. `tests/verify_package.py` checks the real tarball and the extracted
  package's stdio handshake. Runtime dependencies remain Python's standard library only.
