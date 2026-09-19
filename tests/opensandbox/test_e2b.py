"""Unmodified E2B SDKs -> real HTTP gateway -> real companion.

Only Kubernetes placement is simulated here. Cluster acceptance reuses the same
SDK scenarios against installed Linux nodes; no kernel-isolation claim is made
by these local protocol tests.
"""

import asyncio
import os
import socket
import subprocess
import sys

import httpx
import pytest
import uvicorn

from .test_roundtrip import ROOT


@pytest.fixture(params=["http", "https"])
async def e2b_head(roundtrip, monkeypatch, request):
    _, service, cluster, root, app = roundtrip
    await service.store.put_template("base", {"image": "python:3.13-slim", "workdir": str(root)})
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    scheme = request.param
    url = f"{scheme}://127.0.0.1:{port}"
    service.settings.sandbox_domain = f"127.0.0.1.sslip.io:{port}"
    service.settings.endpoint = url
    tls = {}
    if scheme == "https":
        ca, ca_key = root / "ca.crt", root / "ca.key"
        cert, key = root / "server.crt", root / "api.key"
        config = root / "cert.conf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=OpenSandbox-test-CA\n[ext]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-config",
                str(config),
                "-keyout",
                str(ca_key),
                "-out",
                str(ca),
            ],
            check=True,
            capture_output=True,
        )
        csr = root / "server.csr"
        subprocess.run(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=OpenSandbox-test",
                "-keyout",
                str(key),
                "-out",
                str(csr),
            ],
            check=True,
            capture_output=True,
        )
        extensions = root / "server.ext"
        extensions.write_text(
            "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1,DNS:*.127.0.0.1.sslip.io\n"
        )
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(cert),
                "-days",
                "1",
                "-extfile",
                str(extensions),
            ],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(ca))
        tls = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", access_log=False, **tls)
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.01)
    monkeypatch.setenv("E2B_API_URL", url)
    monkeypatch.setenv("E2B_SANDBOX_URL", url)
    monkeypatch.setenv("E2B_API_KEY", "a" * 40)
    monkeypatch.delenv("E2B_DEBUG", raising=False)
    try:
        yield service, root, url
    finally:
        # The simulated Kubernetes adapter cannot stop real local processes.
        # Clean up this test companion's process groups before stopping HTTP.
        endpoint = await service.kube.forward("test", 49321)
        response = await service.http.get(endpoint + "/commands")
        for command in response.json():
            if command["status"] == "running":
                await service.http.delete(endpoint + "/commands/" + command["id"])
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


def python_scenario(root, url=None):
    import time

    from e2b import CommandExitException, PtySize, Sandbox, TimeoutException
    from e2b.exceptions import NotFoundException

    sandbox = Sandbox.create(timeout=120, metadata={"suite": "official-python"})
    assert sandbox.is_running()
    assert sandbox.get_info().sandbox_id == sandbox.sandbox_id
    assert any(s.sandbox_id == sandbox.sandbox_id for s in Sandbox.list().next_items())
    result = sandbox.commands.run(
        "printf '%s' \"$ANSWER\"; printf warning >&2", envs={"ANSWER": "hello"}, cwd=str(root)
    )
    assert result.stdout == "hello" and result.stderr == "warning" and result.exit_code == 0
    with pytest.raises(CommandExitException) as failure:
        sandbox.commands.run("exit 7")
    assert failure.value.exit_code == 7
    path = str(root / "e2b-python" / "hello.txt")
    sandbox.files.write(path, "hello from official Python")
    assert sandbox.files.read(path) == "hello from official Python"
    binary = str(root / "e2b-python" / "binary")
    sandbox.files.write(binary, b"\x00\xff\x01")
    assert sandbox.files.read(binary, format="bytes") == b"\x00\xff\x01"
    assert {entry.name for entry in sandbox.files.list(str(root / "e2b-python"))} == {
        "hello.txt",
        "binary",
    }
    assert sandbox.files.exists(path)
    assert sandbox.files.get_info(path).size == len("hello from official Python")
    assert sandbox.files.make_dir(str(root / "e2b-python" / "empty"))
    assert not sandbox.files.make_dir(str(root / "e2b-python" / "empty"))
    sandbox.files.rename(path, path + ".renamed")
    assert not sandbox.files.exists(path)
    sandbox.files.remove(path + ".renamed")
    with pytest.raises(NotFoundException):
        sandbox.files.read(path)

    chunks = []
    sandbox.commands.run("printf one; sleep 0.05; printf two", on_stdout=chunks.append)
    assert "".join(chunks) == "onetwo"
    process = sandbox.commands.run("cat", background=True, stdin=True)
    sandbox.commands.send_stdin(process.pid, "stdin works\n")
    process.close_stdin()
    assert process.wait().stdout == "stdin works\n"
    reconnect = sandbox.commands.run(
        "printf retained; sleep 0.2; printf connected", background=True
    )
    reconnect.disconnect()
    assert sandbox.commands.connect(reconnect.pid).wait().stdout == "retainedconnected"
    with pytest.raises((TimeoutException, CommandExitException)):
        sandbox.commands.run("sleep 31", timeout=0.1)
    time.sleep(0.2)
    assert all("sleep 31" not in p.args for p in sandbox.commands.list())
    background = sandbox.commands.run("sleep 30", background=True)
    assert any(p.pid == background.pid for p in sandbox.commands.list())
    assert sandbox.commands.kill(background.pid)
    terminal_output = []
    terminal = sandbox.pty.create(PtySize(cols=80, rows=24), timeout=30, cwd=str(root))
    terminal.disconnect()
    terminal = sandbox.pty.connect(terminal.pid, timeout=30)
    sandbox.pty.resize(terminal.pid, PtySize(cols=100, rows=40))
    sandbox.pty.send_stdin(terminal.pid, b"stty size; printf 'terminal-ok\\n'; exit\n")
    terminal.wait(on_pty=terminal_output.append)
    assert b"40 100" in b"".join(terminal_output)
    assert b"terminal-ok" in b"".join(terminal_output)
    # Closing a still-running terminal is a separate operation from shell exit.
    terminal = sandbox.pty.create(PtySize(cols=80, rows=24), timeout=30)
    assert sandbox.pty.kill(terminal.pid)
    terminal.disconnect()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        exposed_port = sock.getsockname()[1]
    import shlex

    web = sandbox.commands.run(
        f"{shlex.quote(os.environ.get('TEST_PYTHON', 'python3'))} -m http.server {exposed_port} --bind 127.0.0.1 --directory {shlex.quote(str(root))}",
        background=True,
    )
    sandbox.files.write(str(root / "index.html"), "official SDK port routing")
    host = sandbox.get_host(exposed_port)
    import ssl

    verify = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
    with httpx.Client(trust_env=False, verify=verify) as http:
        for _ in range(100):
            # Preserve the exact SDK hostname; dial loopback so local tests need no DNS service.
            response = (
                http.get(url + "/", headers={"Host": host})
                if url
                else http.get("https://" + host + "/")
            )
            if response.status_code == 200:
                break
            time.sleep(0.02)
    assert response.text == "official SDK port routing"
    sandbox.commands.kill(web.pid)
    connected = Sandbox.connect(sandbox.sandbox_id)
    connected.set_timeout(180)
    assert connected.is_running()
    connected.kill()
    assert not sandbox.is_running()


async def test_official_python_sdk(e2b_head):
    _, root, url = e2b_head
    if sys.platform == "darwin" and url.startswith("https:"):
        pytest.skip(
            "E2B's native macOS TLS verifier requires Keychain trust; this test does not change the host trust store. Linux CI runs this case."
        )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from pathlib import Path; import os; from tests.opensandbox.test_e2b import python_scenario; python_scenario(Path(os.environ['TEST_WORKDIR']), os.environ['TEST_HEAD_URL'])",
        env={
            **os.environ,
            "TEST_WORKDIR": str(root),
            "TEST_HEAD_URL": url,
            "TEST_PYTHON": sys.executable,
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 0, output.decode()


async def test_official_typescript_sdk(e2b_head):
    _, root, url = e2b_head
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "sdk/typescript/test/e2b.integration.mjs"),
        env={
            **os.environ,
            "TEST_WORKDIR": str(root),
            "TEST_HEAD_URL": url,
            "TEST_PYTHON": sys.executable,
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 0, output.decode()


async def test_websocket_port_gateway(e2b_head):
    import ssl

    import websockets

    from opensandbox.models import CreateSandbox

    service, root, url = e2b_head
    sandbox = await service.create(CreateSandbox(workdir=str(root)))

    async def echo(socket):
        async for message in socket:
            await socket.send(message)

    async with websockets.serve(echo, "127.0.0.1", 0) as backend:
        port = backend.sockets[0].getsockname()[1]
        ws_url = url.replace("https:", "wss:").replace("http:", "ws:") + "/echo"
        options = (
            {"ssl": ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])}
            if url.startswith("https:")
            else {}
        )
        async with websockets.connect(
            ws_url,
            additional_headers={"E2b-Sandbox-Id": sandbox.id, "E2b-Sandbox-Port": str(port)},
            **options,
        ) as socket:
            await socket.send("text frame")
            assert await socket.recv() == "text frame"
            await socket.send(b"\x00\xff")
            assert await socket.recv() == b"\x00\xff"
    await service.destroy(sandbox.id)
