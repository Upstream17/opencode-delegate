# opencode-delegate

Delegate bounded tasks from your main agent to a local [OpenCode](https://opencode.ai) worker, so your
premium subscription quota is spent on scoping, reviewing and deciding instead of on execution.

One stdio MCP server (plus a thin Python CLI underneath it) that any MCP client can drive: Codex,
Claude Code, Claude Desktop, Cursor, Cline, Continue, Zed, Windsurf. The parent agent writes the brief,
owns the design, dispatches execution, watches progress and reviews the evidence; the worker uses its own quota.

[中文说明](README.zh-CN.md)

## Why

Open-source model services often provide generous token allowances for execution work such as
reading files, writing code, organizing material, and running tests. The parent agent scopes the
task, reviews evidence, and integrates results, reserving premium quota for important decisions.
Actual allowance, pricing, and capability depend on your provider and are not guaranteed by this tool.

No model brand is preferred or required. The worker follows your OpenCode configuration by default,
and the parent can select a model per task.

## Delegation boundary

The parent agent owns the design and final judgment. By default the worker performs factual lookup,
implements a settled design, and runs tests; it may choose routine coding details. Architecture,
research assumptions and algorithm selection stay with the parent unless the user explicitly asks
for worker advice. An unresolved design question must be reported before dependent implementation.

Before dispatch, specify the deliverable, settled rules, inputs, allowed changes, acceptance evidence,
and budget/stop conditions. Split tasks that cannot fit the execution window. Do not drop a design
constraint to finish a component check, or equate passing tests with a successful design. See the
[skill's delegation contract](SKILL.md#delegation-contract-parent-designs-worker-executes) for the
readiness check and the worker instruction to include in every prompt.

## Requirements

| Piece | Notes |
|---|---|
| Python | 3.9 or newer (developed and tested on 3.12). **No third-party packages** — standard library only. |
| OpenCode CLI | Tested with 2.0.26. Must be installed and paired on the machine. |
| A model route | Whatever OpenCode is already configured with. No specific brand, subscription, or provider is required. |
| OS | Windows 10/11 for the full feature set. See [support matrix](#support-matrix). |

## Install

You need Node.js 16+, Python 3.9+, and an OpenCode installation that can already use a model.
The launcher discovers Python automatically; it does not install Python, purchase a subscription,
or edit your OpenCode configuration.

### Recommended: one npm install command

```bash
npm install -g --allow-git=all github:Upstream17/opencode-delegate
```

This installs from the public GitHub repository without a manual clone. The package is **not yet
published to the npm registry**, so `npm install -g opencode-delegate` is not available yet. That
short command becomes available after publication. GitHub-source installs may require Git.

`--allow-git=all` explicitly permits Git sources for this invocation because npm 12 disables them
by default; it does not change global npm settings. Older npm versions that do not recognize this
option can omit it.

### Add the server to your MCP client

Installing a tool and registering it with an agent are separate steps. For clients using
`mcpServers`, such as Claude Desktop and Claude Code, add:

```json
{
  "mcpServers": {
    "opencode_delegate": { "command": "opencode-delegate", "args": [] }
  }
}
```

For Codex, add to `~/.codex/config.toml`:

```toml
[mcp_servers.opencode_delegate]
command = 'opencode-delegate'
args = []
```

Restart the client. The default model comes from OpenCode's current configuration; you do not
need to choose it again in the MCP settings.

<details>
<summary>Skip the global install: let the client download and run with npx</summary>

Replace the command and arguments above with:

```json
{ "command": "npx", "args": ["--allow-git=all", "-y", "github:Upstream17/opencode-delegate"] }
```

This is a stdio server: waiting for MCP requests after launch is normal. Usually the MCP client
starts it, rather than a person keeping an interactive terminal open.
</details>

<details>
<summary>Other clients and Windows startup troubleshooting</summary>

Zed uses `context_servers` in its settings, with the same command and arguments. Configuration
locations differ by client; follow that client's MCP schema.

If a Windows client cannot find the global command, restart it to refresh PATH. If it cannot execute
npm's `.cmd` shim, run `npm root -g` to find the package location, then configure `node.exe` with
`<npm-global-directory>/opencode-delegate/bin/opencode-delegate.js` as its argument. This is a
compatibility fallback for certain clients, not a prerequisite for every installation.
</details>

<details>
<summary>Source install and optional skill (developers)</summary>

```bash
git clone https://github.com/Upstream17/opencode-delegate.git
```

Set your client's command to a Python interpreter and its argument to the absolute path of
`mcp_server.py` in the checkout. For Windows TOML paths, use single-quoted literal strings.

Agents that support skill directories can also place the checkout in their skill root. The MCP
server and skill can be used independently. State/logs live in the user directory; the installation
directory does not need to be writable. See environment variables below.
</details>

## Quick start

```
oc_service {action: "status"}                                     # is the shared worker service up?
oc_run     {task: "fix-flaky-test", workspace: "C:\\my\\proj",     # reserve a session, dispatch, open TUI
            prompt: "<objective, allowed changes, constraints, expected evidence>"}
oc_status  {task: "fix-flaky-test"}  # read the returned revision string
oc_wait    {task: "fix-flaky-test", after_revision: "<revision from oc_status>"}             # wake on real progress/completion
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

New-task precedence:

1. The parent's explicit `oc_run.model`.
2. The MCP environment's `OC_DELEGATE_MODEL`.
3. OpenCode's current default in the task workspace, read anew for every new task.
4. If none is available, omit the model argument and record the route OpenCode actually selects.

Reading configuration never edits it. An unsupported/failed config query falls back to letting
OpenCode choose. A resolved default is recorded as this task's fixed route; changing OpenCode's
default affects new tasks only. `oc_continue` / `oc_recover` reuse the recorded task route rather
than resolving a new default. Historical unpinned tasks remain unpinned.

The `oc_run` response's `model_source` identifies the source. Pinned routes are `verified` against
exported evidence; completely unpinned runs `adopt` a valid recorded route (`status: adopted`).
Missing, inconsistent, or mismatched routes require review. A provider/authentication error should
prompt checks of the actual route, credentials and access; a 403 does not prove the default is absent.
The tool never silently switches providers or changes OpenCode settings. `provider/model-name` is
only a format placeholder, not an installable model ID.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `OC_DELEGATE_STATE` | `$CODEX_HOME/opencode-delegate/state`, else `~/.opencode-delegate/state` | Where task records, turn logs and viewer records live. |
| `OC_DELEGATE_MODEL` | unset | Pins a default worker model. Unset falls back to the default model in OpenCode's own configuration (read through `opencode debug config`, never written); if that is unset too, OpenCode chooses. |
| `OC_DELEGATE_LABEL` | `codex-delegate:` | Prefix of the OpenCode session title, whose remainder is the task name. |
| `OC_DELEGATE_WATCH` | `1` on Windows, `0` elsewhere | Set `0` to disable the viewer. Non-Windows watch requests fail before spawning a process. |
| `OC_DELEGATE_SESSION_WAIT` | `20` | Seconds to wait for the viewer to become ready (capped at 60). |
| `OC_DELEGATE_OPENCODE` | auto-discovered | Override the OpenCode executable path. |
| `OC_DELEGATE_PYTHON` | auto-discovered | Python 3.9+ executable for the npm launcher; also used by the probe. |
| `OC_DELEGATE_SERVER` | this folder | Server script override for `mcp_probe.py` only. |

## Support matrix

| Capability | Windows 10/11 | macOS / Linux |
|---|---|---|
| Dispatch, streaming state, status, result, rework, recovery | Yes | Experimental (POSIX branches; real model/TUI integration not validated by the author) |
| Shared background service, explicit loopback server, standalone | Yes | Yes |
| TUI viewer (`oc_watch`, `oc_unwatch`, one window per workspace) | Yes | **No.** The bridge uses `CREATE_NEW_CONSOLE`, `ctypes` process identity and OpenCode's internal `ui.tabs` API. |

Windows 10/11 + OpenCode 2.0.26 is the validated preview target. macOS/Linux core execution is
experimental: the viewer is disabled by default, and `oc_watch` refuses to launch it. POSIX children
run in their own process groups so timeout cleanup can terminate descendants. The CI matrix tests
these branches; a configured real OpenCode provider is still required for end-to-end validation.

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
tests/                 offline tests against tests/fake_opencode.py (standard-library unittest suite)
```

Run the tests with:

```powershell
python -B -m unittest discover -s tests -v
npm test
```

They use a fake OpenCode executable: no network access and no paid model calls. Python tests use
only the standard library; `npm test` uses Node's built-in assertions. Python 3.9+ and Node 16+ are
separate prerequisites; installing OpenCode alone does not guarantee both are present.

Before publishing, build and check the actual package:

```powershell
npm pack --ignore-scripts
python -B tests/verify_package.py opencode-delegate-0.4.1.tgz
```

This checks the exact file allowlist and starts the extracted package through Node -> Python. CI
also plants a cache file before packing, so accidental bytecode inclusion is detected.
The workflow includes Windows/Linux/macOS with Python 3.9/3.12. Those jobs run after a push;
local Windows results alone do not establish full cross-platform compatibility.

## License

MIT. See [LICENSE](LICENSE).
