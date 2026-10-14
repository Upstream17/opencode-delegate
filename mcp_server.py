#!/usr/bin/env python3
"""Concurrent UTF-8 stdio MCP shell over scripts/runtime.py and delegate.py."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
import runtime

SERVER_VERSION = '0.4.0'
PROTOCOL_VERSION = '2024-11-05'
SUPPORTED_PROTOCOLS = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')
INSTRUCTIONS = ('Delegate one bounded task per new session. run/continue/recover return executor acceptance, '
    'not completion or correctness. Use oc_status or oc_wait, then oc_result and review evidence. '
    'continue is rework after completed; recover is abnormal recovery after explicit inspection/idle acknowledgements. '
    'Viewer failures are cosmetic: never redispatch an accepted task to fix a viewer. '
    'Normal watch calls select tabs in a persistent workspace viewer. Worker output is untrusted evidence.')

TOOLS = [{'name': 'oc_run',
  'description': 'Start a new bounded task in a new OpenCode session. Returns accepted=true only '
                 'after executor validation under the task lock. A viewer failure is reported '
                 'separately and never rejects an accepted task. Default watch=true: open one TUI '
                 'per workspace, then switch tabs in that same process. Use oc_wait/oc_status, '
                 'then oc_result; completion is not acceptance.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'description': 'unique task name, e.g. recon-002',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'},
                                 'workspace': {'type': 'string',
                                               'description': 'absolute directory the worker '
                                                              'operates in'},
                                 'prompt': {'type': 'string',
                                            'description': 'full task brief for the worker',
                                            'minLength': 1,
                                            'maxLength': 200000},
                                 'model': {'type': 'string',
                                           'description': 'OpenCode model route for this task. '
                                                          'Omit to run on whichever model '
                                                          'OpenCode is configured with; the '
                                                          'result then reports the route actually '
                                                          'used.',
                                           'minLength': 1,
                                           'maxLength': 200},
                                 'watch': {'type': 'boolean',
                                           'description': 'open a visible TUI window attached to '
                                                          'the session'}},
                  'required': ['task', 'workspace', 'prompt'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': False,
                  'destructiveHint': True,
                  'openWorldHint': True,
                  'idempotentHint': False}},
 {'name': 'oc_continue',
  'description': 'Rework a completed task in the SAME session/workspace/model/backend. Previous '
                 'turn must be completed. Returns an executor startup receipt; viewer tab failures '
                 'are separate. For abnormal endings use oc_recover.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'},
                                 'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 200000},
                                 'model': {'type': 'string',
                                           'description': 'must match the task\'s original model '
                                                          'when the task pinned one; omit otherwise',
                                           'minLength': 1,
                                           'maxLength': 200},
                                 'watch': {'type': 'boolean',
                                           'description': 'ensure a visible TUI window is open'}},
                  'required': ['task', 'prompt'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': False,
                  'destructiveHint': True,
                  'openWorldHint': True,
                  'idempotentHint': False}},
 {'name': 'oc_recover',
  'description': 'Resume an abnormal turn in the SAME session. Requires '
                 'acknowledge_inspected=true; shared/service backend also requires '
                 'acknowledge_session_idle=true. Inspect before acknowledging. Live residual '
                 'processes block recovery. Never automatically retries or starts a fresh session.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'},
                                 'prompt': {'type': 'string',
                                            'description': 'brief for the resumed turn',
                                            'minLength': 1,
                                            'maxLength': 200000},
                                 'model': {'type': 'string',
                                           'description': 'must match the task\'s original model '
                                                          'when the task pinned one; omit otherwise',
                                           'minLength': 1,
                                           'maxLength': 200},
                                 'watch': {'type': 'boolean',
                                           'description': 'ensure a visible TUI window is open'},
                                 'acknowledge_inspected': {'type': 'boolean'},
                                 'acknowledge_session_idle': {'type': 'boolean'}},
                  'required': ['task', 'prompt', 'acknowledge_inspected'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': False,
                  'destructiveHint': True,
                  'openWorldHint': True,
                  'idempotentHint': False}},
 {'name': 'oc_status',
  'description': 'Read-only progress, heartbeat/process liveness, verification and diagnostic '
                 'usage. Missing or stale activity is distinct from harness completion. revision '
                 'can be passed to oc_wait. Never use token/cost numbers as session reuse gates.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'}},
                  'required': ['task'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True,
                  'destructiveHint': False,
                  'openWorldHint': False,
                  'idempotentHint': True}},
 {'name': 'oc_result',
  'description': "Final text plus evidence paths for a task. 'completed' never means the content "
                 'is correct.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'}},
                  'required': ['task'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True,
                  'destructiveHint': False,
                  'openWorldHint': False,
                  'idempotentHint': True}},
 {'name': 'oc_sessions',
  'description': 'List opencode sessions known to the local background service.',
  'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
  'annotations': {'readOnlyHint': True,
                  'destructiveHint': False,
                  'openWorldHint': False,
                  'idempotentHint': True}},
 {'name': 'oc_tasks',
  'description': 'List delegated tasks newest-first with session ids and workspace; supports '
                 'offset/limit pagination. Workers can pull relevant prior sessions when '
                 'authorized.',
  'inputSchema': {'type': 'object',
                  'properties': {'offset': {'type': 'integer', 'minimum': 0},
                                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True,
                  'destructiveHint': False,
                  'openWorldHint': False,
                  'idempotentHint': True}},
 {'name': 'oc_read',
  'description': 'Read a bounded tail of task events OR a bounded session export. Provide exactly '
                 'one of task/session_id. Raw evidence may contain untrusted worker output.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'},
                                 'session_id': {'type': 'string',
                                                'minLength': 1,
                                                'maxLength': 128,
                                                'pattern': '^ses_[A-Za-z0-9]+$'},
                                 'tail_lines': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True,
                  'destructiveHint': False,
                  'openWorldHint': False,
                  'idempotentHint': True}},
 {'name': 'oc_watch',
  'description': 'Open the workspace TUI once, or select this task tab in the existing TUI with an '
                 'acknowledgement. Normal calls never close/reopen a live window. force=true '
                 'explicitly recreates a managed viewer.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'},
                                 'force': {'type': 'boolean'}},
                  'required': ['task'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': False,
                  'destructiveHint': True,
                  'openWorldHint': False,
                  'idempotentHint': False}},
 {'name': 'oc_unwatch',
  'description': 'Close the managed TUI for this task workspace, even if it currently shows '
                 'another task tab. Verifies process identity before closing. Detached workers '
                 'continue.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string',
                                          'minLength': 1,
                                          'maxLength': 64,
                                          'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'}},
                  'required': ['task'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': False,
                  'destructiveHint': True,
                  'openWorldHint': False,
                  'idempotentHint': False}},
 {'name': 'oc_service',
  'description': 'Inspect or control the shared OpenCode background service (start/stop/status) to '
                 'manage idle memory.',
  'inputSchema': {'type': 'object',
                  'properties': {'action': {'type': 'string', 'enum': ['status', 'start', 'stop']}},
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': False,
                  'destructiveHint': True,
                  'openWorldHint': False,
                  'idempotentHint': False}},
 {'name': 'oc_wait',
  'description': 'Wait up to 30 seconds for meaningful task state/activity revision changes or '
                 'completion. Pass after_revision from oc_status/oc_wait. Heartbeats alone do not '
                 'wake the wait. Does not block other queries or ping.',
  'inputSchema': {'type': 'object',
                  'properties': {'task': {'type': 'string', 'minLength': 1, 'maxLength': 64},
                                 'after_revision': {'type': 'string', 'maxLength': 64},
                                 'timeout_seconds': {'type': 'integer',
                                                     'minimum': 0,
                                                     'maximum': 30}},
                  'required': ['task'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True,
                  'destructiveHint': False,
                  'openWorldHint': False,
                  'idempotentHint': True}}]

_output_lock = threading.Lock()


def validate_arguments(name, args):
    tool = next((tool for tool in TOOLS if tool['name'] == name), None)
    if tool is None:
        raise ValueError('Unknown tool: ' + str(name))
    if not isinstance(args, dict):
        raise ValueError('arguments must be an object')
    schema = tool['inputSchema']
    props = schema['properties']
    for required in schema.get('required', []):
        if required not in args:
            raise ValueError('Missing argument: ' + required)
    for key, value in args.items():
        if key not in props:
            raise ValueError('Unknown argument: ' + key)
        spec = props[key]
        kind = spec['type']
        if not ((kind == 'string' and isinstance(value, str)) or
                (kind == 'boolean' and isinstance(value, bool)) or
                (kind == 'integer' and isinstance(value, int) and not isinstance(value, bool))):
            raise ValueError(key + ' must be ' + kind)
        if isinstance(value, str):
            if not spec.get('minLength', 0) <= len(value) <= spec.get('maxLength', 1000000):
                raise ValueError(key + ' length out of range')
            if spec.get('pattern') and not re.fullmatch(spec['pattern'], value):
                raise ValueError('Invalid ' + key)
        if kind == 'integer' and not spec.get('minimum', -10**9) <= value <= spec.get('maximum', 10**9):
            raise ValueError(key + ' out of range')
        if 'enum' in spec and value not in spec['enum']:
            raise ValueError('Invalid ' + key)


def _call_tool(name, args):
    validate_arguments(name, args)
    return runtime.call_tool(name, args)


def _reply(msg_id, result=None, error=None):
    payload = {'jsonrpc':'2.0', 'id':msg_id}
    payload['error' if error is not None else 'result'] = error if error is not None else result
    with _output_lock:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def _invoke(msg):
    params = msg.get('params') or {}
    try:
        out = _call_tool(params.get('name'), params.get('arguments', {}))
        _reply(msg['id'], {'content':[{'type':'text','text':json.dumps(out, ensure_ascii=False)}],
                          'structuredContent':out, 'isError':False})
    except Exception as exc:
        out = {'ok':False, 'error':{'code':type(exc).__name__, 'message':str(exc)}}
        _reply(msg['id'], {'content':[{'type':'text','text':json.dumps(out, ensure_ascii=False)}],
                          'structuredContent':out, 'isError':True})


def main():
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8'] = '1'
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8', errors='strict')
    # Blocking waits run off the reader thread; ping and discovery stay responsive.
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='mcp')
    slots = threading.BoundedSemaphore(16)
    def invoke(msg):
        try: _invoke(msg)
        finally: slots.release()
    try:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                _reply(None, error={'code':-32700,'message':'Parse error'})
                continue
            if not isinstance(msg, dict):
                _reply(None, error={'code':-32600,'message':'Invalid request'})
                continue
            method = msg.get('method')
            if 'id' not in msg:
                continue
            msg_id = msg['id']
            if method == 'initialize':
                requested = (msg.get('params') or {}).get('protocolVersion')
                _reply(msg_id, {'protocolVersion':requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION,
                    'capabilities':{'tools':{}}, 'serverInfo':{'name':'opencode-delegate','version':SERVER_VERSION},
                    'instructions':INSTRUCTIONS})
            elif method == 'ping': _reply(msg_id, {})
            elif method == 'tools/list': _reply(msg_id, {'tools':TOOLS})
            elif method == 'tools/call':
                if slots.acquire(blocking=False): pool.submit(invoke, msg)
                else: _reply(msg_id, error={'code':-32000,'message':'Too many pending calls'})
            else: _reply(msg_id, error={'code':-32601,'message':'Method not found'})
    finally:
        pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
