"""Drive the opencode-delegate MCP server over stdio and time every response.

Diagnostic only. It speaks to the server exactly the way the Codex app does, so a fast
response here with a hanging in-app call means the client connection is stale, not the
server. Every path resolves relative to this file, so a copy works anywhere.

Usage: python mcp_probe.py <call-timeout-seconds> <tool> [json-args]
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
SERVER = Path(os.environ.get("OC_DELEGATE_SERVER") or (SKILL_DIR / "mcp_server.py"))
PY = os.environ.get("OC_DELEGATE_PYTHON") or sys.executable


def reader(stream, out: queue.Queue) -> None:
    for line in iter(stream.readline, ""):
        line = line.strip()
        if line:
            out.put(line)
    out.put(None)


def main() -> int:
    call_timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    tool = sys.argv[2] if len(sys.argv) > 2 else "oc_service"
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {"action": "status"}

    proc = subprocess.Popen(
        [PY, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=reader, args=(proc.stdout, lines), daemon=True).start()
    errs: queue.Queue = queue.Queue()
    threading.Thread(target=reader, args=(proc.stderr, errs), daemon=True).start()

    def call(payload: dict, label: str, timeout: float) -> dict | None:
        started = time.time()
        # ensure_ascii=False matches how the Codex app writes non-ASCII arguments, which is
        # the case that exposes a locale-encoded stdin in the server.
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        try:
            raw = lines.get(timeout=timeout)
        except queue.Empty:
            print(f"[{label}] NO RESPONSE after {time.time() - started:.1f}s")
            return None
        if raw is None:
            print(f"[{label}] stream closed after {time.time() - started:.1f}s")
            return None
        print(f"[{label}] {time.time() - started:.1f}s -> {raw[:400]}")
        return json.loads(raw)

    init = call(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
        "initialize",
        30,
    )
    if init is None:
        print("server never initialized; stderr follows")
        print(errs.get(timeout=2) if not errs.empty() else "(no stderr)")
        proc.kill()
        return 1

    # A notification has no id and never gets a reply; write it without waiting.
    proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}, ensure_ascii=False)
        + "\n"
    )
    proc.stdin.flush()
    call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "tools/list", 30)
    result = call(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        f"tools/call {tool}",
        call_timeout,
    )

    proc.kill()
    if result is None:
        print("--- server stderr ---")
        while not errs.empty():
            print(errs.get()[:2000])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
