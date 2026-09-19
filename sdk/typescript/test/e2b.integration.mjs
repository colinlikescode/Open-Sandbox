// Uses the published, unmodified e2b package. Also runnable against a real cluster.
import assert from 'node:assert/strict'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import { Sandbox } from 'e2b'

const root = process.env.TEST_WORKDIR || '/workspace'
const sandbox = await Sandbox.create({ timeoutMs: 120_000, metadata: { suite: 'official-typescript' } })
try {
  assert.equal(await sandbox.isRunning(), true)
  assert.equal((await sandbox.getInfo()).sandboxId, sandbox.sandboxId)
  assert.ok((await Sandbox.list().nextItems()).some(s => s.sandboxId === sandbox.sandboxId))
  const result = await sandbox.commands.run('printf "$ANSWER"; printf warning >&2', { envs: { ANSWER: 'hello' }, cwd: root })
  assert.equal(result.stdout, 'hello')
  assert.equal(result.stderr, 'warning')
  assert.equal(result.exitCode, 0)
  await assert.rejects(sandbox.commands.run('exit 7'), e => e.exitCode === 7)
  const path = `${root}/e2b-typescript/hello.txt`
  await sandbox.files.write(path, 'hello from official TypeScript')
  assert.equal(await sandbox.files.read(path), 'hello from official TypeScript')
  await sandbox.files.write(`${root}/e2b-typescript/binary`, new Uint8Array([0,255,1]).buffer)
  assert.deepEqual(await sandbox.files.read(`${root}/e2b-typescript/binary`, { format: 'bytes' }), new Uint8Array([0,255,1]))
  assert.equal((await sandbox.files.list(`${root}/e2b-typescript`)).length, 2)
  assert.equal(await sandbox.files.exists(path), true)
  assert.equal((await sandbox.files.getInfo(path)).size, 30)
  assert.equal(await sandbox.files.makeDir(`${root}/e2b-typescript/empty`), true)
  assert.equal(await sandbox.files.makeDir(`${root}/e2b-typescript/empty`), false)
  await sandbox.files.rename(path, `${path}.renamed`)
  await sandbox.files.remove(`${path}.renamed`)
  assert.equal(await sandbox.files.exists(path), false)
  await assert.rejects(sandbox.files.read(path))
  let output = ''
  await sandbox.commands.run('printf one; sleep 0.05; printf two', { onStdout: chunk => { output += chunk } })
  assert.equal(output, 'onetwo')
  const cat = await sandbox.commands.run('cat', { background: true, stdin: true })
  await cat.sendStdin('stdin works\n')
  await cat.closeStdin()
  assert.equal((await cat.wait()).stdout, 'stdin works\n')
  const reconnect = await sandbox.commands.run('printf retained; sleep 0.2; printf connected', { background: true })
  reconnect.disconnect()
  assert.equal((await (await sandbox.commands.connect(reconnect.pid)).wait()).stdout, 'retainedconnected')
  await assert.rejects(sandbox.commands.run('sleep 31', { timeoutMs: 100 }))
  await new Promise(r => setTimeout(r, 200))
  assert.ok((await sandbox.commands.list()).every(p => !p.args.includes('sleep 31')))
  const background = await sandbox.commands.run('sleep 30', { background: true })
  assert.ok((await sandbox.commands.list()).some(p => p.pid === background.pid))
  assert.equal(await background.kill(), true)
  let terminalOutput = ''
  let terminal = await sandbox.pty.create({ cols: 80, rows: 24, cwd: root, timeoutMs: 30_000, onData: data => { terminalOutput += new TextDecoder().decode(data) } })
  terminal.disconnect()
  terminal = await sandbox.pty.connect(terminal.pid, { timeoutMs: 30_000, onData: data => { terminalOutput += new TextDecoder().decode(data) } })
  await sandbox.pty.resize(terminal.pid, { cols: 100, rows: 40 })
  await sandbox.pty.sendInput(terminal.pid, new TextEncoder().encode("stty size; printf 'terminal-ok\\n'\n"))
  for (let n = 0; n < 100 && !terminalOutput.includes('40 100'); n++) await new Promise(r => setTimeout(r, 20))
  assert.ok(terminalOutput.includes('40 100'), terminalOutput)
  await sandbox.pty.kill(terminal.pid)

  const listener = net.createServer()
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve))
  const port = listener.address().port
  await new Promise(resolve => listener.close(resolve))
  const quote = value => `'${value.replaceAll("'", "'\\''")}'`
  const python = process.env.TEST_PYTHON || 'python3'
  await sandbox.files.write(`${root}/index.html`, 'official SDK port routing')
  const web = await sandbox.commands.run(`${quote(python)} -m http.server ${port} --bind 0.0.0.0 --directory ${quote(root)}`, { background: true })
  const host = sandbox.getHost(port)
  let response
  for (let n = 0; n < 100; n++) {
    if (process.env.TEST_HEAD_URL) {
      response = await new Promise((resolve, reject) => {
        const transport = process.env.TEST_HEAD_URL.startsWith('https:') ? https : http
        transport.get(process.env.TEST_HEAD_URL, { headers: { Host: host } }, res => {
          let body = ''; res.on('data', chunk => { body += chunk }); res.on('end', () => resolve({ status: res.statusCode, body }))
        }).on('error', reject)
      })
    } else {
      const res = await fetch(`https://${host}`)
      response = { status: res.status, body: await res.text() }
    }
    if (response.status === 200) break
    await new Promise(r => setTimeout(r, 20))
  }
  assert.equal(response.body, 'official SDK port routing')
  await web.kill()
  const connected = await Sandbox.connect(sandbox.sandboxId)
  await connected.setTimeout(180_000)
  assert.equal(await connected.isRunning(), true)
  await connected.kill()
  assert.equal(await sandbox.isRunning(), false)
  console.log('Official E2B TypeScript SDK: lifecycle, commands, files, PTY and ports passed')
} finally { await sandbox.kill() }
