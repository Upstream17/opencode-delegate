"""Stall detection and no-hard-cap behaviour for the delegation helper.

These tests encode the two operating rules:

1. A worker that is still producing output must never be cut short by a wall-clock
   budget. Only an explicit ``--timeout`` may impose a cap.
2. A worker that has gone silent *and* stopped consuming CPU is a stall. It must be
   terminated promptly, and reported as ``stalled`` rather than as a timeout, so the
   parent agent can decide whether to recover it.

Silence alone is not evidence of a stall: a long solver run legitimately emits nothing
for minutes. That is why the busy-but-silent case is covered here.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DELEGATE = ROOT / "scripts" / "delegate.py"
FAKE = Path(__file__).resolve().parent / "fake_opencode.py"
MODEL = "opencode-go/deepseek-v4.1-flash"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class StallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            **({"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}))
        self.base = Path(self._tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.prompt = self.base / "prompt.txt"
        self.prompt.write_text("do the task", encoding="utf-8")
        self.env = dict(os.environ)
        self.env.pop("OC_DELEGATE_TIMEOUT", None)
        self.env.pop("OC_DELEGATE_STALL_TIMEOUT", None)

    def tearDown(self):
        self._tmp.cleanup()

    def run_delegate(self, *extra, task="t1", env=None, timeout=60):
        command = [sys.executable, str(DELEGATE), "run",
                   "--state-dir", str(self.state), "--task", task,
                   "--workspace", str(self.workspace),
                   "--opencode", str(FAKE),
                   "--prompt-file", str(self.prompt),
                   *extra]
        environment = dict(self.env)
        environment.update(env or {})
        return subprocess.run(command, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=environment)

    # ---- rule 1: a live worker is never cut short --------------------------

    def test_default_has_no_wall_clock_cap(self):
        """默认策略必须是不设墙钟上限，并把该策略写进任务记录。"""
        result = self.run_delegate("--model", MODEL, env={"FAKE_MODE": "slow_progress"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = read_json(self.state / "t1.json")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["limits"]["timeout"], 0)
        self.assertEqual(record["termination"], "completed")

    def test_zero_timeout_is_accepted_as_unlimited(self):
        """--timeout 0 必须被接受；旧实现直接拒绝 0。"""
        result = self.run_delegate("--model", MODEL, "--timeout", "0",
                                   env={"FAKE_MODE": "slow_progress"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = read_json(self.state / "t1.json")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["limits"]["timeout"], 0)

    # ---- rule 2: silent + idle is a stall, reported as such ----------------

    def test_stalled_worker_is_reported_as_stalled(self):
        result = self.run_delegate("--model", MODEL, "--stall-timeout", "1",
                                   env={"FAKE_MODE": "hang"}, timeout=40)
        self.assertNotEqual(result.returncode, 0)
        record = read_json(self.state / "t1.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["termination"], "stalled")
        self.assertTrue(record["error"].startswith("stalled:"), record["error"])
        self.assertGreaterEqual(record["silent_seconds"], 1.0)
        self.assertFalse((self.state / "t1.lock").exists())

    def test_busy_silent_worker_is_not_mistaken_for_a_stall(self):
        """静默但在烧 CPU 的长算例不允许被杀 —— 这是不误杀长任务的关键。"""
        result = self.run_delegate("--model", MODEL, "--stall-timeout", "1",
                                   env={"FAKE_MODE": "busy_silent"}, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = read_json(self.state / "t1.json")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["termination"], "completed")

    # ---- explicit caps and existing semantics ------------------------------

    def test_explicit_timeout_still_caps(self):
        result = self.run_delegate("--model", MODEL, "--timeout", "2",
                                   env={"FAKE_MODE": "hang"}, timeout=40)
        record = read_json(self.state / "t1.json")
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["termination"], "timeout")
        self.assertTrue(record["error"].startswith("timeout:"), record["error"])

    def test_stall_timeout_must_be_positive(self):
        result = self.run_delegate("--model", MODEL, "--stall-timeout", "0",
                                   env={"FAKE_MODE": "slow_progress"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stall", (result.stdout + result.stderr).lower())


class PsCpuProbeTest(unittest.TestCase):
    """The ps(1) fallback must work where /proc is absent (macOS), on every host.

    The tree summation is exercised as a pure function so this covers macOS even when the
    test suite runs on Windows or Linux.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts"))
        import delegate  # noqa: E402  (path set up above)
        cls.delegate = delegate

    def test_parses_ps_cputime_tokens(self):
        parse = self.delegate._parse_ps_cputime
        self.assertAlmostEqual(parse("0:00.05"), 0.05)
        self.assertAlmostEqual(parse("12:34.56"), 754.56)
        self.assertAlmostEqual(parse("1:02:03.45"), 3723.45)
        self.assertIsNone(parse("nonsense"))

    def test_sums_only_the_named_tree(self):
        output = "\n".join([
            "  100   1 0:00.10",   # root of the tree
            "  200 100 0:01.50",   # child
            "  300 200 0:02.00",   # grandchild: the real work happens here
            "  400   1 9:00.00",   # unrelated process must not be counted
            "  500   1 9:00.00",
        ])
        total = self.delegate._ps_tree_cpu_from_output(output, 100)
        self.assertAlmostEqual(total, 3.60)

    def test_unknown_pid_reports_unavailable(self):
        output = "  400   1 9:00.00"
        self.assertIsNone(self.delegate._ps_tree_cpu_from_output(output, 999))


if __name__ == "__main__":
    unittest.main()