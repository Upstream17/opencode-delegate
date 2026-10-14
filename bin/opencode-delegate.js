#!/usr/bin/env node
// Launcher for the Python MCP server, so an MCP client can register this server with no absolute
// paths and nothing to escape:
//
//   Codex : command = 'npx'   args = ['-y', 'opencode-delegate']
//   JSON  : {"command": "npx", "args": ["-y", "opencode-delegate"]}
//
// It only locates a Python 3 interpreter and hands it the bundled mcp_server.py. stdio is inherited
// untouched: nothing here reads, buffers or rewrites the MCP stream.

'use strict'

const { spawn, spawnSync } = require('node:child_process')
const path = require('node:path')

const SERVER = path.join(__dirname, '..', 'mcp_server.py')

// Ordered discovery candidates; OC_DELEGATE_PYTHON overrides the whole list.
const CANDIDATES = process.platform === 'win32'
  ? [['py', ['-3']], ['python', []], ['python3', []]]
  : [['python3', []], ['python', []]]

function resolvePython() {
  const explicit = process.env.OC_DELEGATE_PYTHON
  const candidates = explicit ? [[explicit, []]] : CANDIDATES
  for (const [command, prefix] of candidates) {
    const probe = spawnSync(command, [...prefix, '-c',
      'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'], {
      stdio: 'ignore', windowsHide: true, timeout: 5000
    })
    if (!probe.error && probe.status === 0) return [command, prefix]
  }
  return null
}

const python = resolvePython()
if (!python) {
  process.stderr.write(
    'opencode-delegate: no usable Python 3.9+ interpreter found (missing, too old, or probe timed out).\n' +
    'Install Python 3.9+ (https://www.python.org/downloads/) and restart the MCP client, or set\n' +
    'OC_DELEGATE_PYTHON to an absolute interpreter path in the server environment.\n')
  process.exit(127)
}

const [command, prefix] = python
const child = spawn(command, [...prefix, SERVER, ...process.argv.slice(2)], {
  stdio: 'inherit',
  windowsHide: true,
  env: {
    ...process.env,
    // Keep the stdio protocol UTF-8 whatever the system locale is (a GBK locale corrupts
    // non-ASCII workspace paths before Python ever sees them).
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONDONTWRITEBYTECODE: '1'
  }
})

child.on('error', (error) => {
  process.stderr.write(`opencode-delegate: failed to start the server: ${error.message}\n`)
  process.exit(1)
})

child.on('exit', (code, signal) => {
  process.exit(code === null ? (signal ? 1 : 0) : code)
})

// Forward termination so the client shutting us down shuts the server down too.
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal))
}
