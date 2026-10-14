"""Fake OpenCode executable for offline tests.

Emulates the subset of `opencode run --format json` and `opencode session export` that the
delegation helper relies on. Behaviour is selected with the FAKE_MODE environment variable and
no network or paid model calls are made.
"""
import json
import os
import sys
import time

DEFAULT_MODEL = "opencode-go/deepseek-v4.1-flash"


def emit(payload: dict) -> None:
    """Write one JSONL event and flush immediately."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def model_route(requested: str) -> dict:
    """Build an export model route, optionally mismatching the requested model."""
    if os.environ.get("FAKE_MODE") == "mismatch":
        return {"providerID": "opencode-free", "id": "other-model", "variant": "default"}
    provider, _, rest = requested.partition("/")
    model_id, _, variant = rest.partition("#")
    return {"providerID": provider, "id": model_id or "deepseek-v4.1-flash",
            "variant": variant or "default"}


def handle_export(argv: list) -> int:
    """Emit a minimal session export document."""
    session_id = argv[-1] if argv else "ses_fake"
    document = {"info": {"id": session_id}, "messages": [{"model": model_route(DEFAULT_MODEL)}]}
    sys.stdout.write(json.dumps(document))
    sys.stdout.flush()
    return 0


def handle_run() -> int:
    """Emit synthetic run events according to FAKE_MODE."""
    mode = os.environ.get("FAKE_MODE", "ok")
    session_id = os.environ.get("FAKE_SESSION", "ses_fake0000000000000000000000")
    if os.environ.get("FAKE_HONOR_SESSION") and "--session" in sys.argv:
        session_id = sys.argv[sys.argv.index("--session") + 1]
    if mode == "no_session":
        session_id = None

    def text(value: str):
        payload = {"type": "text", "part": {"type": "text", "text": value}}
        if session_id:
            payload["sessionID"] = session_id
        return payload

    if mode == "stream":
        emit(text("start"))
        time.sleep(5.0)
        emit({"type": "tool", "sessionID": session_id,
              "part": {"type": "tool", "tool": "bash", "state": {"status": "running"}}})
        time.sleep(0.4)
        emit(text("stream complete"))
        return 0
    if mode == "hang":
        emit(text("working"))
        time.sleep(60)
        return 0
    if mode == "stderr":
        emit(text("ok"))
        sys.stderr.write("warning: synthetic stderr line\n")
        sys.stderr.flush()
        return 0
    if mode == "nonzero":
        emit(text("partial"))
        return 3
    if mode == "error_then_text":
        emit({"type": "error", "sessionID": session_id,
              "error": {"name": "APIError", "data": {"message": "synthetic boom"}}})
        emit(text("I claim I finished"))
        return 0
    if mode == "badjson":
        sys.stdout.write("[1, 2, 3]\n")
        sys.stdout.flush()
        emit(text("scalar line ignored"))
        return 0
    if mode == "mixed":
        emit({"type": "text", "sessionID": "ses_AAAA", "part": {"type": "text", "text": "a"}})
        emit({"type": "text", "sessionID": "ses_BBBB", "part": {"type": "text", "text": "b"}})
        return 0
    emit(text("hello world"))
    emit({"type": "step-finish", "sessionID": session_id})
    return 0


def main() -> int:
    """Dispatch on the first positional argument."""
    argv = sys.argv[1:]
    log_path = os.environ.get("FAKE_ARGV_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    if argv[:2] == ["session", "export"]:
        return handle_export(argv)
    return handle_run()


if __name__ == "__main__":
    sys.exit(main())
