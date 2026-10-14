"""Release regressions, including a real Node -> Python stdio handshake; no model calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import delegate
import runtime


class ReleaseTest(unittest.TestCase):
    def test_unsupported_viewer_never_spawns_or_reads_task(self):
        with mock.patch.object(runtime, 'VIEWER_SUPPORTED', False), \
             mock.patch.object(runtime, 'read_state') as read, \
             mock.patch.object(runtime.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'only on Windows'):
                runtime.watch('nonexistent-task')
        read.assert_not_called()
        spawn.assert_not_called()

    def test_posix_default_disables_viewer_even_with_env_opt_in(self):
        code = ('import sys; sys.path.insert(0,sys.argv[1]); import runtime; '
                'assert runtime.WATCH_DEFAULT is (sys.platform == "win32")')
        env = runtime.child_env() | {'OC_DELEGATE_WATCH': '1'}
        proc = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT / 'scripts')],
                              env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_route_identity_is_unverifiable(self):
        for route in ({}, {'variant': 'default'}, {'providerID': 'p', 'id': ''},
                      {'providerID': None, 'id': 'm'}, 'invalid', {'providerID': ' ', 'id': 'm'}):
            with self.subTest(route=route), mock.patch.object(delegate, 'run_blocking',
                return_value=(0, json.dumps({'messages': [{'model': route}]}), '', False)):
                verification, model = delegate.verify_model(['fake'], {'kind': 'service'}, 'ses_fake', str(ROOT), None)
                self.assertEqual(verification['status'], 'unverifiable')
                self.assertIsNone(model)

    def test_posix_kill_targets_only_owned_process_group(self):
        proc = mock.Mock(pid=999999)
        proc._delegate_process_group = True
        with mock.patch.object(delegate, 'os') as system, \
             mock.patch.object(delegate.signal, 'SIGKILL', 9, create=True):
            system.name = 'posix'
            delegate.kill_tree(proc)
            system.killpg.assert_called_once_with(proc.pid, delegate.signal.SIGKILL)
        proc.kill.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX process group integration; run by Linux/macOS CI')
    def test_timeout_stops_descendant_work(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'unexpected-child-output'
            child_code = 'import time,pathlib;time.sleep(1.5);pathlib.Path(' + repr(str(marker)) + ').write_text("alive")'
            parent_code = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",' + repr(child_code) + ']);time.sleep(10)'
            code, _, _, timed_out = delegate.run_blocking([sys.executable, '-c', parent_code], folder, .4)
            self.assertTrue(timed_out)
            self.assertEqual(code, -1)
            time.sleep(1.7)
            self.assertFalse(marker.exists(), 'Descendant survived timeout and continued changing files')

    @unittest.skipUnless(shutil.which('node'), 'Node is needed only for npm launcher tests')
    def test_node_launcher_regressions(self):
        proc = subprocess.run(['node', str(ROOT / 'tests/test_launcher.cjs')],
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed only for npm launcher tests')
    def test_actual_launcher_handshake_is_pure_jsonrpc(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'release-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}]
        proc = subprocess.run(['node', str(ROOT / 'bin/opencode-delegate.js')],
            input=''.join(json.dumps(message) + '\n' for message in messages),
            env=runtime.child_env() | {'OC_DELEGATE_PYTHON': sys.executable},
            capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]['result']['serverInfo']['version'], '0.4.1')
        self.assertEqual(len(replies[1]['result']['tools']), 12)


if __name__ == '__main__':
    unittest.main()
