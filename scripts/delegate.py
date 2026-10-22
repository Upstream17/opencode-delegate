"""Streaming OpenCode delegation with durable local task state and a read-only status action.

The helper runs the installed OpenCode CLI as an external worker, persists stdout JSONL and
stderr incrementally, keeps an atomic task state file (publishing an observed session id, activity,
heartbeat and PIDs while the worker runs), and verifies the recorded model route from a fresh
session export. A clean process exit plus fresh model verification is harness completion only;
parent acceptance is tracked separately and always starts as ``pending``.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

# Recommended route (very large quota, 1M-token context, accepts images). Never applied
# implicitly: an omitted --model means "run on whichever model OpenCode's own configuration
# selects", and the turn verification then adopts the route the session actually used.
RECOMMENDED_MODEL = "opencode-go/deepseek-v4.1-flash"
# Stable, greppable session label. Any session — including a later worker looking for context —
# can map a task name to its OpenCode session with `session list | grep <prefix><task>`.
# Dynamically resolves host: Antigravity -> "antigravity:", Codex -> "codex-delegate:",
# or overridden explicitly via OC_DELEGATE_LABEL.
def resolve_title_prefix() -> str:
    override = os.environ.get("OC_DELEGATE_LABEL")
    if override:
        return override
    for k in os.environ:
        if k.startswith("ANTIGRAVITY_") or k.startswith("AGY_"):
            return "antigravity:"
        if k.startswith("CODEX_"):
            return "codex-delegate:"
    try:
        import psutil
        curr = psutil.Process()
        for _ in range(6):
            curr = curr.parent()
            if not curr:
                break
            name = (curr.name() or "").lower()
            exe = (curr.exe() or "").lower()
            if "antigravity" in name or "antigravity" in exe or "language_server" in name:
                return "antigravity:"
            if "codex" in name or "codex" in exe:
                return "codex-delegate:"
    except Exception:
        pass
    return "codex-delegate:"


TITLE_PREFIX = resolve_title_prefix()
TASK_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
RESERVED_TASK = re.compile(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])")
HEARTBEAT_INTERVAL = 2.0
STATUS_SUMMARY_LIMIT = 160
FINAL_TEXT_LIMIT = 20000
DEFAULT_STALE_SECONDS = 60.0
# A worker that keeps producing output is alive and owns its own time budget: 0 means no
# wall-clock cap at all. Only an explicit --timeout (or OC_DELEGATE_TIMEOUT) may cut a turn,
# so a slow but productive long task is never killed for taking too long.
DEFAULT_TIMEOUT_SECONDS = 0.0
# Silence alone is not evidence of death: real turns emit nothing for minutes while a solver
# runs (gaps over 300s have been observed in successful turns). A stall therefore needs BOTH no
# output for this many seconds AND a process tree that stopped consuming CPU.
DEFAULT_STALL_SECONDS = 180.0
# OpenCode's shared service can briefly return a truncated session export just after a worker
# exits. Retry only structurally incomplete exports; never waive a semantic route mismatch.
MODEL_EXPORT_ATTEMPTS = 4
# Injectable so offline tests exercise the retry path without paying real backoff: a deterministic
# negative must not turn a millisecond unit test into a multi-second sleep.
try:
    MODEL_EXPORT_RETRY_DELAY = float(os.environ.get("OC_DELEGATE_EXPORT_RETRY_DELAY") or 0.5)
except ValueError:
    MODEL_EXPORT_RETRY_DELAY = 0.5
KNOWN_BACKENDS = ("service", "server", "standalone")
# The worker runs detached, so it has no console to inherit. Without this flag every console
# child (the OpenCode CLI, its short helper calls, taskkill) is handed a fresh visible console
# window by Windows, which flashes black windows at the user for the life of each turn.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def bound(value: object, limit: int = STATUS_SUMMARY_LIMIT) -> str:
    """Return a short, single-line string safe to publish in task state."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def age_seconds(timestamp: Optional[str]) -> Optional[float]:
    """Return the age of a timestamp in seconds, tolerating naive or malformed values."""
    if not timestamp:
        return None
    try:
        moment = datetime.datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return round((datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds(), 1)


def write_json(path: Path, value: object) -> None:
    """Atomically write JSON so readers never observe a partial state file."""
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        deadline = time.monotonic() + 2
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows readers/antivirus may briefly deny delete sharing on the old file.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
    finally:
        temporary.unlink(missing_ok=True)


def command_prefix(explicit: Optional[str]) -> list:
    """Resolve the OpenCode invocation prefix; a .py path runs under the current interpreter."""
    if explicit:
        candidate = Path(explicit).expanduser().resolve(strict=True)
        if candidate.suffix.lower() in (".ps1", ".cmd", ".bat"):
            raise ValueError("Pass the actual opencode executable, not a shell shim")
        if candidate.suffix.lower() == ".py":
            return [sys.executable, str(candidate)]
        return [str(candidate)]
    candidate = shutil.which("opencode.exe" if os.name == "nt" else "opencode")
    if candidate:
        return [candidate]
    if os.name == "nt":
        fallback = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@opencode/cli/bin/opencode.exe"
        if fallback.is_file():
            return [str(fallback)]
    raise ValueError("OpenCode executable not found; provide --opencode with its absolute path")


def pid_alive(pid: Optional[int]) -> bool:
    """Return True if the process id is alive. Never signals or terminates the process."""
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def validate_server(url: str) -> str:
    """Validate an explicit backend URL: root-path HTTP(S) loopback only, without credentials."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("--server URL is malformed") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError("--server must use http or https")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ValueError("--server must not embed credentials")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("--server must not include a path, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("--server port is out of range")
    host = parsed.hostname
    if not host:
        raise ValueError("--server must include a host")
    if host.lower() != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("--server must point at localhost or a loopback address") from exc
        if not address.is_loopback:
            raise ValueError("--server must point at localhost or a loopback address")
    return url.rstrip("/")


def validate_backend(backend: Optional[dict]) -> dict:
    """Validate a backend descriptor, including a server URL loaded from disk."""
    if not backend:
        return {"kind": "standalone", "server": None}
    kind = backend.get("kind")
    if kind not in KNOWN_BACKENDS:
        raise ValueError(f"Unknown recorded backend kind: {kind!r}")
    if kind == "server":
        server = backend.get("server")
        if not server:
            raise ValueError("Recorded server backend has no URL")
        return {"kind": "server", "server": validate_server(server)}
    return {"kind": kind, "server": None}


def describe_backend(backend: Optional[dict]) -> dict:
    """Normalise a possibly legacy backend descriptor for reporting."""
    if not backend:
        return {"kind": "standalone", "server": None}
    return {"kind": backend.get("kind", "standalone"), "server": backend.get("server")}


def backend_from_args(args: argparse.Namespace) -> dict:
    """Build the backend descriptor recorded in task state."""
    if args.standalone:
        return {"kind": "standalone", "server": None}
    if args.server:
        return {"kind": "server", "server": validate_server(args.server)}
    return {"kind": "service", "server": None}


def backend_flags(backend: Optional[dict]) -> list:
    """Return the CLI flags selecting a previously recorded backend."""
    kind = (backend or {}).get("kind", "standalone")
    if kind == "standalone":
        return ["--standalone"]
    if kind == "server":
        return ["--server", (backend or {}).get("server", "")]
    return []


class StateStore:
    """Serialise concurrent state updates to one atomic JSON file."""

    def __init__(self, path: Path, record: dict):
        self.path = path
        self._record = record
        self._lock = threading.Lock()

    def update(self, **fields: object) -> None:
        with self._lock:
            self._record.update(fields)
            self._record["updated_at"] = utc_now()
            write_json(self.path, self._record)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._record)


def kill_tree(process: subprocess.Popen) -> None:
    """Terminate a process tree, preferring taskkill on Windows."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        else:
            if getattr(process, '_delegate_process_group', False):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def reap(process: subprocess.Popen) -> None:
    """Reap a child process without blocking indefinitely."""
    try:
        process.wait(timeout=30)
    except Exception:
        pass


def join_thread(thread: threading.Thread, timeout: float = 15) -> None:
    """Join a started thread with a bounded wait."""
    if thread.ident is not None:
        thread.join(timeout)


def _windows_tree_cpu_seconds(pid: int) -> Optional[float]:
    """Sum CPU seconds across one Windows process tree, or None when it cannot be measured."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return None
        parents: dict = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        tree = {pid}
        frontier = [pid]
        while frontier:
            current = frontier.pop()
            for child, parent in parents.items():
                if parent == current and child not in tree:
                    tree.add(child)
                    frontier.append(child)
        total = 0.0
        measured = False
        for member in tree:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, member)
            if not handle:
                continue
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
                    for value in (kernel, user):
                        total += (value.dwHighDateTime << 32 | value.dwLowDateTime) / 1e7
                    measured = True
            finally:
                kernel32.CloseHandle(handle)
        return total if measured else None
    except Exception:
        return None


def _posix_tree_cpu_seconds(pid: int) -> Optional[float]:
    """Sum CPU seconds across one Linux process tree, or None when /proc is unavailable."""
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
        names = os.listdir("/proc")
    except (AttributeError, ValueError, OSError):
        return None
    parents: dict = {}
    cputime: dict = {}
    for name in names:
        if not name.isdigit():
            continue
        try:
            fields = Path("/proc", name, "stat").read_text(encoding="utf-8", errors="replace")
            fields = fields.rsplit(")", 1)[1].split()
            parents[int(name)] = int(fields[1])
            cputime[int(name)] = (int(fields[11]) + int(fields[12])) / ticks
        except (OSError, IndexError, ValueError):
            continue
    tree = {pid}
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        for child, parent in parents.items():
            if parent == current and child not in tree:
                tree.add(child)
                frontier.append(child)
    measured = [member for member in tree if member in cputime]
    if not measured:
        return None
    return sum(cputime[member] for member in measured)


def _parse_ps_cputime(token: str) -> Optional[float]:
    """Convert a ps(1) cputime token (M:SS.hh or H:MM:SS.hh) to seconds."""
    try:
        total = 0.0
        for piece in token.split(":"):
            total = total * 60.0 + float(piece)
        return total
    except ValueError:
        return None


def _ps_tree_cpu_from_output(output: str, pid: int) -> Optional[float]:
    """Sum CPU seconds for one process tree from `ps -axo pid=,ppid=,cputime=` output."""
    parents: dict = {}
    cputime: dict = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parents[child] = parent
        value = _parse_ps_cputime(parts[2])
        if value is not None:
            cputime[child] = value
    tree = {pid}
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        for child, parent in parents.items():
            if parent == current and child not in tree:
                tree.add(child)
                frontier.append(child)
    measured = [member for member in tree if member in cputime]
    if not measured:
        return None
    return sum(cputime[member] for member in measured)


def _ps_tree_cpu_seconds(pid: int) -> Optional[float]:
    """Sum CPU seconds across a process tree using ps(1), for platforms without /proc."""
    try:
        completed = subprocess.run(["ps", "-axo", "pid=,ppid=,cputime="],
                                   capture_output=True, encoding="utf-8", errors="replace",
                                   timeout=15, creationflags=NO_WINDOW)
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return _ps_tree_cpu_from_output(completed.stdout or "", pid)


def tree_cpu_seconds(pid: int) -> Optional[float]:
    """Total CPU seconds used by a process tree, or None when this platform cannot report it."""
    if not pid or not pid_alive(pid):
        return None
    if os.name == "nt":
        return _windows_tree_cpu_seconds(pid)
    if sys.platform.startswith("linux"):
        return _posix_tree_cpu_seconds(pid)
    return _ps_tree_cpu_seconds(pid)


def _env_float(name: str, fallback: float) -> float:
    """Read a float from the environment, falling back on anything unparseable."""
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


class StreamRunner:
    """Run a child process while pumping stdout/stderr lines and heartbeats to callbacks.

    Any callback, reader or heartbeat failure is surfaced to the caller as a RuntimeError after
    the child tree is terminated and reaped, so a broken stream can never look successful.
    """

    def __init__(self, command: list, cwd: str, timeout: float,
                 on_start: Callable[[int], None],
                 on_stdout_line: Callable[[str], None],
                 on_stderr_line: Callable[[str], None],
                 on_tick: Callable[[float], None],
                 stall_timeout: float = DEFAULT_STALL_SECONDS,
                 cpu_probe: Optional[Callable[[int], Optional[float]]] = None):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.stall_timeout = stall_timeout
        self.cpu_probe = cpu_probe or tree_cpu_seconds
        self.last_output_at = time.monotonic()
        self.silent_seconds = 0.0
        self._cpu_baseline: Optional[float] = None
        self.on_start = on_start
        self.on_stdout_line = on_stdout_line
        self.on_stderr_line = on_stderr_line
        self.on_tick = on_tick
        self.child_pid: Optional[int] = None
        self.reader_errors: list = []

    def _pump(self, stream, callback: Callable[[str], None]) -> None:
        try:
            for line in iter(stream.readline, ""):
                # Any output, on either stream, proves the worker is still alive.
                self.last_output_at = time.monotonic()
                try:
                    callback(line)
                except Exception as exc:
                    self.reader_errors.append(bound(f"{type(exc).__name__}: {exc}"))
        except Exception as exc:
            self.reader_errors.append(bound(f"reader {type(exc).__name__}: {exc}"))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _cpu_advanced(self) -> bool:
        """True when the worker process tree consumed CPU since the previous sample.

        An unavailable probe fails open (reports progress), so a platform that cannot measure
        CPU can never have legitimate long work killed; `--timeout` remains the bound there.
        """
        sample = self.cpu_probe(self.child_pid) if self.child_pid else None
        if sample is None:
            return True
        previous = self._cpu_baseline
        self._cpu_baseline = sample
        if previous is None:
            return True
        return sample > previous + 0.05

    def _termination_reason(self, now: float, started: float) -> Optional[str]:
        """Decide whether this turn must be cut short, and why.

        A worker that is still producing output is never cut by the wall clock: `timeout` of 0
        (the default) means no cap at all. Silence only counts as a stall once the process tree
        has also stopped consuming CPU, because a long tool call emits nothing for minutes.
        """
        if self.timeout and now - started > self.timeout:
            return "timeout"
        if self.stall_timeout and now - self.last_output_at > self.stall_timeout:
            if self._cpu_advanced():
                # Alive but quiet: a long tool call. Grant another full stall window.
                self.last_output_at = now
                return None
            return "stalled"
        return None

    def run(self) -> tuple:
        process = subprocess.Popen(
            self.command, cwd=self.cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW,
            start_new_session=os.name != 'nt')
        process._delegate_process_group = os.name != 'nt'
        self.child_pid = process.pid
        out_thread = threading.Thread(target=self._pump, args=(process.stdout, self.on_stdout_line), daemon=True)
        err_thread = threading.Thread(target=self._pump, args=(process.stderr, self.on_stderr_line), daemon=True)
        termination = "completed"
        started = time.monotonic()

        def shutdown() -> None:
            kill_tree(process)
            reap(process)
            join_thread(out_thread)
            join_thread(err_thread)

        try:
            self.on_start(process.pid)
            out_thread.start()
            err_thread.start()
            # Baseline the CPU counter so the first stall probe has a real delta to compare.
            self._cpu_baseline = self.cpu_probe(process.pid)
            while True:
                try:
                    process.wait(timeout=HEARTBEAT_INTERVAL)
                    break
                except subprocess.TimeoutExpired:
                    if self.reader_errors:
                        kill_tree(process)
                        reap(process)
                        break
                    now = time.monotonic()
                    self.on_tick(now - started)
                    reason = self._termination_reason(now, started)
                    if reason:
                        termination = reason
                        self.silent_seconds = now - self.last_output_at
                        kill_tree(process)
                        reap(process)
                        break
            self.on_tick(time.monotonic() - started)
        except KeyboardInterrupt:
            shutdown()
            raise
        except BaseException:
            shutdown()
            raise
        join_thread(out_thread)
        join_thread(err_thread)
        if out_thread.is_alive() or err_thread.is_alive():
            kill_tree(process)
            reap(process)
            raise RuntimeError("worker stream reader did not stop; failing closed")
        if self.reader_errors:
            if process.poll() is None:
                kill_tree(process)
            reap(process)
            raise RuntimeError("worker stream failed: " + "; ".join(self.reader_errors[:5]))
        return process.returncode, termination


def extract_session_id(event: dict) -> Optional[str]:
    """Find a session id in a JSONL event without assuming a single schema."""
    candidates = [event.get("sessionID"), event.get("session_id")]
    part = event.get("part")
    if isinstance(part, dict):
        candidates.append(part.get("sessionID"))
    properties = event.get("properties")
    if isinstance(properties, dict):
        candidates.append(properties.get("sessionID"))
    for value in candidates:
        if isinstance(value, str) and value:
            return value
    return None


def summarize_event(event: dict) -> str:
    """Return a bounded, payload-free summary of an event for live state."""
    event_type = str(event.get("type", "?"))
    part = event.get("part")
    if isinstance(part, dict) and part.get("type") and part.get("type") != event_type:
        return bound(f"{event_type}:{part.get('type')}")
    return bound(event_type)


def summarize_tool(event: dict) -> Optional[str]:
    """Return a bounded tool name/status summary, never tool input or output."""
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    tool = part.get("tool") or event.get("tool")
    if not tool:
        return None
    if isinstance(tool, dict):
        name = tool.get("name") or tool.get("tool")
        status = tool.get("status")
    else:
        name = tool
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
    return bound(":".join(str(bit) for bit in (name, status) if bit))


def summarize_error(raw: object) -> dict:
    """Return a class/code-only error summary, never the nested payload."""
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        return {"name": bound(raw.get("name") or raw.get("type") or "error", 64),
                "code": bound(data.get("code") or raw.get("code") or "", 64) or None}
    return {"name": type(raw).__name__, "code": None}


def run_blocking(command: list, cwd: str, timeout: float) -> tuple:
    """Run a short command to completion, returning (code, stdout, stderr, timed_out)."""
    if os.name != 'nt':
        with subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding='utf-8', errors='replace', start_new_session=True) as process:
            process._delegate_process_group = True
            try:
                output, error = process.communicate(timeout=timeout)
                return process.returncode, output, error, False
            except subprocess.TimeoutExpired:
                kill_tree(process)
                output, error = process.communicate()
                return -1, output, error, True
            except BaseException:
                kill_tree(process)
                process.communicate()
                raise
    try:
        result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, encoding="utf-8", errors="replace",
                                timeout=timeout, creationflags=NO_WINDOW)
        return result.returncode, result.stdout, result.stderr, False
    except subprocess.TimeoutExpired as expired:
        return -1, expired.stdout or "", expired.stderr or "", True


def verify_model(prefix: list, backend: dict, session_id: str, workspace: str,
                 requested: Optional[str]) -> tuple:
    """Verify the recorded route with a fresh export; returns (verification, verified_model).

    With a requested route this asserts the export matches it. Without one it adopts the single
    route the session actually used, so a run on OpenCode's own default is still evidenced.
    """
    # The service can truncate a sanitized export while the session is still finalizing. Retry only
    # payloads that are structurally incomplete: unparseable JSON, a non-object, or a malformed
    # messages list. A route problem is semantic -- a complete export does not gain a route by being
    # fetched again -- so those fail fast with their original status instead of paying backoff on a
    # deterministic negative. --sanitize shrinks the payload (measured: 3376 -> 1630 bytes) without
    # touching messages[].model, which narrows the window without losing the route evidence.
    command = prefix + ["session", "export", "--sanitize"] + backend_flags(backend) + [session_id]
    data = None
    failure = None
    attempts = 0
    for attempts in range(1, MODEL_EXPORT_ATTEMPTS + 1):
        code, output, error, timed_out = run_blocking(command, workspace, 60)
        if timed_out:
            failure = ("error", "export timed out")
            break
        if code != 0:
            failure = ("error", bound(error[-400:]) or f"export exited with code {code}")
            break
        try:
            candidate = json.loads(output)
        except json.JSONDecodeError as exc:
            failure = ("error", f"export was not JSON: {exc}")
        else:
            if not isinstance(candidate, dict):
                failure = ("error", "export was not a JSON object")
            else:
                candidate_messages = candidate.get("messages")
                if (not isinstance(candidate_messages, list)
                        or any(not isinstance(message, dict) for message in candidate_messages)):
                    failure = ("unverifiable", "export messages were malformed")
                else:
                    data = candidate
                    break
        if attempts < MODEL_EXPORT_ATTEMPTS:
            time.sleep(MODEL_EXPORT_RETRY_DELAY * attempts)

    if data is None:
        status, message = failure or ("error", "model route export unavailable")
        suffix = "export attempt" if attempts == 1 else "export attempts"
        return {"status": status, "message": f"{message} after {attempts} {suffix}",
                "export_attempts": attempts}, None

    messages = data["messages"]
    routes = [message['model'] for message in messages if message.get('model') is not None]
    # Validate the shape before touching route.get(): an export may record a non-object route.
    if any(not isinstance(route, dict) or any(
            not isinstance(route.get(key), str) or not route[key].strip()
            for key in ('providerID', 'id')) for route in routes):
        return {"status": "unverifiable", "expected": requested,
                "message": "export recorded an incomplete model route",
                "export_attempts": attempts}, None
    sample = [{"providerID": route.get("providerID"), "id": route.get("id")} for route in routes[:3]]
    if requested:
        expected_provider, _, rest = requested.partition("/")
        expected_model, _, expected_variant = rest.partition("#")
        if not routes:
            # Historical status for a pinned run whose complete export records no route at all.
            return {"status": "mismatch", "expected": requested, "sample": sample,
                    "export_attempts": attempts}, None
        mismatch = any(
            route.get("id") != expected_model
            or route.get("providerID") != expected_provider
            or (expected_variant and route.get("variant") != expected_variant)
            for route in routes)
        if mismatch:
            return {"status": "mismatch", "expected": requested, "sample": sample,
                    "export_attempts": attempts}, None
        return {"status": "verified", "expected": requested, "routes": len(routes),
                "export_attempts": attempts}, routes[-1]
    # No model was requested: adopt the route OpenCode selected instead of asserting one.
    if not routes:
        return {"status": "unverifiable", "expected": None,
                "message": "export recorded no model route",
                "export_attempts": attempts}, None
    if len({(route.get("providerID"), route.get("id"), route.get("variant")) for route in routes}) > 1:
        return {"status": "inconsistent", "expected": None, "sample": sample,
                "export_attempts": attempts}, None
    adopted = routes[-1]
    return {"status": "adopted", "expected": None, "routes": len(routes),
            "providerID": adopted.get("providerID"), "id": adopted.get("id"),
            "variant": adopted.get("variant"), "export_attempts": attempts}, adopted


def validate_task(task: str) -> None:
    """Reject invalid or Windows-reserved task names."""
    if not TASK_PATTERN.fullmatch(task):
        raise ValueError("Task must contain 1-64 letters, digits, hyphens or underscores")
    if RESERVED_TASK.fullmatch(task):
        raise ValueError("Task name is reserved on Windows")


def read_lock(lock_path: Path) -> Optional[dict]:
    """Read a lock file; returns None for empty, legacy or corrupt content."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_lock(lock_path: Path) -> str:
    """Create an exclusive lock owned by this process; returns the ownership token."""
    token = secrets.token_hex(8)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "token": token, "created_at": utc_now()}, handle)
    return token


def release_lock(lock_path: Path, token: str) -> None:
    """Remove the lock only when this process still owns it (never another owner's lock)."""
    info = read_lock(lock_path)
    if info and info.get("token") == token:
        lock_path.unlink(missing_ok=True)


def acquire_lock(lock_path: Path, record: Optional[dict], allow_stale: bool) -> str:
    """Acquire an exclusive lock, optionally clearing a confirmed-dead stale lock.

    A live lock owner, a live recorded child/wrapper process, or an unverifiable legacy lock all
    fail closed. A stale lock is only removed after a compare-and-delete against the content read.
    """
    if lock_path.exists():
        info = read_lock(lock_path)
        owner = info.get("pid") if info else None
        owner_alive = pid_alive(owner)
        pids = [record.get("wrapper_pid"), record.get("child_pid")] if record else []
        pids_known = any(pid is not None for pid in pids)
        record_alive = any(pid_alive(pid) for pid in pids)
        owner_known_dead = owner is not None and not owner_alive
        # Adequate stale evidence: a lock record naming a dead owner, or a legacy lock whose
        # recorded wrapper/child are known and confirmed dead. Never clear on an unknown lock.
        stale_evidence = owner_known_dead or (info is None and pids_known)
        confirmed_no_processes = (not owner_alive) and (not record_alive) and stale_evidence
        if not (allow_stale and confirmed_no_processes):
            raise ValueError("Task has an active or unverified lock; inspect before recovery")
        current = read_lock(lock_path)
        if lock_path.exists() and current != info:
            raise ValueError("Lock changed while inspecting; retry")
        lock_path.unlink(missing_ok=True)
    try:
        return write_lock(lock_path)
    except FileExistsError as exc:
        raise ValueError("Task is already running or locked; wait or inspect its state") from exc


def summarize_turn(record: dict) -> dict:
    """Build a compact, retained summary of the finished turn."""
    return {
        "turn": record.get("turn"),
        "status": record.get("status"),
        "text": (record.get("text") or "")[:FINAL_TEXT_LIMIT],
        "error": record.get("error"),
        "verified_model": record.get("verified_model"),
        "verification": record.get("verification"),
        "exit_code": record.get("exit_code"),
        "needs_review": record.get("needs_review", False),
        "acceptance": record.get("acceptance", "pending"),
        "interrupted": record.get("interrupted", False),
        "server_session_may_be_running": record.get("server_session_may_be_running", False),
        "parse_errors": record.get("parse_errors") or [],
        "events_path": record.get("events_path"),
        "stderr_path": record.get("stderr_path"),
        "ended_at": record.get("ended_at"),
    }


def begin_turn(record: dict, turn: int) -> None:
    """Retain the previous turn's evidence, then clear every live per-turn field."""
    if record.get("turn", 0) >= 1 and any(record.get(field) for field in ("status", "text", "error")):
        record.setdefault("turns", []).append(summarize_turn(record))
    record.update(turn=turn, started_at=utc_now(), ended_at=None, updated_at=utc_now(),
                  error=None, text=None, exit_code=None, verified_model=None, verification=None,
                  needs_review=False, current_event=None, last_tool=None, heartbeat_at=utc_now(),
                  last_event_at=None, event_count=0, stderr_bytes=0, wrapper_pid=os.getpid(),
                  child_pid=None, events_path=None, stderr_path=None, elapsed_seconds=0.0,
                  observed_session_id=None, event_errors=None, parse_errors=[],
                  interrupted=False, server_session_may_be_running=False, acceptance="pending",
                  status="running")


def finalize(store: StateStore, status: str, **fields: object) -> dict:
    """Write a terminal turn state and return the resulting record."""
    fields.setdefault("updated_at", utc_now())
    fields.setdefault("ended_at", utc_now())
    fields.setdefault("needs_review", status != "completed")
    store.update(status=status, **fields)
    return store.snapshot()


def _report(store: StateStore, message: str, exit_code: Optional[int], backend: dict, **fields: object) -> int:
    """Mark a non-trusted terminal outcome, preserving evidence for parent review."""
    if backend.get("kind") in ("service", "server"):
        fields.setdefault("server_session_may_be_running", True)
    finalize(store, "needs_review", error=message, exit_code=exit_code, **fields)
    return 1


def execute_turn(record: dict, store: StateStore, args: argparse.Namespace, workspace: Path,
                 backend: dict, prefix: list, prompt: str) -> int:
    """Run one worker turn with streaming state, then verify or flag for review."""
    expected_session_id = record.get("session_id")
    events_path = Path(args.state_dir).resolve() / f"{args.task}.turn-{record['turn']}.jsonl"
    stderr_path = Path(args.state_dir).resolve() / f"{args.task}.turn-{record['turn']}.stderr.log"
    store.update(events_path=str(events_path), stderr_path=str(stderr_path))
    stats = {"count": 0, "last_text": None, "sessions": set(), "first_session": None,
             "errors": [], "parse_errors": []}

    events_file = events_path.open("w", encoding="utf-8", newline="")
    stderr_file = stderr_path.open("w", encoding="utf-8", newline="")

    def on_stdout(line: str) -> None:
        events_file.write(line)          # IO failure here propagates as a reader error
        events_file.flush()
        stripped = line.strip()
        if not stripped:
            return
        stats["count"] += 1
        line_number = stats["count"]
        fields = {"event_count": line_number, "last_event_at": utc_now()}
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            stats["parse_errors"].append({"line": line_number, "error": "json-decode"})
            store.update(current_event="unparsed", **fields)
            return
        if not isinstance(event, dict):
            stats["parse_errors"].append({"line": line_number, "error": "non-object"})
            store.update(current_event="non-object", **fields)
            return
        session_id = extract_session_id(event)
        if session_id:
            if stats["first_session"] is None:
                stats["first_session"] = session_id
            stats["sessions"].add(session_id)
            fields["observed_session_id"] = session_id
            if expected_session_id is None and not record.get("session_id"):
                record["session_id"] = session_id     # adopt the first id early for a new run
                fields["session_id"] = session_id
        fields["current_event"] = summarize_event(event)
        tool = summarize_tool(event)
        if tool:
            fields["last_tool"] = tool
        event_type = event.get("type")
        if event_type == "text":
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            stats["last_text"] = str(part.get("text", ""))
        elif event_type == "error":
            stats["errors"].append(summarize_error(event.get("error", event)))
        store.update(**fields)

    def on_stderr(line: str) -> None:
        stderr_file.write(line)
        stderr_file.flush()
        record["stderr_bytes"] = record.get("stderr_bytes", 0) + len(line)
        store.update(stderr_bytes=record["stderr_bytes"])

    def on_start(pid: int) -> None:
        store.update(child_pid=pid)

    def on_tick(elapsed: float) -> None:
        silent = age_seconds(record.get("last_event_at"))
        store.update(heartbeat_at=utc_now(), elapsed_seconds=round(elapsed, 1),
                     silent_seconds=round(silent or 0.0, 1))

    command = prefix + ["run"] + backend_flags(backend) + ["--format", "json"]
    if args.model:
        command += ["--model", args.model]
    if expected_session_id:
        command += ["--session", expected_session_id]
    if args.action == "run":
        # Labelled even when the id is reserved, so the session is findable by task name. OpenCode
        # accepts --title together with --session (verified). Continuations skip it so a manual
        # rename in the TUI is not clobbered.
        command += ["--title", TITLE_PREFIX + args.task]
    command.append(prompt)
    runner = StreamRunner(command, str(workspace), args.timeout, on_start, on_stdout, on_stderr,
                          on_tick, stall_timeout=args.stall_timeout)
    try:
        exit_code, termination = runner.run()
    finally:
        events_file.flush()
        stderr_file.flush()
        events_file.close()
        stderr_file.close()

    observed = stats["sessions"]
    store.update(exit_code=exit_code,
                 text=(stats["last_text"] or "")[:FINAL_TEXT_LIMIT],
                 parse_errors=stats["parse_errors"],
                 observed_session_id=stats["first_session"],
                 termination=termination,
                 silent_seconds=round(runner.silent_seconds, 1))

    if termination == "timeout":
        return _report(store, f"timeout: exceeded explicit --timeout {args.timeout:g}s",
                       exit_code, backend)
    if termination == "stalled":
        snapshot = store.snapshot()
        return _report(
            store,
            f"stalled: no worker output for {runner.silent_seconds:.0f}s and no CPU activity "
            f"(last event {snapshot.get('current_event') or 'none'}, "
            f"last tool {snapshot.get('last_tool') or 'none'})",
            exit_code, backend)
    if stats["parse_errors"]:
        return _report(store, "worker stream contained unparseable lines", exit_code, backend,
                       parse_errors=stats["parse_errors"])
    if exit_code != 0:
        return _report(store, f"worker exited with code {exit_code}", exit_code, backend)
    if stats["errors"]:
        return _report(store, "worker reported an error event", exit_code, backend, event_errors=stats["errors"])
    if expected_session_id is not None:
        if observed != {expected_session_id}:
            return _report(store, "session mismatch: worker returned an unexpected session id",
                           exit_code, backend, observed_session_id=stats["first_session"])
    elif record.get("session_id") is None:
        return _report(store, "worker did not report a session id", exit_code, backend)
    elif observed != {record["session_id"]}:
        return _report(store, "worker reported multiple session ids", exit_code, backend,
                       observed_session_id=sorted(observed)[:3])
    if not stats["last_text"]:
        return _report(store, "worker produced no final text", exit_code, backend)
    verification, verified_model = verify_model(prefix, backend, record["session_id"], str(workspace), args.model)
    if verification["status"] not in ("verified", "adopted"):
        return _report(store, f"model verification {verification['status']}", exit_code, backend,
                       verification=verification)
    finalize(store, "completed", verification=verification, verified_model=verified_model,
             error=None, needs_review=False, acceptance="pending")
    return 0


def load_record(state_path: Path) -> dict:
    """Load a task state record from disk."""
    return json.loads(state_path.read_text(encoding="utf-8"))


def _validate_continuation(record: dict, args: argparse.Namespace, workspace: Path, backend: dict) -> None:
    """Validate a loaded record for continue/recover while the exclusive lock is held."""
    existing_backend = validate_backend(record.get("backend"))
    if record.get("workspace") != str(workspace) or record.get("model") != args.model:
        raise ValueError("Continuation requires the original workspace and model")
    if existing_backend != backend:
        raise ValueError(
            "Backend mismatch: record uses "
            f"{existing_backend['kind']}; pass matching --standalone/--server or omit flags")
    if args.action == "continue":
        if record.get("status") != "completed" or not record.get("session_id"):
            raise ValueError("Previous task did not complete; inspect its state before recovery")
        return
    if record.get("status") == "completed":
        raise ValueError("Completed tasks require continue, not recover")
    if not args.acknowledge_inspected:
        raise ValueError("Recovery requires --acknowledge-inspected after reviewing the workspace")
    if not record.get("session_id"):
        raise ValueError("Cannot recover without an existing session id; refusing to start fresh")
    residue = [pid for pid in (record.get("wrapper_pid"), record.get("child_pid")) if pid_alive(pid)]
    if residue:
        raise ValueError(f"Recovery blocked: residual live process {residue[0]}; inspect before recovery")
    if backend["kind"] in ("service", "server") and not args.acknowledge_session_idle:
        raise ValueError(
            "Shared/service recovery requires --acknowledge-session-idle: killing this client does not "
            "stop the server session. A parent/operator must confirm the session is idle on the server.")


def run_task(args: argparse.Namespace) -> int:
    """Execute run/continue/recover and print a compact result."""
    validate_task(args.task)
    if args.timeout < 0:
        raise ValueError("Timeout must be 0 (no cap) or positive")
    if args.stall_timeout <= 0:
        raise ValueError("Stall timeout must be positive")
    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / (args.task + ".json")
    lock_path = state_dir / (args.task + ".lock")
    sync_backend = backend_from_args(args)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig")
    if not prompt.strip():
        raise ValueError("Prompt is empty")
    prefix = command_prefix(args.opencode)

    # The record is loaded and validated only while the exclusive lock is held (no TOCTOU).
    preload = load_record(state_path) if (args.action != "run" and state_path.exists()) else None
    token = acquire_lock(lock_path, preload, allow_stale=(args.action == "recover"))
    try:
        if args.action == "run":
            if state_path.exists():
                raise ValueError("Task already exists; use continue or a new task name")
            record = {"task": args.task, "workspace": str(workspace), "model": args.model,
                      "backend": sync_backend, "session_id": reserved_session_id(args), "turn": 0,
                      "turns": [],
                      "created_at": utc_now(), "acceptance": "pending"}
        else:
            if not state_path.exists():
                raise ValueError("No task state found; use run for a new task")
            record = load_record(state_path)
            if args.model is None:
                # Inherit the route the task was created with, so only a deliberate change of model
                # is rejected. A task created without a pinned model stays unpinned.
                args.model = record.get("model")
            _validate_continuation(record, args, workspace, sync_backend)
        # Record the budget this turn actually ran under, so a later reviewer can tell a deliberate
        # cap from a stall without reading the command line back out of the logs.
        record["limits"] = {"timeout": args.timeout, "stall_timeout": args.stall_timeout}
        turn = record.get("turn", 0) + 1
        begin_turn(record, turn)
        store = StateStore(state_path, record)
        store.update(**record)
        if getattr(args, "startup_file", None):
            write_json(Path(args.startup_file), {"accepted": True, "dispatch_id": args.dispatch_id,
                       "task": args.task, "turn": turn, "pid": os.getpid(),
                       "session_id": record.get("session_id")})
        try:
            code = execute_turn(record, store, args, workspace, sync_backend, prefix, prompt)
        except KeyboardInterrupt:
            finalize(store, "needs_review", error=f"interrupted by user (Ctrl+C) on turn {turn}",
                     exit_code=None, interrupted=True,
                     server_session_may_be_running=sync_backend["kind"] in ("service", "server"))
            print(json.dumps({"status": "needs_review", "task": args.task,
                              "error": "interrupted by user (Ctrl+C)"}, ensure_ascii=False))
            return 130
        except Exception as exc:
            finalize(store, "needs_review", error=bound(str(exc)), exit_code=None,
                     server_session_may_be_running=sync_backend["kind"] in ("service", "server"))
            raise
        snapshot = store.snapshot()
        print(json.dumps({
            "status": snapshot.get("status"), "task": args.task, "turn": snapshot.get("turn"),
            "session_id": snapshot.get("session_id"), "backend": snapshot.get("backend"),
            "verified_model": snapshot.get("verified_model"), "error": snapshot.get("error"),
            "acceptance": snapshot.get("acceptance", "pending"),
            "server_session_may_be_running": snapshot.get("server_session_may_be_running", False),
            "text": snapshot.get("text"), "events_path": snapshot.get("events_path"),
            "stderr_path": snapshot.get("stderr_path"), "state_path": str(state_path),
        }, ensure_ascii=False))
        return code
    finally:
        release_lock(lock_path, token)


def status_snapshot(args: argparse.Namespace) -> dict:
    """Read-only status of one task; never starts OpenCode and never infers test success."""
    validate_task(args.task)
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_path = state_dir / (args.task + ".json")
    lock_path = state_dir / (args.task + ".lock")
    if not state_path.exists():
        return {"task": args.task, "state": "missing", "state_path": str(state_path),
                          "backend": None, "verification": None, "needs_review": True,
                          "acceptance": "pending", "evidence": {"state": str(state_path)}}
    record = load_record(state_path)
    status = record.get("status", "unknown")
    backend = describe_backend(record.get("backend"))
    heartbeat_age = age_seconds(record.get("heartbeat_at"))
    activity_age = age_seconds(record.get("last_event_at"))
    lock_record = read_lock(lock_path)
    owner = lock_record.get("pid") if lock_record else None
    pids = [record.get("wrapper_pid"), record.get("child_pid")]
    alive = pid_alive(owner) or any(pid_alive(pid) for pid in pids)
    if status == "running":
        liveness_state = "alive" if alive else "unknown"
        possibly_stale = (not alive) or heartbeat_age is None or heartbeat_age > args.stale_seconds
        if possibly_stale and alive:
            liveness_state = "possibly_stale"
    else:
        liveness_state = "not_applicable"
        possibly_stale = False
    verification = record.get("verification") or {}
    evidence = {
        "state": str(state_path),
        "events": record.get("events_path"),
        "stderr": record.get("stderr_path"),
    }
    return {
        "task": args.task,
        "state": status,
        "state_path": str(state_path),
        "liveness": {"state": liveness_state, "applicable": status == "running", "alive": alive,
                     "possibly_stale": possibly_stale, "heartbeat_age_seconds": heartbeat_age,
                     "timestamp_malformed": bool(record.get("heartbeat_at") and heartbeat_age is None),
                     "wrapper_pid": record.get("wrapper_pid"), "child_pid": record.get("child_pid")},
        "activity": {"current_event": record.get("current_event"), "last_tool": record.get("last_tool"),
                     "last_event_at": record.get("last_event_at"), "activity_age_seconds": activity_age,
                     "elapsed_seconds": record.get("elapsed_seconds"), "event_count": record.get("event_count")},
        "backend": backend,
        "model": record.get("model"),
        "session_id": record.get("session_id"),
        "observed_session_id": record.get("observed_session_id"),
        "turn": record.get("turn"),
        "updated_at": record.get("updated_at"),
        "verification": {"status": verification.get("status"), "verified_model": record.get("verified_model")},
        "acceptance": record.get("acceptance", "pending"),
        "needs_review": bool(record.get("needs_review") or status not in ("completed",)),
        "server_session_may_be_running": bool(record.get("server_session_may_be_running")),
        "evidence": evidence,
    }


def do_status(args: argparse.Namespace) -> int:
    print(json.dumps(status_snapshot(args), ensure_ascii=False))
    return 0


def reserved_session_id(args: argparse.Namespace) -> Optional[str]:
    """Return the caller-reserved session id for a fresh run, or None to let OpenCode mint one.

    Reserving the id pins the worker to the session a visible TUI will later attach to. The
    generated ids always match this pattern, so the check only rejects hand-written garbage
    before it can be passed to the CLI.
    """
    value = getattr(args, "session_id", None)
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise ValueError("--session-id must match [A-Za-z0-9_-]{1,120}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for all actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "continue", "recover", "status"))
    parser.add_argument("--workspace")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--prompt-file")
    parser.add_argument("--model",
                        help="Worker model route, e.g. " + RECOMMENDED_MODEL +
                             ". Omit to let OpenCode use its own configured default.")
    parser.add_argument("--opencode")
    parser.add_argument("--startup-file", help=argparse.SUPPRESS)
    parser.add_argument("--dispatch-id", help=argparse.SUPPRESS)
    parser.add_argument("--session-id",
                        help="run only: reserve this session id instead of letting OpenCode mint one")
    parser.add_argument("--timeout", type=float, default=_env_float("OC_DELEGATE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS),
                        help="Hard wall-clock cap in seconds. 0 (default) means no cap: a worker that "
                             "keeps producing output is never cut short. OC_DELEGATE_TIMEOUT overrides "
                             "the default.")
    parser.add_argument("--stall-timeout", type=float,
                        default=_env_float("OC_DELEGATE_STALL_TIMEOUT", DEFAULT_STALL_SECONDS),
                        help="Cut the turn when the worker produced no output for this many seconds AND "
                             "its process tree stopped using CPU. Silence alone never kills a long run. "
                             "OC_DELEGATE_STALL_TIMEOUT overrides the default.")
    parser.add_argument("--stale-seconds", type=float, default=DEFAULT_STALE_SECONDS)
    parser.add_argument("--acknowledge-inspected", action="store_true",
                        help="Required for recover: confirms the workspace was inspected after failure")
    parser.add_argument("--acknowledge-session-idle", action="store_true",
                        help="Required for shared/service recover: operator confirms the server session is idle")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument("--standalone", action="store_true",
                         help="Run with a private server instead of the shared background service")
    backend.add_argument("--server", help="Connect to a localhost/loopback HTTP(S) server URL")
    return parser


def main(argv: Optional[list] = None) -> int:
    """Entry point; validates action-specific requirements before dispatch."""
    args = build_parser().parse_args(argv)
    if args.action == "status":
        return do_status(args)
    for required in ("workspace", "prompt_file"):
        if not getattr(args, required):
            raise ValueError(f"--{required.replace('_', '-')} is required for {args.action}")
    return run_task(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as failure:
        if "--startup-file" in sys.argv:
            startup = Path(sys.argv[sys.argv.index("--startup-file") + 1])
            dispatch_id = sys.argv[sys.argv.index("--dispatch-id") + 1]
            if not startup.exists():
                write_json(startup, {"accepted": False, "dispatch_id": dispatch_id, "error": str(failure)})
        print(json.dumps({"status": "error", "error": str(failure)}, ensure_ascii=False))
        sys.exit(1)
