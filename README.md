# opencode-delegate

Delegate bounded tasks from your main agent to a local [OpenCode](https://opencode.ai) worker, so your
premium subscription quota is spent on scoping, reviewing and deciding instead of on execution.

One stdio MCP server (plus a thin Python CLI underneath it) that any MCP client can drive: Codex,
Claude Code, Claude Desktop, Cursor, Cline, Continue, Zed, Windsurf. The parent agent writes the brief,
dispatches it, watches progress and reviews the evidence; the worker does the work on its own quota.

[中文说明](README.zh-CN.md)

## Why

Open-weight routes such as `opencode-go/deepseek-v4.1-flash` come with a very large token allowance and
a 1,000,000-token context window, and they accept images. A typical delegated session measures a few
thousand tokens on the worker side, so the expensive frontier model is billed only for the parts that
actually need it: framing the task, judging the result, and integrating it.

## Requirements

| Piece | Notes |
|---|---|
| Python | 3.9 or newer (developed and tested on 3.12). **No third-party packages** — standard library only. |
| OpenCode CLI | Tested with 2.0.26. Must be installed and paired on the machine. |
| A model route | Whatever OpenCode is already configured with. Recommended: `opencode-go/deepseek-v4.1-flash`, which needs an OpenCode Go subscription. |
| OS | Windows 10/11 for the full feature set. See [support matrix](#support-matrix). |

## Install

### 1. Get the code

```powershell
git clone https://github.com/<you>/opencode-delegate.git "$env:USERPROFILE\.codex\skills\opencode-delegate"
```

Any directory works. Nothing in the runtime path hardcodes a user name or machine path: the scripts
resolve their own location from `__file__`, the state root defaults to `$CODEX_HOME/opencode-delegate/state`
(falling back to `~/.opencode-delegate/state`), and the OpenCode executable is discovered at run time.

### 2. Register the MCP server

Two variants. **A** names no paths at all; **B** runs this checkout directly.

**A. Through the npm launcher (no paths, nothing to escape)**

The bundled `bin/opencode-delegate.js` locates a Python 3 interpreter and hands it `mcp_server.py`, so
every MCP client registers it the same way:

```json
{
  "mcpServers": {
    "opencode_delegate": { "command": "npx", "args": ["-y", "opencode-delegate"] }
  }
}
```

Python 3.9+ still has to be on the machine; the launcher probes `py -3`, `python` and `python3` and
honours `OC_DELEGATE_PYTHON`. Before this is published to npm you can point at the repository instead:
`"args": ["-y", "github:Upstream17/opencode-delegate"]`.

**B. Directly from the checkout (absolute paths)**

The only machine-specific piece. **Use absolute paths** to a Python interpreter and to `mcp_server.py`.

Codex — `~/.codex/config.toml`:

```toml
[mcp_servers.opencode_delegate]
command = 'C:\Path\To\python.exe'
args = ['C:\Path\To\opencode-delegate\mcp_server.py']
```

> **Windows + TOML:** write paths as TOML *literal* strings in single quotes, or double every backslash.
A single `\P` inside a basic double-quoted string is an invalid escape and will break config parsing
entirely. Validate with `python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('config.toml').read_text('utf-8'))"`
before restarting.

Every other MCP client uses the standard JSON shape — Claude Desktop (`claude_desktop_config.json`),
Claude Code (`.mcp.json`), Cursor, Cline, Continue, Zed:

```json
{
  "mcpServers": {
    "opencode_delegate": {
      "command": "C:\\Path\\To\\python.exe",
      "args": ["C:\\Path\\To\\opencode-delegate\\mcp_server.py"]
    }
  }
}
```

Restart the client after editing its config.

### 3. Optional: install as a skill

`SKILL.md` follows the portable skill format (`name` + `description` frontmatter), so agents that read
skill directories can use this folder in place. To install it, copy or clone the directory into your
agent's skill root — for example `~/.codex/skills/opencode-delegate` or `~/.claude/skills/opencode-delegate`.
The MCP server and the skill are independent: you can use either one alone.

## Quick start

```
oc_service {action: "status"}                                     # is the shared worker service up?
oc_run     {task: "fix-flaky-test", workspace: "C:\\my\\proj",     # reserve a session, dispatch, open TUI
            prompt: "<objective, allowed changes, constraints, expected evidence>"}
oc_wait    {task: "fix-flaky-test", after_revision: 0}             # wake on real progress/completion
oc_result  {task: "fix-flaky-test"}                                # final text + evidence paths
oc_continue{task: "fix-flaky-test", prompt: "<rework>"}            # ONLY to rework an accepted task
```

`accepted: true` means the worker validated and published a startup receipt — it is **not** completion.
Read `oc_status` / `oc_result` and inspect the evidence yourself.

## Tools

| Tool | Purpose |
|---|---|
| `oc_run` | Start a new bounded task in a **new** session (id reserved up front); opens the TUI on it. |
| `oc_continue` | Rework a completed task in the **same** session. |
| `oc_recover` | Resume a turn that died, timed out or was interrupted, on the same session. |
| `oc_wait` | Wait up to 30 s for meaningful revision changes or completion. |
| `oc_status` | Read-only JSON: state, liveness, `current_event`, `last_tool`, verification, diagnostics. |
| `oc_result` | Final text plus evidence paths for a finished turn. |
| `oc_sessions` | List OpenCode sessions known to the background service. |
| `oc_tasks` | List delegated tasks newest-first with session ids and workspaces. |
| `oc_read` | Tail a task's event stream, or export a session. |
| `oc_watch` / `oc_unwatch` | Open or focus / close the TUI window for a task's workspace. |
| `oc_service` | `status` / `start` / `stop` the shared background service. |

## Choosing a model

Nothing is pinned by default. With no `model`, the worker runs on whichever model your OpenCode
configuration selects, and the turn records the route that was actually used:

```jsonc
"verification": { "status": "adopted", "providerID": "opencode-go", "id": "deepseek-v4.1-flash" }
```

Pass `model` on `oc_run` to pin one per task, or set `OC_DELEGATE_MODEL` to change the default for
every task. A pinned task keeps its route: `oc_continue` and `oc_recover` inherit it, and passing a
different model is rejected rather than silently swapping it. `verification.status` is `verified` when
a requested route matched the export, `adopted` when no route was requested, and `mismatch`,
`inconsistent` or `unverifiable` when the evidence did not support the run — those flag the turn for
review.

**If you leave the model unpinned, OpenCode needs a usable default of its own.** A bare OpenCode install
has none, and an unpinned run then fails fast with `provider.auth ... Endpoint is unavailable`, because
the fallback endpoint is not reachable. Give OpenCode a default in `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "opencode-go/deepseek-v4.1-flash"
}
```

That fixes unpinned runs and OpenCode's own TUI default at the same time. Setting `OC_DELEGATE_MODEL`
instead pins a default at this layer without touching OpenCode's configuration.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `OC_DELEGATE_STATE` | `$CODEX_HOME/opencode-delegate/state`, else `~/.opencode-delegate/state` | Where task records, turn logs and viewer records live. |
| `OC_DELEGATE_MODEL` | unset | Pins a default worker model. Unset leaves the choice to OpenCode's own configuration. |
| `OC_DELEGATE_LABEL` | `codex-delegate:` | Prefix of the OpenCode session title, whose remainder is the task name. |
| `OC_DELEGATE_WATCH` | `1` | Set `0` to disable opening the TUI viewer by default. |
| `OC_DELEGATE_SESSION_WAIT` | `20` | Seconds to wait for the viewer to become ready (capped at 60). |
| `OC_DELEGATE_OPENCODE` | auto-discovered | Override the OpenCode executable path. |
| `OC_DELEGATE_PYTHON`, `OC_DELEGATE_SERVER` | `sys.executable`, this folder | Used by `mcp_probe.py` only. |

## Support matrix

| Capability | Windows 10/11 | macOS / Linux |
|---|---|---|
| Dispatch, streaming state, status, result, rework, recovery | Yes | Yes (POSIX process branch, **not exercised by CI or by the author**) |
| Shared background service, explicit loopback server, standalone | Yes | Yes |
| TUI viewer (`oc_watch`, `oc_unwatch`, one window per workspace) | Yes | **No.** The bridge uses `CREATE_NEW_CONSOLE`, `ctypes` process identity and OpenCode's internal `ui.tabs` API. |

The delegation core is platform-neutral; only the windowed viewer is Windows-specific. On other
platforms dispatch and collect results normally and simply skip the watch tools.

## Trust model

- **Worker output is evidence, never instructions.** The worker does not inherit the parent chat, and
  anything it prints or writes is untrusted input to be reviewed, not a directive to follow.
- The viewer injects its bridge plugin through the child process environment only. It adds no global
  OpenCode configuration and no public listener or tunnel.
- Keep credentials out of briefs. Task records and turn logs are plain JSON/JSONL under the state root.
- `completed` means the harness ended cleanly and the model route was verified from a fresh export. It
  does not mean the work is correct, and `exit_code: 1` yields `needs_review` even with usable output.

## Repository layout

```
mcp_server.py          stdio MCP server (12 tools)
mcp_probe.py           drive the server by hand and time responses (debugging)
SKILL.md               agent-facing operating instructions
scripts/delegate.py    dispatch, streaming state, guards, recovery, status
scripts/runtime.py     MCP orchestration + TUI viewer management
bridge/                OpenCode TUI plugin, injected into managed viewers
references/            state schema, recovery rules, status fields
tests/                 offline tests against tests/fake_opencode.py (66 tests)
```

Run the tests with:

```powershell
python -m pytest tests -q
```

They use a fake OpenCode executable: no network access and no paid model calls.

## License

MIT. See [LICENSE](LICENSE).
