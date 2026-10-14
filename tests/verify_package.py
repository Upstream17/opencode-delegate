"""Check the actual npm tarball allowlist and launch it from a clean extracted directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
allowed = set(json.loads((ROOT / 'package.json').read_text())['files']) | {'package.json'}
with tempfile.TemporaryDirectory() as folder:
    with tarfile.open(sys.argv[1], 'r:gz') as archive:
        members = archive.getmembers()
        files = {m.name.removeprefix('package/') for m in members if m.isfile()}
        assert files == allowed, {'unexpected': sorted(files - allowed), 'missing': sorted(allowed - files)}
        for member in members:
            assert not member.issym() and not member.islnk(), 'Unexpected archive link'
            destination = (Path(folder) / member.name).resolve()
            assert destination.is_relative_to(Path(folder).resolve()), 'Unsafe archive path'
        for member in members:
            if member.isfile():
                destination = Path(folder) / member.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream:
                    destination.write_bytes(stream.read())
    message = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'packed-test', 'version': '1'}}}
    env = dict(os.environ, OC_DELEGATE_PYTHON=sys.executable, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    proc = subprocess.run(['node', str(Path(folder) / 'package/bin/opencode-delegate.js')],
        input=json.dumps(message) + '\n', capture_output=True, encoding='utf-8', env=env, timeout=20)
    assert proc.returncode == 0, proc.stderr
    reply = json.loads(proc.stdout)
    assert reply['result']['serverInfo']['version'] == '0.4.1'
    print('PASS: exact npm file allowlist, no caches/state, extracted package handshake')
