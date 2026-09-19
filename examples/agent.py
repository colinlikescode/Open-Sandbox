"""Run after setting E2B_API_KEY, E2B_API_URL and E2B_SANDBOX_URL."""

from e2b import Sandbox

sandbox = Sandbox.create()
try:
    sandbox.files.write("/workspace/main.py", 'print("hello from OpenSandbox")')
    print(sandbox.commands.run("python3 /workspace/main.py").stdout, end="")
finally:
    sandbox.kill()
