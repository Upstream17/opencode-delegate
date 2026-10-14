# Operational reference

Extra detail for the `opencode-delegate` helper. Keep it local; it contains no credentials.

## Task state schema (`<state-dir>/<task>.json`)

- `task`, `workspace`, `model`, `backend` (`{"kind": "service"|"server"|"standalone", "server": url|null}`)
- `session_id` (authoritative; set only once for a new run and never overwritten), `turn`, `status`
  (`running` | `completed` | `needs_review`), `acceptance` (`pending` until the parent decides)
- Live fields, cleared at the start of each turn: `heartbeat_at` (wrapper alive), `last_event_at`
  (activity received), `observed_session_id` (diagnostic id seen in the stream), `current_event`,
  `last_tool`, `event_count`, `stderr_bytes`, `wrapper_pid`, `child_pid`, `elapsed_seconds`,
  `events_path`, `stderr_path`, `exit_code`, `text`, `error`, `verification`, `verified_model`,
  `needs_review`, `event_errors`, `parse_errors`, `interrupted`, `server_session_may_be_running`.
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
- Any timeout, Ctrl+C, non-zero exit, error event, session mismatch/mix, unparseable line, missing
  session/text, or verification mismatch produces `needs_review` and preserves evidence. A process
  exit code alone is never treated as proof that tests passed.
- A reader/callback failure is surfaced as a `needs_review` turn; the child tree is terminated and
  reaped on any post-spawn exception.

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
  backend, `--acknowledge-session-idle` (operator/parent confirms the server session is idle — killing
  this client does not stop it). A live residual child/wrapper process blocks recovery on ALL
  backends, including `--standalone`. It resumes the same session; no automatic retry exists.
- Error/timeout/Ctrl+C on a service/server backend sets `server_session_may_be_running`. Inspect the
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
