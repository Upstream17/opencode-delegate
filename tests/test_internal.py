"""Unit tests for internal helpers that need no child process (fast, offline).

Covers 64-bit pid handling, URL/backend validation, and failure propagation in the streaming
runner using a mocked subprocess so a broken stream can never look successful.
"""
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import delegate  # noqa: E402


class FakeProcess:
    """Minimal Popen stand-in with immediate exit."""

    def __init__(self, out_lines=(), err_lines=(), returncode=0):
        self.pid = 987654
        self.returncode = returncode
        self.stdout = io.StringIO("".join(out_lines))
        self.stderr = io.StringIO("".join(err_lines))
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class HangingProcess(FakeProcess):
    """Popen stand-in that never exits until killed."""

    def wait(self, timeout=None):
        if self.killed:
            return self.returncode
        time.sleep(0.01)
        raise subprocess.TimeoutExpired("cmd", timeout)

    def poll(self):
        return self.returncode if self.killed else None


class InternalTest(unittest.TestCase):
    def test_atomic_write_retries_transient_windows_reader_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            replace = Path.replace
            calls = []
            def transient(source, target):
                calls.append(source)
                if len(calls) == 1:
                    raise PermissionError('sharing violation')
                return replace(source, target)
            with mock.patch.object(Path, 'replace', transient):
                delegate.write_json(path, {'event_count':8})
            self.assertEqual(__import__('json').loads(path.read_text())['event_count'], 8)
            self.assertEqual(len(calls), 2)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_pid_alive_self_and_dead(self):
        self.assertTrue(delegate.pid_alive(os.getpid()))
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        self.assertFalse(delegate.pid_alive(process.pid))
        self.assertFalse(delegate.pid_alive(None))
        self.assertFalse(delegate.pid_alive(0))

    def test_validate_server_accepts_loopback(self):
        self.assertEqual(delegate.validate_server("http://127.0.0.1:49374"), "http://127.0.0.1:49374")
        self.assertEqual(delegate.validate_server("http://localhost:8080/"), "http://localhost:8080")
        self.assertEqual(delegate.validate_server("https://127.0.0.1"), "https://127.0.0.1")

    def test_validate_server_rejects_bad_urls(self):
        for url in ("http://127.0.0.1:49374/path", "http://127.0.0.1:49374/?x=1",
                    "http://127.0.0.1:49374/#frag", "http://user:pass@127.0.0.1:49374",
                    "http://example.com:80", "ftp://127.0.0.1:1", "http://127.0.0.1:99999"):
            with self.assertRaises(ValueError, msg=url):
                delegate.validate_server(url)

    def test_validate_backend(self):
        self.assertEqual(delegate.validate_backend(None), {"kind": "standalone", "server": None})
        self.assertEqual(delegate.validate_backend({"kind": "server", "server": "http://127.0.0.1:1"}),
                         {"kind": "server", "server": "http://127.0.0.1:1"})
        with self.assertRaises(ValueError):
            delegate.validate_backend({"kind": "bogus"})
        with self.assertRaises(ValueError):
            delegate.validate_backend({"kind": "server", "server": "http://example.com"})
        with self.assertRaises(ValueError):
            delegate.validate_backend({"kind": "server", "server": None})

    def test_reader_callback_failure_is_surfaced(self):
        def failing(_line):
            raise RuntimeError("callback boom")

        runner = delegate.StreamRunner(["x"], ".", 5, lambda pid: None, failing,
                                       lambda line: None, lambda elapsed: None)
        with mock.patch.object(delegate.subprocess, "Popen", lambda *a, **k: FakeProcess(out_lines=['{"a":1}\n'])):
            with mock.patch.object(delegate, "kill_tree") as kill:
                with self.assertRaises(RuntimeError) as caught:
                    runner.run()
        self.assertIn("callback boom", str(caught.exception))
        self.assertFalse(kill.called, "already-exited process must not be killed")

    def test_on_tick_failure_terminates_child(self):
        def failing_tick(_elapsed):
            raise RuntimeError("tick boom")

        runner = delegate.StreamRunner(["x"], ".", 5, lambda pid: None, lambda line: None,
                                       lambda line: None, failing_tick)
        with mock.patch.object(delegate.subprocess, "Popen", lambda *a, **k: HangingProcess()):
            with mock.patch.object(delegate, "kill_tree") as kill:
                with self.assertRaises(RuntimeError) as caught:
                    runner.run()
        self.assertIn("tick boom", str(caught.exception))
        self.assertTrue(kill.called)

    def test_on_start_failure_terminates_child(self):
        def failing_start(_pid):
            raise RuntimeError("start boom")

        runner = delegate.StreamRunner(["x"], ".", 5, failing_start, lambda line: None,
                                       lambda line: None, lambda elapsed: None)
        with mock.patch.object(delegate.subprocess, "Popen", lambda *a, **k: HangingProcess()):
            with mock.patch.object(delegate, "kill_tree") as kill:
                with self.assertRaises(RuntimeError) as caught:
                    runner.run()
        self.assertIn("start boom", str(caught.exception))
        self.assertTrue(kill.called)

    def test_reader_failure_terminates_hanging_child(self):
        def failing(_line):
            raise RuntimeError("reader boom")

        runner = delegate.StreamRunner(["x"], ".", 30, lambda pid: None, failing,
                                       lambda line: None, lambda elapsed: None)
        with mock.patch.object(delegate.subprocess, "Popen",
                               lambda *a, **k: HangingProcess(out_lines=['{"a":1}\n'])):
            with mock.patch.object(delegate, "kill_tree") as kill:
                with self.assertRaises(RuntimeError) as caught:
                    runner.run()
        self.assertIn("reader boom", str(caught.exception))
        self.assertTrue(kill.called, "hanging child must be terminated on reader failure")

    def test_summarize_event_is_payload_free(self):
        event = {"type": "tool", "part": {"type": "tool", "tool": "bash",
                                          "state": {"input": "SECRET"}}}
        summary = delegate.summarize_tool(event)
        self.assertNotIn("SECRET", summary)
        self.assertNotIn("SECRET", delegate.summarize_event(event))


class ToolSchemaTest(unittest.TestCase):
    """Guard the wire shape of the tools/list payload.

    `description` must serialize to a JSON *string*. A stray trailing comma inside a
    parenthesized implicit string concatenation turns it into a 1-tuple, which becomes a
    JSON array; rmcp deserializes Tool.description as Option<String>, so the whole
    tools/list response is rejected and the Codex app drops the server with
    "Unexpected response type". A plain handshake probe cannot see this, so it is asserted
    here on the JSON that actually goes over the wire.
    """

    def _wire_tools(self):
        import json

        sys.path.insert(0, str(ROOT))
        import mcp_server  # noqa: E402  (import after ROOT is on sys.path)

        return json.loads(json.dumps({"tools": mcp_server.TOOLS}))

    def test_tools_match_mcp_wire_shape(self):
        known = {"name", "description", "inputSchema", "outputSchema",
                 "annotations", "title", "icons", "_meta"}
        tools = self._wire_tools()["tools"]
        self.assertTrue(tools, "server must advertise at least one tool")
        for tool in tools:
            with self.subTest(tool=tool.get("name")):
                self.assertIsInstance(tool.get("name"), str)
                self.assertEqual(set(tool) - known, set(), "unknown tool keys reach the client")
                self.assertIsInstance(tool.get("description"), str)
                schema = tool.get("inputSchema")
                self.assertIsInstance(schema, dict)
                self.assertEqual(schema.get("type"), "object")
                props = schema.get("properties", {})
                self.assertIsInstance(props, dict)
                for name in schema.get("required", []):
                    self.assertIn(name, props, "required key missing from properties")


if __name__ == "__main__":
    unittest.main(verbosity=2)
