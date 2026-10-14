'use strict'
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const filename = path.join(__dirname, '..', 'bin', 'opencode-delegate.js')
const source = fs.readFileSync(filename, 'utf8')

function run({ platform = 'win32', env = {}, probes = [] } = {}) {
  const calls = []
  let error = ''
  let exitCode = null
  const handlers = {}
  const child = { on(name, callback) { handlers[name] = callback }, kill() {} }
  const cp = {
    spawnSync(command, args, options) {
      calls.push({ type: 'probe', command, args, options })
      return probes.length ? probes.shift() : { status: 0 }
    },
    spawn(command, args, options) {
      calls.push({ type: 'server', command, args, options })
      return child
    }
  }
  const process = {
    platform, env, argv: ['node', filename], on() {},
    exit(code) { exitCode = code; throw new Error('test-exit') },
    stderr: { write(text) { error += text } }
  }
  try {
    vm.runInNewContext(source, {
      __dirname: path.dirname(filename), process,
      require(name) { return name === 'node:child_process' ? cp : require(name) }
    })
  } catch (e) { if (e.message !== 'test-exit') throw e }
  return { calls, error, exitCode, handlers }
}

const cases = {
  'uses hidden, bounded version probes and hidden server': () => {
    const result = run()
    const [probe, server] = result.calls
    assert.match(probe.args[probe.args.length - 1], /sys\.version_info >= \(3, 9\)/)
    assert.equal(probe.options.windowsHide, true)
    assert.equal(probe.options.timeout, 5000)
    assert.equal(server.options.windowsHide, true)
    assert.equal(server.options.stdio, 'inherit')
    assert.equal(server.options.env.PYTHONUTF8, '1')
  },
  'skips old interpreter and tries next candidate': () => {
    const result = run({ probes: [{ status: 1 }, { status: 0 }] })
    assert.equal(result.calls[result.calls.length - 1].command, 'python')
  },
  'skips a timed-out probe': () => {
    const result = run({ probes: [{ status: null, error: { code: 'ETIMEDOUT' } }, { status: 0 }] })
    assert.equal(result.calls[result.calls.length - 1].command, 'python')
  },
  'validates explicit interpreter without silently substituting': () => {
    const result = run({ env: { OC_DELEGATE_PYTHON: '/path with spaces/python' }, probes: [{ status: 1 }] })
    assert.equal(result.calls.length, 1)
    assert.equal(result.calls[0].command, '/path with spaces/python')
    assert.equal(result.exitCode, 127)
    assert.match(result.error, /Python 3\.9\+/)
  },
  'reports all interpreters missing on stderr': () => {
    const result = run({ probes: Array(3).fill({ error: { code: 'ENOENT' } }) })
    assert.equal(result.exitCode, 127)
    assert.equal(result.calls.filter(call => call.type === 'server').length, 0)
  },
  'uses python3 first on POSIX': () => {
    const result = run({ platform: 'linux' })
    assert.equal(result.calls[0].command, 'python3')
  }
}
let failures = 0
for (const [name, test] of Object.entries(cases)) {
  try { test(); console.log('PASS ' + name) }
  catch (error) { failures++; console.error('FAIL ' + name, error) }
}
if (failures) process.exit(1)
console.log(Object.keys(cases).length + ' launcher tests passed')

