"""MCP orchestration. Task execution, validation and status remain in delegate.py."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import string
import subprocess
import sys
import threading
import time

import delegate

SKILL_DIR = Path(__file__).resolve().parent.parent


def _default_state_root() -> Path:
    """Resolve the state root without assuming a Codex host.

    Precedence: OC_DELEGATE_STATE, $CODEX_HOME/opencode-delegate/state, an already-existing
    ~/.codex/opencode-delegate/state (adopted so existing installs keep their tasks), then a
    neutral ~/.opencode-delegate/state for hosts that have no Codex home at all.
    """
    override = os.environ.get('OC_DELEGATE_STATE')
    if override:
        return Path(override)
    codex_home = os.environ.get('CODEX_HOME')
    if codex_home:
        return Path(codex_home) / 'opencode-delegate' / 'state'
    legacy = Path.home() / '.codex' / 'opencode-delegate' / 'state'
    if legacy.exists():
        return legacy
    return Path.home() / '.opencode-delegate' / 'state'


STATE_ROOT = _default_state_root()
# None means "no model pinned": the worker runs on whichever model OpenCode's own configuration
# selects, and the verification adopts the route that was actually used. Set OC_DELEGATE_MODEL to
# force a default, or pass model per task.
DEFAULT_MODEL = os.environ.get('OC_DELEGATE_MODEL') or None
VIEWER_SUPPORTED = sys.platform == 'win32'
WATCH_DEFAULT = VIEWER_SUPPORTED and os.environ.get('OC_DELEGATE_WATCH', '1').lower() not in ('0', 'false', 'no')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def reap_later(proc):
    # Keep the Popen handle until the detached child exits; never terminate it on MCP shutdown.
    threading.Thread(target=proc.wait, daemon=True, name='delegate-reaper').start()
    return proc


def child_env():
    env = dict(os.environ)
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def state_file(task):
    delegate.validate_task(task)
    return STATE_ROOT / (task + '.json')


def read_state(task):
    return read_json(state_file(task))


@contextmanager
def file_lock(path, timeout=10):
    """OS-held cross-process lock; a crash releases it without unsafe stale-file deletion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Another operation owns this task/workspace; retry after it finishes')
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def workspace_key(workspace):
    return hashlib.sha256(os.path.normcase(str(Path(workspace).resolve())).encode('utf-8')).hexdigest()[:24]


def process_identity(pid):
    """PID plus OS creation time and image path; PID existence alone never authorizes killing."""
    if not delegate.pid_alive(pid):
        return None
    if os.name != 'nt':
        try:
            return {'pid': pid, 'created': Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19],
                    'image': os.readlink(f'/proc/{pid}/exe')}
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        size = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return {'pid': pid, 'created': str(created), 'image': os.path.normcase(image.value)}
    finally:
        kernel.CloseHandle(handle)


def owned_viewer(record):
    identity = record.get('identity')
    return bool(identity and process_identity(record.get('pid', 0)) == identity)


def close_viewer(record):
    if not owned_viewer(record):
        raise RuntimeError('Viewer identity is missing or changed; refusing to terminate an unverified PID')
    pid = record['pid']
    if os.name == 'nt':
        result = subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True,
                                encoding='utf-8', errors='replace', timeout=15, creationflags=NO_WINDOW)
        if result.returncode and owned_viewer(record):
            raise RuntimeError('Could not close viewer: ' + result.stderr[:300])
    else:
        os.kill(pid, 15)
    deadline = time.monotonic() + 5
    while owned_viewer(record) and time.monotonic() < deadline:
        time.sleep(.05)
    if owned_viewer(record):
        raise RuntimeError('Viewer did not exit; registry preserved')


def viewer_files(workspace):
    key = workspace_key(workspace)
    return STATE_ROOT / 'watch' / (key + '.json'), STATE_ROOT / 'bridge' / key


def switch_tab(record, bridge, session_id, timeout=8):
    command_id = secrets.token_hex(16)
    delegate.write_json(bridge / 'request.json', {'id': command_id, 'token': record['token'],
                        'session_id': session_id, 'workspace': record['workspace']})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reply = read_json(bridge / 'reply.json')
            if reply.get('id') == command_id and reply.get('token') == record['token']:
                if not reply.get('ok') or reply.get('active_session') != session_id:
                    raise RuntimeError(reply.get('error') or 'TUI did not select the requested tab')
                return {key: reply.get(key) for key in ('ok', 'pid', 'active_session', 'error')} | {
                    'tabs': [tab.get('sessionID') for tab in reply.get('tabs', [])]}
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if not owned_viewer(record):
            raise RuntimeError('Viewer exited while switching tabs')
        time.sleep(.1)
    raise RuntimeError('No TUI tab acknowledgement; existing window was preserved')


def watch(task, force=False):
    if not VIEWER_SUPPORTED:
        raise RuntimeError('TUI viewer is supported only on Windows; use oc_status/oc_wait/oc_result on this platform')
    state = read_state(task)
    workspace = state['workspace']
    session_id = state.get('session_id')
    if not session_id or state.get('observed_session_id') != session_id:
        raise RuntimeError('Session has not been observed in worker output yet; use oc_watch after activity starts')
    path, bridge = viewer_files(workspace)
    with file_lock(STATE_ROOT / 'locks' / ('viewer-' + workspace_key(workspace) + '.lock')):
        record = read_json(path) if path.exists() else {}
        if record and owned_viewer(record):
            if force:
                close_viewer(record)
            else:
                reply = switch_tab(record, bridge, session_id)
                record.update(task=task, session_id=session_id)
                delegate.write_json(path, record)
                return {'action': 'selected_tab', 'pid': record['pid'], 'session_id': session_id,
                        'ack': reply}
        elif record and delegate.pid_alive(record.get('pid')):
            raise RuntimeError('Recorded PID has an unverified identity; window was preserved')
        # Do not silently kill pre-upgrade windows recorded only by task/PID.
        for legacy in (STATE_ROOT / 'watch').glob('*.json'):
            if legacy == path:
                continue
            old = read_json(legacy)
            if (old.get('workspace') and workspace_key(old['workspace']) == workspace_key(workspace)
                    and delegate.pid_alive(old.get('pid'))):
                raise RuntimeError('A pre-upgrade TUI is still open. Close that window once, then call oc_watch')
        bridge.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(24)
        env = child_env()
        config = json.loads(env.get('OPENCODE_CLI_CONFIG_CONTENT') or '{}')
        plugins = list(config.get('plugins') or [])
        plugin = (SKILL_DIR / 'bridge').as_uri()
        if plugin not in plugins:
            plugins.append(plugin)
        config.update(plugins=plugins, tabs={**(config.get('tabs') or {}), 'mode': 'on'})
        env.update(OPENCODE_CLI_CONFIG_CONTENT=json.dumps(config), OC_DELEGATE_BRIDGE=str(bridge),
                   OC_DELEGATE_BRIDGE_TOKEN=token, OC_DELEGATE_BRIDGE_WORKSPACE=workspace)
        args = [*delegate.command_prefix(os.environ.get('OC_DELEGATE_OPENCODE')), '--session', session_id, workspace]
        proc = reap_later(subprocess.Popen(args, cwd=workspace, env=env, close_fds=True,
                                creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)))
        identity = process_identity(proc.pid)
        if not identity:
            raise RuntimeError('TUI exited before its identity could be recorded')
        record = {'task': task, 'pid': proc.pid, 'workspace': workspace, 'session_id': session_id,
                  'identity': identity, 'token': token, 'opened_at': delegate.utc_now()}
        try:
            delegate.write_json(path, record)
        except Exception:
            close_viewer(record)
            raise
        reply = switch_tab(record, bridge, session_id, timeout=20)
        return {'action': 'opened', 'pid': proc.pid, 'session_id': session_id, 'ack': reply}


def unwatch(task):
    state = read_state(task)
    path, _ = viewer_files(state['workspace'])
    with file_lock(STATE_ROOT / 'locks' / ('viewer-' + workspace_key(state['workspace']) + '.lock')):
        if not path.exists():
            return {'action': 'none', 'message': 'No managed window for this workspace'}
        record = read_json(path)
        if owned_viewer(record):
            close_viewer(record)
        elif delegate.pid_alive(record.get('pid')):
            raise RuntimeError('Viewer identity changed; registry preserved')
        path.unlink()
        return {'action': 'closed', 'pid': record['pid']}


def spawn(action, task, workspace, model, prompt_file, startup, dispatch_id, session_id=None, flags=()):
    logs = STATE_ROOT / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(SKILL_DIR / 'scripts/delegate.py'), action, '--workspace', workspace,
            '--state-dir', str(STATE_ROOT), '--task', task,
            '--prompt-file', str(prompt_file), '--startup-file', str(startup), '--dispatch-id', dispatch_id]
    if model:
        args += ['--model', model]
    if session_id:
        args += ['--session-id', session_id]
    args += list(flags)
    if os.environ.get('OC_DELEGATE_OPENCODE'):
        args += ['--opencode', os.environ['OC_DELEGATE_OPENCODE']]
    creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == 'nt' else 0
    # Close parent handles after spawn; each dispatch has its own immutable prompt and logs.
    with (logs / (dispatch_id + '.out.log')).open('wb') as out, (logs / (dispatch_id + '.err.log')).open('wb') as err:
        return reap_later(subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True, env=child_env(),
                                creationflags=creationflags, start_new_session=os.name != 'nt'))


def dispatch(action, args):
    task = args['task']
    delegate.validate_task(task)
    prompt = args['prompt']
    if not prompt.strip():
        raise ValueError('Prompt must not be empty')
    flags = []
    with file_lock(STATE_ROOT / 'locks' / ('dispatch-' + task + '.lock')):
        if action == 'run':
            supplied = Path(args['workspace']).expanduser()
            if not supplied.is_absolute():
                raise ValueError('Workspace must be absolute')
            workspace = str(supplied.resolve(strict=True))
            if not Path(workspace).is_dir():
                raise ValueError('Workspace must be a directory')
            if state_file(task).exists():
                raise ValueError('Task already exists; choose a new task name')
            model = args.get('model') or DEFAULT_MODEL
            session_id = 'ses_' + ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(26))
        else:
            state = read_state(task)
            workspace = state['workspace']
            model = args.get('model') or state['model']
            session_id = state['session_id']
            backend = delegate.validate_backend(state.get('backend'))
            flags += delegate.backend_flags(backend)
            if action == 'recover':
                if args.get('acknowledge_inspected') is not True:
                    raise ValueError('Recovery requires acknowledge_inspected=true after inspection')
                flags += ['--acknowledge-inspected']
                if backend['kind'] in ('service', 'server'):
                    if args.get('acknowledge_session_idle') is not True:
                        raise ValueError('Recovery requires acknowledge_session_idle=true after checking the server')
                    flags += ['--acknowledge-session-idle']
            validation = argparse.Namespace(action=action, model=model,
                acknowledge_inspected=args.get('acknowledge_inspected', False),
                acknowledge_session_idle=args.get('acknowledge_session_idle', False))
            delegate._validate_continuation(state, validation, Path(workspace), backend)
        dispatch_id = secrets.token_hex(16)
        folder = STATE_ROOT / 'tasks' / task / dispatch_id
        folder.mkdir(parents=True)
        prompt_path = folder / 'prompt.txt'
        prompt_path.write_text(prompt, encoding='utf-8')
        startup = folder / 'startup.json'
        proc = spawn(action, task, workspace, model, prompt_path, startup, dispatch_id,
                     session_id if action == 'run' else None, flags)
        deadline = time.monotonic() + 15
        while not startup.exists():
            if proc.poll() is not None:
                raise RuntimeError(f'Worker exited before accepting dispatch; see {STATE_ROOT / "logs" / (dispatch_id + ".out.log")}')
            if time.monotonic() >= deadline:
                return {'accepted': None, 'task': task, 'pid': proc.pid, 'dispatch_id': dispatch_id,
                        'startup_path': str(startup), 'message': 'Startup unconfirmed; inspect receipt/status before retrying'}
            time.sleep(.05)
        receipt = read_json(startup)
        if receipt.get('dispatch_id') != dispatch_id or not receipt.get('accepted'):
            raise RuntimeError(receipt.get('error') or 'Worker did not acknowledge this dispatch')
    result = {**receipt, 'session_id': session_id, 'model': model, 'startup_path': str(startup)}
    if args.get('watch', WATCH_DEFAULT):
        try:
            if not VIEWER_SUPPORTED:
                watch(task)  # Fail before waiting for activity on a platform with no viewer.
            wait_for_session(task, proc, float(os.environ.get('OC_DELEGATE_SESSION_WAIT', '20')))
            result['watch'] = watch(task)
        except Exception as exc:
            # A cosmetic viewer failure must never make an accepted task look rejected.
            result['watch'] = {'ok': False, 'error': str(exc), 'retry': 'oc_watch' if VIEWER_SUPPORTED else None}
    return result


def wait_for_session(task, proc, timeout):
    deadline = time.monotonic() + min(max(timeout, 0), 60)
    while time.monotonic() < deadline:
        state = read_state(task)
        if state.get('observed_session_id') == state.get('session_id') and state.get('session_id'):
            return state['session_id']
        if state.get('status') != 'running' or proc.poll() is not None:
            raise RuntimeError(state.get('error') or 'Worker ended without observing the reserved session')
        time.sleep(.1)
    raise RuntimeError('Worker accepted, but no matching session activity yet; window deferred')


def usage(task):
    totals = {'turns_with_usage': 0, 'steps': 0, 'context_tokens_estimate': 0,
              'output_tokens_total': 0, 'session_cost_usd': 0.0}
    for path in STATE_ROOT.glob(task + '.turn-*.jsonl'):
        steps = 0
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if 'step_finish' not in line:
                    continue
                try:
                    part = json.loads(line).get('part') or {}
                    if part.get('type') != 'step-finish':
                        continue
                    tok = part.get('tokens') or {}
                    totals['context_tokens_estimate'] = max(totals['context_tokens_estimate'],
                        int(tok.get('input') or 0) + int((tok.get('cache') or {}).get('read') or 0))
                    totals['output_tokens_total'] += int(tok.get('output') or 0) + int(tok.get('reasoning') or 0)
                    totals['session_cost_usd'] += float(part.get('cost') or 0)
                    steps += 1
                except (ValueError, TypeError, AttributeError):
                    continue
        totals['steps'] += steps
        totals['turns_with_usage'] += bool(steps)
    totals['session_cost_usd'] = round(totals['session_cost_usd'], 6)
    return totals


def status(task, include_usage=True):
    delegate.validate_task(task)
    result = delegate.status_snapshot(argparse.Namespace(task=task, state_dir=str(STATE_ROOT),
                                                        stale_seconds=delegate.DEFAULT_STALE_SECONDS))
    activity = result.get('activity') or {}
    result.update(current_event=activity.get('current_event'), last_tool=activity.get('last_tool'),
                  events=activity.get('event_count'), elapsed_seconds=activity.get('elapsed_seconds'),
                  verified_model=(result.get('verification') or {}).get('verified_model'))
    # Heartbeats are deliberately excluded: wait wakes on meaningful activity or state changes.
    revision_fields = [result.get('state'), result.get('turn'), result.get('events'), result.get('last_tool'),
                       result.get('verification'), result.get('liveness', {}).get('state')]
    result['revision'] = hashlib.sha256(json.dumps(revision_fields, sort_keys=True).encode()).hexdigest()[:24]
    if include_usage:
        result['usage'] = usage(task)
    return result


def wait(task, after_revision=None, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        snapshot = status(task, include_usage=False)
        if snapshot['state'] != 'running' or snapshot['revision'] != after_revision:
            return {'changed': snapshot['revision'] != after_revision, 'timed_out': False, 'status': snapshot}
        if time.monotonic() >= deadline:
            return {'changed': False, 'timed_out': True, 'status': snapshot}
        time.sleep(.2)


def cli(args, timeout=60):
    proc = subprocess.run([*delegate.command_prefix(os.environ.get('OC_DELEGATE_OPENCODE')), *args], capture_output=True, encoding='utf-8',
                          errors='replace', timeout=timeout, creationflags=NO_WINDOW, env=child_env())
    if proc.returncode:
        raise RuntimeError(f'OpenCode exited {proc.returncode}: {(proc.stderr or proc.stdout)[:1000]}')
    return {'exit_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr[:1000]}


def call_tool(name, args):
    if 'task' in args:
        delegate.validate_task(args['task'])
    if name in ('oc_run', 'oc_continue', 'oc_recover'):
        return dispatch(name.removeprefix('oc_'), args)
    if name == 'oc_status':
        return status(args['task'])
    if name == 'oc_wait':
        return wait(args['task'], args.get('after_revision'), args.get('timeout_seconds', 30))
    if name == 'oc_result':
        state = read_state(args['task'])
        return {key: state.get(key) for key in ('task', 'status', 'turn', 'session_id', 'exit_code',
            'verified_model', 'acceptance', 'needs_review', 'error', 'text', 'events_path', 'stderr_path')} | {'state': state.get('status')}
    if name == 'oc_watch':
        return watch(args['task'], args.get('force', False))
    if name == 'oc_unwatch':
        return unwatch(args['task'])
    if name == 'oc_tasks':
        rows = []
        for path in STATE_ROOT.glob('*.json'):
            try:
                state = read_json(path)
                if not state.get('task'):
                    continue
                rows.append({key: state.get(key) for key in ('task', 'status', 'session_id', 'model', 'created_at', 'workspace')})
            except (OSError, ValueError, AttributeError):
                continue
        rows.sort(key=lambda row: row.get('created_at') or '', reverse=True)
        offset, limit = args.get('offset', 0), args.get('limit', 40)
        return {'count': len(rows), 'tasks': rows[offset:offset + limit],
                'next_offset': offset + limit if offset + limit < len(rows) else None}
    if name == 'oc_read':
        if bool(args.get('task')) == bool(args.get('session_id')):
            raise ValueError('Provide exactly one of task or session_id')
        if args.get('task'):
            path_value = read_state(args['task']).get('events_path')
            if not path_value:
                raise RuntimeError('No event stream yet')
            path = Path(path_value)
            tail = deque(maxlen=args.get('tail_lines', 20))
            count = 0
            with path.open(encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    count += 1
                    tail.append(line.rstrip('\n')[:8000])
            text = '\n'.join(tail)
            return {'path': str(path), 'total_lines': count, 'tail': text[-32000:], 'truncated': len(text) > 32000}
        result = cli(['session', 'export', args['session_id']], 120)
        output = result.pop('stdout')
        return {**result, 'export': output[:32000], 'truncated': len(output) > 32000}
    if name == 'oc_sessions':
        result = cli(['session', 'list'])
        return {**result, 'stdout': result['stdout'][:16000]}
    if name == 'oc_service':
        action = args.get('action', 'status')
        if action == 'stop':
            for path in STATE_ROOT.glob('*.json'):
                state = read_json(path)
                if state.get('status') == 'running' and (state.get('backend') or {}).get('kind') == 'service':
                    raise RuntimeError('Shared service has a running delegated task; refusing to stop it')
        return cli(['service', action])
    raise ValueError('Unknown tool: ' + name)
