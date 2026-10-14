"""Offline unit and integration tests for the delegation helper.

All tests use the fake executable in tests/fake_opencode.py: no network access and no paid model
calls. Tests exercise streaming state, backend selection, guards, locking, recovery and the
read-only status action.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DELEGATE = ROOT / "scripts" / "delegate.py"
FAKE = Path(__file__).resolve().parent / "fake_opencode.py"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class DelegateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self._tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.prompt = self.base / "prompt.txt"
        self.prompt.write_text("do the task", encoding="utf-8")
        self.env = dict(os.environ)
        self.env["FAKE_ARGV_LOG"] = str(self.base / "argv.log")

    def tearDown(self):
        self._tmp.cleanup()

    def run_delegate(self, *extra, action="run", task="t1", env=None, timeout=90, prompt=None):
        command = [sys.executable, str(DELEGATE), action,
                   "--state-dir", str(self.state), "--task", task,
                   "--workspace", str(self.workspace),
                   "--opencode", str(FAKE),
                   "--prompt-file", str(prompt or self.prompt),
                   *extra]
        environment = dict(self.env)
        environment.update(env or {})
        return subprocess.run(command, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=environment)

    def argv_log(self):
        path = self.base / "argv.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ---- streaming / state -------------------------------------------------

    def test_incremental_visibility_before_exit(self):
        command = [sys.executable, str(DELEGATE), "run", "--state-dir", str(self.state),
                   "--task", "stream", "--workspace", str(self.workspace), "--opencode", str(FAKE),
                   "--prompt-file", str(self.prompt)]
        environment = dict(self.env)
        environment["FAKE_MODE"] = "stream"
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", errors="replace", env=environment)
        state_path = self.state / "stream.json"
        seen = None
        deadline = time.time() + 10
        while time.time() < deadline and process.poll() is None:
            if state_path.exists():
                record = read_json(state_path)
                if record.get("status") == "running" and record.get("observed_session_id"):
                    seen = record
                    break
            time.sleep(0.15)
        self.assertIsNotNone(seen, "state did not expose observed_session_id while running")
        self.assertTrue(seen.get("observed_session_id"))
        self.assertTrue(seen.get("heartbeat_at"))
        self.assertTrue(seen.get("last_event_at"))
        self.assertEqual(seen.get("current_event"), "text")
        events = Path(seen["events_path"])
        self.assertTrue(events.exists() and events.stat().st_size > 0)
        try:
            process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertFalse((self.state / "stream.lock").exists(), "lock not released")

    def test_heartbeat_advances_without_new_activity(self):
        command = [sys.executable, str(DELEGATE), "run", "--state-dir", str(self.state),
                   "--task", "hb", "--workspace", str(self.workspace), "--opencode", str(FAKE),
                   "--prompt-file", str(self.prompt)]
        environment = dict(self.env)
        environment["FAKE_MODE"] = "stream"
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", errors="replace", env=environment)
        state_path = self.state / "hb.json"
        first = None
        deadline = time.time() + 10
        while time.time() < deadline and process.poll() is None:
            if state_path.exists():
                record = read_json(state_path)
                if record.get("last_event_at") and record.get("heartbeat_at"):
                    first = record
                    break
            time.sleep(0.1)
        self.assertIsNotNone(first)
        observed = False
        deadline = time.time() + 12
        while time.time() < deadline and process.poll() is None:
            record = read_json(state_path)
            if record.get("heartbeat_at") != first["heartbeat_at"] and record.get("last_event_at") == first["last_event_at"]:
                observed = True
                break
            time.sleep(0.2)
        process.communicate(timeout=30)
        self.assertTrue(observed, "heartbeat should advance while activity timestamp is unchanged")

    def test_stderr_persisted(self):
        result = self.run_delegate(task="err", env={"FAKE_MODE": "stderr"})
        self.assertEqual(result.returncode, 0, result.stderr)
        record = read_json(self.state / "err.json")
        stderr_path = Path(record["stderr_path"])
        self.assertTrue(stderr_path.exists())
        self.assertIn("synthetic stderr", stderr_path.read_text(encoding="utf-8"))

    def test_error_then_final_text_is_not_completed(self):
        result = self.run_delegate(task="errtxt", env={"FAKE_MODE": "error_then_text"})
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "errtxt.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertTrue(record["needs_review"])
        self.assertIsNone(record["verified_model"])
        self.assertIsNone(record["verification"])
        self.assertIn("error event", record["error"])

    # ---- verification / model ---------------------------------------------

    def test_model_mismatch_needs_review(self):
        result = self.run_delegate(task="mm", env={"FAKE_MODE": "mismatch"})
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "mm.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["verification"]["status"], "mismatch")
        self.assertIsNone(record["verified_model"])

    def test_continuation_clears_stale_verification(self):
        first = self.run_delegate(task="cont")
        self.assertEqual(first.returncode, 0, first.stderr)
        record = read_json(self.state / "cont.json")
        self.assertIsNotNone(record["verified_model"])
        self.assertEqual(record["turn"], 1)
        second = self.run_delegate(action="continue", task="cont", env={"FAKE_MODE": "mismatch"})
        self.assertNotEqual(second.returncode, 0)
        record = read_json(self.state / "cont.json")
        self.assertEqual(record["turn"], 2)
        self.assertIsNone(record["verified_model"], "stale verified_model leaked into new turn")
        self.assertEqual(record["verification"]["status"], "mismatch")
        self.assertEqual(len(record["turns"]), 1)
        self.assertIsNotNone(record["turns"][0]["verified_model"], "previous turn evidence not retained")

    def test_continue_uses_same_session(self):
        self.run_delegate(task="resume")
        first = read_json(self.state / "resume.json")
        session = first["session_id"]
        self.run_delegate(action="continue", task="resume")
        run_calls = [entry for entry in self.argv_log() if entry and entry[0] == "run"]
        self.assertEqual(len(run_calls), 2)
        self.assertIn("--session", run_calls[1])
        self.assertEqual(run_calls[1][run_calls[1].index("--session") + 1], session)

    # ---- caller-reserved session ids --------------------------------------

    def test_reserved_session_id_is_used_and_recorded(self):
        # A reserved id lets a visible TUI attach before the worker starts, so the worker must
        # create that exact session instead of minting its own.
        reserved = "ses_reserved0000000000000000AA"
        result = self.run_delegate("--session-id", reserved, task="reserved",
                                   env={"FAKE_SESSION": reserved})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = read_json(self.state / "reserved.json")
        self.assertEqual(record["session_id"], reserved)
        record_session = record["session_id"]
        run_calls = [entry for entry in self.argv_log() if entry and entry[0] == "run"]
        self.assertEqual(run_calls[0][run_calls[0].index("--session") + 1], record_session)
        # Both flags together: the id is pinned AND the session stays findable by task name,
        # which is what lets a later worker look this session up for context.
        self.assertEqual(run_calls[0][run_calls[0].index("--title") + 1], "codex-delegate:reserved")

    def test_continuation_does_not_relabel_the_session(self):
        self.run_delegate(task="label")
        self.run_delegate(action="continue", task="label")
        run_calls = [entry for entry in self.argv_log() if entry and entry[0] == "run"]
        self.assertIn("--title", run_calls[0], "a new run must label the session")
        self.assertNotIn("--title", run_calls[1],
                         "a continuation must not clobber a manual rename in the TUI")

    def test_reserved_session_id_mismatch_needs_review(self):
        reserved = "ses_reserved0000000000000000BB"
        result = self.run_delegate("--session-id", reserved, task="reserved-mismatch",
                                   env={"FAKE_SESSION": "ses_someoneelse000000000000000"})
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "reserved-mismatch.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertIn("session mismatch", record["error"])
        self.assertEqual(record["session_id"], reserved, "the reserved id stays authoritative")

    def test_reserved_session_id_rejects_malformed(self):
        result = self.run_delegate("--session-id", "bad id!", task="reserved-bad")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--session-id must match", result.stdout + result.stderr)

    # ---- timeout / cancellation -------------------------------------------

    def test_timeout_cleanup(self):
        result = self.run_delegate("--timeout", "2", task="timeout", env={"FAKE_MODE": "hang"}, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "timeout.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertIn("timeout", record["error"])
        self.assertIsNotNone(record["child_pid"])
        self.assertFalse((self.state / "timeout.lock").exists())
        alive = True
        if os.name == "nt":
            probe = subprocess.run(["tasklist", "/FI", f"PID eq {record['child_pid']}"],
                                   capture_output=True, encoding="utf-8", errors="replace")
            alive = str(record["child_pid"]) in probe.stdout
        else:
            alive = False
        self.assertFalse(alive, "child process survived timeout cleanup")

    # ---- backends ----------------------------------------------------------

    def test_default_backend_is_shared_service(self):
        self.run_delegate(task="svc")
        record = read_json(self.state / "svc.json")
        self.assertEqual(record["backend"], {"kind": "service", "server": None})
        export_calls = [entry for entry in self.argv_log() if entry[:2] == ["session", "export"]]
        self.assertTrue(export_calls)
        self.assertNotIn("--standalone", export_calls[0])
        self.assertNotIn("--server", export_calls[0])

    def test_standalone_backend_flags(self):
        self.run_delegate("--standalone", task="solo")
        record = read_json(self.state / "solo.json")
        self.assertEqual(record["backend"]["kind"], "standalone")
        for entry in self.argv_log():
            self.assertIn("--standalone", entry)

    def test_explicit_loopback_server_backend(self):
        self.run_delegate("--server", "http://127.0.0.1:49374", task="srv")
        record = read_json(self.state / "srv.json")
        self.assertEqual(record["backend"], {"kind": "server", "server": "http://127.0.0.1:49374"})
        export_calls = [entry for entry in self.argv_log() if entry[:2] == ["session", "export"]]
        self.assertIn("--server", export_calls[0])

    def test_public_server_rejected(self):
        result = self.run_delegate("--server", "http://example.com:8080", task="pub")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("loopback", result.stdout + result.stderr)

    def test_standalone_and_server_mutually_exclusive(self):
        result = self.run_delegate("--standalone", "--server", "http://127.0.0.1:1", task="both")
        self.assertNotEqual(result.returncode, 0)

    # ---- guards ------------------------------------------------------------

    def test_bad_and_reserved_task_names(self):
        bad = self.run_delegate(task="bad name!")
        self.assertNotEqual(bad.returncode, 0)
        reserved = self.run_delegate(task="con")
        self.assertNotEqual(reserved.returncode, 0)
        self.assertIn("reserved", reserved.stdout + reserved.stderr)

    def test_duplicate_run_rejected_while_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            command = [sys.executable, str(DELEGATE), "run", "--state-dir", str(state),
                       "--task", "dup", "--workspace", str(self.workspace), "--opencode", str(FAKE),
                       "--prompt-file", str(self.prompt)]
            environment = dict(self.env)
            environment["FAKE_MODE"] = "stream"
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       encoding="utf-8", errors="replace", env=environment)
            deadline = time.time() + 10
            while time.time() < deadline and not (state / "dup.json").exists():
                time.sleep(0.1)
            self.assertTrue((state / "dup.json").exists())
            second = subprocess.run(command, capture_output=True, encoding="utf-8",
                                    errors="replace", env=environment, timeout=30)
            self.assertNotEqual(second.returncode, 0)
            output = second.stdout + second.stderr
            self.assertTrue("lock" in output or "already exists" in output, output)
            process.communicate(timeout=30)

    def test_workspace_and_model_continuation_guards(self):
        self.run_delegate(task="guard")
        other_ws = self.base / "other"
        other_ws.mkdir()
        mismatch = subprocess.run(
            [sys.executable, str(DELEGATE), "continue", "--state-dir", str(self.state), "--task", "guard",
             "--workspace", str(other_ws), "--opencode", str(FAKE), "--prompt-file", str(self.prompt)],
            capture_output=True, encoding="utf-8", errors="replace", env=self.env, timeout=60)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("original workspace and model", mismatch.stdout + mismatch.stderr)

    def test_continue_of_failed_task_rejected(self):
        self.run_delegate(task="failed", env={"FAKE_MODE": "mismatch"})
        result = self.run_delegate(action="continue", task="failed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not complete", result.stdout + result.stderr)

    def test_recover_requires_acknowledgement_and_resumes_same_session(self):
        self.run_delegate(task="rec", env={"FAKE_MODE": "mismatch"})
        record = read_json(self.state / "rec.json")
        session = record["session_id"]
        denied = self.run_delegate(action="recover", task="rec")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("acknowledge-inspected", denied.stdout + denied.stderr)
        approved = self.run_delegate("--acknowledge-inspected", "--acknowledge-session-idle",
                                     action="recover", task="rec")
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        run_calls = [entry for entry in self.argv_log() if entry and entry[0] == "run"]
        self.assertEqual(run_calls[-1][run_calls[-1].index("--session") + 1], session)

    def test_recover_backend_mismatch_rejected(self):
        self.run_delegate("--standalone", task="rb", env={"FAKE_MODE": "mismatch"})
        result = self.run_delegate("--acknowledge-inspected", action="recover", task="rb")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Backend mismatch", result.stdout + result.stderr)

    # ---- status (read-only) ------------------------------------------------

    def test_status_missing_state(self):
        result = subprocess.run(
            [sys.executable, str(DELEGATE), "status", "--state-dir", str(self.state), "--task", "nope"],
            capture_output=True, encoding="utf-8", errors="replace", env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "missing")
        self.assertIsNone(payload["backend"])

    def test_status_flags_stale_running_state(self):
        old = "2020-01-01T00:00:00+00:00"
        (self.state / "stale.json").write_text(json.dumps({
            "task": "stale", "status": "running", "backend": {"kind": "service", "server": None},
            "heartbeat_at": old, "wrapper_pid": None, "child_pid": None, "model": "m",
            "verified_model": None, "verification": None, "needs_review": False,
            "events_path": None, "stderr_path": None,
        }), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(DELEGATE), "status", "--state-dir", str(self.state), "--task", "stale"],
            capture_output=True, encoding="utf-8", errors="replace", env=self.env, timeout=30)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "running")
        self.assertTrue(payload["liveness"]["possibly_stale"])
        self.assertTrue(payload["needs_review"])


    # ---- session / stream integrity ---------------------------------------

    def test_continuation_session_mismatch_needs_review(self):
        self.run_delegate(task="sess")
        first = read_json(self.state / "sess.json")
        result = self.run_delegate(action="continue", task="sess",
                                   env={"FAKE_SESSION": "ses_DIFFERENT00000000000000000"})
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "sess.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertIn("session mismatch", record["error"])
        self.assertEqual(record["session_id"], first["session_id"], "authoritative session overwritten")

    def test_mixed_session_ids_needs_review(self):
        result = self.run_delegate(task="mixed", env={"FAKE_MODE": "mixed"})
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "mixed.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertIn("multiple session", record["error"])
        self.assertEqual(record["session_id"], "ses_AAAA", "mixed session must keep the first id")

    def test_malformed_json_line_needs_review(self):
        result = self.run_delegate(task="badjson", env={"FAKE_MODE": "badjson"})
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "badjson.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertIn("unparseable", record["error"])
        raw = Path(record["events_path"]).read_text(encoding="utf-8")
        self.assertIn("[1, 2, 3]", raw, "raw line must be retained even when not trusted")
        self.assertNotIn("[1, 2, 3]", json.dumps(record.get("parse_errors")))
        for item in record.get("parse_errors", []):
            self.assertTrue(set(item) <= {"line", "error"}, item)

    def test_error_event_retains_adopted_session_and_summary(self):
        result = self.run_delegate(task="errsess", env={"FAKE_MODE": "error_then_text"})
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "errsess.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertIsNotNone(record["session_id"], "new run must retain the first id for recover")
        self.assertEqual(record["event_errors"][0].get("name"), "APIError")
        self.assertNotIn("boom", json.dumps(record["event_errors"]))

    def test_recover_dead_owner_live_child_shared_rejected(self):
        record = {"task": "dc", "workspace": str(self.workspace.resolve()),
                  "model": "opencode-go/deepseek-v4.1-flash", "backend": {"kind": "service", "server": None},
                  "session_id": "ses_dc", "turn": 1, "status": "needs_review", "needs_review": True,
                  "wrapper_pid": None, "child_pid": os.getpid(), "turns": []}
        (self.state / "dc.json").write_text(json.dumps(record), encoding="utf-8")
        (self.state / "dc.lock").write_text(json.dumps({"pid": self._dead_pid(), "token": "t"}), encoding="utf-8")
        before = (self.state / "dc.json").read_bytes()
        result = self.run_delegate("--acknowledge-inspected", "--acknowledge-session-idle",
                                   action="recover", task="dc")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock", result.stdout + result.stderr)
        self.assertEqual((self.state / "dc.json").read_bytes(), before)

    def test_recover_live_child_no_lock_shared_rejected(self):
        record = {"task": "lc", "workspace": str(self.workspace.resolve()),
                  "model": "opencode-go/deepseek-v4.1-flash", "backend": {"kind": "service", "server": None},
                  "session_id": "ses_lc", "turn": 1, "status": "needs_review", "needs_review": True,
                  "wrapper_pid": None, "child_pid": os.getpid(), "turns": []}
        (self.state / "lc.json").write_text(json.dumps(record), encoding="utf-8")
        before = (self.state / "lc.json").read_bytes()
        result = self.run_delegate("--acknowledge-inspected", "--acknowledge-session-idle",
                                   action="recover", task="lc")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("residual live process", result.stdout + result.stderr)
        self.assertEqual((self.state / "lc.json").read_bytes(), before)

    def test_recover_unknown_legacy_lock_fails_closed(self):
        record = {"task": "leg", "workspace": str(self.workspace.resolve()),
                  "model": "opencode-go/deepseek-v4.1-flash", "backend": {"kind": "service", "server": None},
                  "session_id": "ses_leg", "turn": 1, "status": "needs_review", "needs_review": True, "turns": []}
        (self.state / "leg.json").write_text(json.dumps(record), encoding="utf-8")
        (self.state / "leg.lock").write_text("", encoding="utf-8")
        before = (self.state / "leg.json").read_bytes()
        result = self.run_delegate("--acknowledge-inspected", "--acknowledge-session-idle",
                                   action="recover", task="leg")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock", result.stdout + result.stderr)
        self.assertEqual((self.state / "leg.json").read_bytes(), before)

    def test_completed_turn_requires_parent_acceptance(self):
        self.run_delegate(task="acc")
        record = read_json(self.state / "acc.json")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["acceptance"], "pending")
        self.assertFalse(record["needs_review"])

    def test_timeout_sets_server_session_flag(self):
        self.run_delegate("--timeout", "2", task="tsrv", env={"FAKE_MODE": "hang"}, timeout=60)
        record = read_json(self.state / "tsrv.json")
        self.assertTrue(record["server_session_may_be_running"])

    # ---- recovery acknowledgement / residual process -----------------------

    def test_shared_recovery_requires_idle_ack(self):
        self.run_delegate(task="idle", env={"FAKE_MODE": "mismatch"})
        before = (self.state / "idle.json").read_bytes()
        blocked = self.run_delegate("--acknowledge-inspected", action="recover", task="idle")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("acknowledge-session-idle", blocked.stdout + blocked.stderr)
        self.assertEqual((self.state / "idle.json").read_bytes(), before, "rejected recovery mutated state")

    def test_recover_blocks_on_live_residual_process(self):
        record = {"task": "live", "workspace": str(self.workspace.resolve()),
                  "model": "opencode-go/deepseek-v4.1-flash",
                  "backend": {"kind": "standalone", "server": None}, "session_id": "ses_live",
                  "turn": 1, "status": "needs_review", "needs_review": True,
                  "wrapper_pid": os.getpid(), "child_pid": None, "turns": []}
        (self.state / "live.json").write_text(json.dumps(record), encoding="utf-8")
        before = (self.state / "live.json").read_bytes()
        result = self.run_delegate("--standalone", "--acknowledge-inspected", "--acknowledge-session-idle",
                                   action="recover", task="live")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("residual live process", result.stdout + result.stderr)
        self.assertEqual((self.state / "live.json").read_bytes(), before)

    def _dead_pid(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        return process.pid

    def test_status_reports_activity_and_live_liveness(self):
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        record = {"task": "act", "workspace": str(self.workspace), "model": "m",
                  "backend": {"kind": "service", "server": None}, "session_id": "ses_x",
                  "observed_session_id": "ses_x", "turn": 1, "status": "running",
                  "heartbeat_at": now, "last_event_at": now, "current_event": "tool:tool",
                  "last_tool": "bash:running", "elapsed_seconds": 3.2, "event_count": 4,
                  "wrapper_pid": os.getpid(), "child_pid": None, "needs_review": False}
        (self.state / "act.json").write_text(json.dumps(record), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(DELEGATE), "status", "--state-dir", str(self.state), "--task", "act"],
            capture_output=True, encoding="utf-8", errors="replace", env=self.env, timeout=30)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["activity"]["current_event"], "tool:tool")
        self.assertEqual(payload["activity"]["last_tool"], "bash:running")
        self.assertEqual(payload["activity"]["activity_age_seconds"] is not None, True)
        self.assertTrue(payload["liveness"]["alive"])
        self.assertFalse(payload["liveness"]["possibly_stale"])

    def test_status_dead_running_with_lock_is_stale(self):
        dead = self._dead_pid()
        (self.state / "dead.lock").write_text(json.dumps({"pid": dead, "token": "t"}), encoding="utf-8")
        record = {"task": "dead", "status": "running", "backend": {"kind": "service", "server": None},
                  "heartbeat_at": None, "wrapper_pid": None, "child_pid": None, "model": "m",
                  "verified_model": None, "verification": None, "needs_review": False}
        (self.state / "dead.json").write_text(json.dumps(record), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(DELEGATE), "status", "--state-dir", str(self.state), "--task", "dead"],
            capture_output=True, encoding="utf-8", errors="replace", env=self.env, timeout=30)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["liveness"]["alive"])
        self.assertTrue(payload["liveness"]["possibly_stale"])
        self.assertEqual(payload["liveness"]["state"], "unknown")

    def test_status_terminal_liveness_not_applicable(self):
        record = {"task": "done", "status": "completed", "needs_review": False,
                  "backend": {"kind": "service", "server": None}, "acceptance": "pending"}
        (self.state / "done.json").write_text(json.dumps(record), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(DELEGATE), "status", "--state-dir", str(self.state), "--task", "done"],
            capture_output=True, encoding="utf-8", errors="replace", env=self.env, timeout=30)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["liveness"]["state"], "not_applicable")
        self.assertFalse(payload["liveness"]["applicable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
