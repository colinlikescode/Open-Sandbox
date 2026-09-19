import { Sandbox } from 'e2b'

const sandbox = await Sandbox.create()
try {
  await sandbox.files.write('/workspace/main.py', 'print("hello from OpenSandbox")')
  console.log((await sandbox.commands.run('python3 /workspace/main.py')).stdout)
} finally {
  await sandbox.kill()
}
