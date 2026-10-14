"""MCP regressions: actual startup receipts, prompts, liveness and concurrent stdio."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import mcp_server
import runtime
import delegate


class MCPTest(unittest.TestCase):
    def setUp(self):
        # Viewer lifecycle cases below mock the Windows implementation on every CI host.
        viewer_patch = mock.patch.object(runtime, 'VIEWER_SUPPORTED', True)
        viewer_patch.start()
        self.addCleanup(viewer_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / '中文 workspace'
        self.workspace.mkdir()
        self.state = self.base / 'state'
        self.state.mkdir()
        patch = mock.patch.object(runtime, 'STATE_ROOT', self.state)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, OC_DELEGATE_OPENCODE=str(ROOT / 'tests/fake_opencode.py'),
                              FAKE_HONOR_SESSION='1')
        env.start()
        self.addCleanup(env.stop)

    def args(self, task='one', **extra):
        return dict(task=task, workspace=str(self.workspace), prompt='Do one thing', watch=False, **extra)

    def finish(self, task):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = runtime.status(task)
            if snapshot['state'] != 'running':
                # Terminal state may precede the worker releasing its lock by a few ms.
                while (self.state / (task + '.lock')).exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                return snapshot
            time.sleep(.02)
        self.fail('Worker never finished')

    def test_status_reads_persisted_activity_and_liveness(self):
        delegate.write_json(self.state / 'one.json', dict(task='one', status='running',
            current_event='tool', last_tool='shell:running', event_count=8, elapsed_seconds=9.1,
            heartbeat_at=delegate.utc_now(), last_event_at=delegate.utc_now(), wrapper_pid=os.getpid()))
        result = runtime.status('one')
        self.assertEqual(result['events'], 8)
        self.assertEqual(result['elapsed_seconds'], 9.1)
        self.assertEqual(result['last_tool'], 'shell:running')
        self.assertTrue(result['liveness']['alive'])

    def test_bad_task_and_parameters_do_not_write_files(self):
        for task in ('../bad', 'CON', '/absolute', 'x/y'):
            with self.assertRaises(ValueError):
                mcp_server._call_tool('oc_run', self.args(task))
        for args in ({'task':'one', 'tail_lines':-1}, {'task':'one','tail_lines':True}):
            with self.assertRaises(ValueError):
                mcp_server._call_tool('oc_read', args)
        with self.assertRaises(ValueError):
            mcp_server._call_tool('oc_service', {'action':'delete'})
        self.assertEqual(list(self.state.iterdir()), [])

    def test_detached_worker_does_not_inherit_mcp_stdin(self):
        folder = self.state / 'startup-test'
        folder.mkdir()
        prompt = folder / 'prompt.txt'
        prompt.write_text('test')
        with mock.patch.object(runtime.subprocess, 'Popen') as popen, \
             mock.patch.object(runtime, 'reap_later', side_effect=lambda proc: proc):
            runtime.spawn('run','one',str(self.workspace),runtime.DEFAULT_MODEL,prompt,
                          folder / 'startup.json','test-dispatch')
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_executor_receipt_and_immutable_prompts(self):
        first = mcp_server._call_tool('oc_run', self.args())
        self.assertIs(first['accepted'], True)
        self.assertEqual(self.finish('one')['state'], 'completed', runtime.read_state('one'))
        second = mcp_server._call_tool('oc_continue', {'task':'one','prompt':'Rework exactly this', 'watch':False})
        self.assertIs(second['accepted'], True)
        self.assertEqual(second['turn'], 2)
        self.finish('one')
        files = sorted((self.state / 'tasks/one').glob('*/prompt.txt'))
        self.assertEqual({p.read_text() for p in files}, {'Do one thing','Rework exactly this'})
        self.assertNotEqual(first['dispatch_id'], second['dispatch_id'])

    def test_executor_rejection_is_not_reported_as_accepted(self):
        # Preflight passes; a direct CLI writer acquired the authoritative lock first.
        delegate.write_lock(self.state / 'one.lock')
        with self.assertRaisesRegex(RuntimeError, 'locked|lock'):
            mcp_server._call_tool('oc_run', self.args())
        self.assertFalse((self.state / 'one.json').exists())

    def test_continue_failed_state_rejected_before_prompt_write(self):
        delegate.write_json(self.state / 'one.json', dict(task='one', workspace=str(self.workspace),
            model=runtime.DEFAULT_MODEL, session_id='ses_one', status='needs_review', backend={'kind':'service'}))
        with self.assertRaises(ValueError):
            mcp_server._call_tool('oc_continue', {'task':'one','prompt':'wrong','watch':False})
        self.assertFalse((self.state / 'tasks').exists())
        with self.assertRaises(ValueError):
            mcp_server._call_tool('oc_recover', {'task':'one','prompt':'wrong','acknowledge_inspected':True,'watch':False})

    def test_reserved_id_is_not_session_readiness(self):
        delegate.write_json(self.state / 'one.json', dict(task='one', workspace=str(self.workspace), status='running', session_id='ses_reserved'))
        proc = mock.Mock()
        proc.poll.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'no matching session'):
            runtime.wait_for_session('one', proc, .01)
        with self.assertRaisesRegex(RuntimeError, 'not been observed'):
            runtime.watch('one')

    def test_viewer_failure_does_not_reject_accepted_worker(self):
        with mock.patch.object(runtime, 'watch', side_effect=RuntimeError('viewer unavailable')):
            response = mcp_server._call_tool('oc_run', self.args() | {'watch':True})
        self.assertTrue(response['accepted'])
        self.assertFalse(response['watch']['ok'])
        self.assertEqual(self.finish('one')['state'], 'completed')

    def test_explicit_unsupported_watch_does_not_wait_or_spawn_viewer(self):
        with mock.patch.object(runtime, 'VIEWER_SUPPORTED', False), \
             mock.patch.object(runtime, 'wait_for_session') as wait:
            response = mcp_server._call_tool('oc_run', self.args() | {'watch': True})
        self.assertTrue(response['accepted'])
        self.assertFalse(response['watch']['ok'])
        self.assertIsNone(response['watch']['retry'])
        wait.assert_not_called()
        self.finish('one')

    def test_two_concurrent_dispatches_one_wins_without_prompt_overwrite(self):
        outputs = []
        def run(prompt):
            try: outputs.append(mcp_server._call_tool('oc_run', self.args() | {'prompt':prompt}))
            except Exception as exc: outputs.append(exc)
        threads = [threading.Thread(target=run, args=(value,)) for value in ('first','second')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(15)
        self.assertEqual(sum(isinstance(o, dict) and o['accepted'] for o in outputs), 1)
        self.finish('one')
        files = list((self.state / 'tasks/one').glob('*/prompt.txt'))
        self.assertEqual(len(files), 1)

    def test_watch_selects_tab_without_spawning_or_killing(self):
        state = dict(task='one', workspace=str(self.workspace), session_id='ses_one', observed_session_id='ses_one')
        delegate.write_json(self.state / 'one.json', state)
        registry, bridge = runtime.viewer_files(str(self.workspace))
        registry.parent.mkdir()
        record = dict(pid=1234, identity={'pid':1234}, token='test', workspace=str(self.workspace))
        delegate.write_json(registry, record)
        with mock.patch.object(runtime, 'owned_viewer', return_value=True), \
             mock.patch.object(runtime, 'switch_tab', return_value={'active_session':'ses_one'}) as switch, \
             mock.patch.object(runtime.subprocess, 'Popen') as spawn, \
             mock.patch.object(runtime, 'close_viewer') as close:
            response = runtime.watch('one')
        self.assertEqual(response['action'], 'selected_tab')
        self.assertEqual(response['pid'], 1234)
        switch.assert_called_once()
        spawn.assert_not_called()
        close.assert_not_called()

    def test_recycled_pid_cannot_be_killed(self):
        with mock.patch.object(runtime, 'process_identity', return_value={'pid':123,'created':'new'}), \
             mock.patch.object(runtime.subprocess, 'run') as kill:
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                runtime.close_viewer({'pid':123, 'identity':{'pid':123,'created':'old'}})
        kill.assert_not_called()

    def test_tab_failure_keeps_existing_window(self):
        delegate.write_json(self.state / 'one.json', dict(task='one', workspace=str(self.workspace),
            session_id='ses_one', observed_session_id='ses_one'))
        registry, _ = runtime.viewer_files(str(self.workspace))
        registry.parent.mkdir()
        delegate.write_json(registry, dict(pid=1234, identity={'pid':1234}, token='test', workspace=str(self.workspace)))
        with mock.patch.object(runtime, 'owned_viewer', return_value=True), \
             mock.patch.object(runtime, 'switch_tab', side_effect=RuntimeError('No acknowledgement')), \
             mock.patch.object(runtime.subprocess, 'Popen') as spawn, \
             mock.patch.object(runtime, 'close_viewer') as close:
            with self.assertRaisesRegex(RuntimeError, 'acknowledgement'):
                runtime.watch('one')
        spawn.assert_not_called()
        close.assert_not_called()
        self.assertTrue(registry.exists())

    def test_os_lock_excludes_another_process(self):
        lock = self.state / 'locks/workspace.lock'
        env = runtime.child_env()
        code = 'import sys; sys.path.insert(0,sys.argv[1]); import runtime;\nwith runtime.file_lock(__import__("pathlib").Path(sys.argv[2]), timeout=.2): print("unexpected")'
        with runtime.file_lock(lock):
            proc = subprocess.run([sys.executable,'-B','-c',code,str(ROOT / 'scripts'),str(lock)],
                env=env, capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('owns this task/workspace', proc.stderr)
        with runtime.file_lock(lock, timeout=.1):
            pass

    def test_unwatch_old_task_closes_current_workspace_viewer(self):
        delegate.write_json(self.state / 'one.json', {'task':'one','workspace':str(self.workspace)})
        registry, _ = runtime.viewer_files(str(self.workspace))
        registry.parent.mkdir()
        delegate.write_json(registry, {'task':'another-task','pid':123})
        with mock.patch.object(runtime, 'owned_viewer', return_value=True), \
             mock.patch.object(runtime, 'close_viewer') as close:
            self.assertEqual(runtime.unwatch('one')['action'], 'closed')
        close.assert_called_once()
        self.assertFalse(registry.exists())

    def test_wait_does_not_wake_for_heartbeat_alone(self):
        path = self.state / 'one.json'
        record = dict(task='one', status='running', event_count=1, heartbeat_at=delegate.utc_now(), wrapper_pid=os.getpid())
        delegate.write_json(path, record)
        revision = runtime.status('one')['revision']
        delegate.write_json(path, record | {'heartbeat_at':delegate.utc_now(), 'elapsed_seconds':123})
        result = runtime.wait('one', revision, .05)
        self.assertTrue(result['timed_out'])
        delegate.write_json(path, record | {'event_count':2})
        self.assertTrue(runtime.wait('one', revision, 1)['changed'])

    def test_stdio_ping_remains_responsive_during_wait(self):
        delegate.write_json(self.state / 'one.json', dict(task='one',status='running', event_count=1,
            heartbeat_at=delegate.utc_now(), wrapper_pid=os.getpid()))
        revision = runtime.status('one')['revision']
        env = runtime.child_env() | {'OC_DELEGATE_STATE':str(self.state)}
        proc = subprocess.Popen([sys.executable,'-B',str(ROOT / 'mcp_server.py')], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        responses = queue.Queue()
        def reader():
            for line in proc.stdout: responses.put(json.loads(line))
        threading.Thread(target=reader, daemon=True).start()
        def send(data):
            proc.stdin.write(json.dumps(data) + '\n'); proc.stdin.flush()
        send({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'oc_wait',
            'arguments':{'task':'one','after_revision':revision,'timeout_seconds':2}}})
        send({'jsonrpc':'2.0','id':2,'method':'ping'})
        self.assertEqual(responses.get(timeout=1)['id'], 2)
        self.assertEqual(responses.get(timeout=4)['id'], 1)
        proc.stdin.close()
        proc.wait(timeout=3)
        proc.stdout.close()
        proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
