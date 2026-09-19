"""SDK -> head HTTP API -> real companion process. Kubernetes placement is simulated.

These tests execute only fixed test commands locally. Kernel isolation is verified
by the installer smoke tests on the target Linux VMs, not by this local suite.
"""

import asyncio
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from opensandbox.api import create_app
from opensandbox.cluster.kube import Kubernetes
from opensandbox.sdk.async_client import APIError, AsyncOpenSandbox
from opensandbox.service import Service

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def agent_binary(tmp_path_factory):
    go = shutil.which("go") or str(ROOT / ".cache/opensandbox-tools/go/bin/go")
    if not Path(go).exists():
        pytest.fail("Go is required for companion integration tests; install Go 1.23+")
    binary = tmp_path_factory.mktemp("companion") / "agent"
    subprocess.run([go, "build", "-o", str(binary), "."], cwd=ROOT / "agent", check=True)
    return binary


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
async def roundtrip(running_service, agent_binary, tmp_path):
    service, cluster = running_service
    # These are protocol tests on macOS/Linux, not gVisor tests. Simulate only
    # kernel attestation alongside the already simulated Kubernetes placement.
    original_verifier = service.verify_runtime
    service.verify_runtime = lambda pod, nodes, health: original_verifier(
        pod, nodes, {**health, "gvisor": True}
    )
    port = free_port()
    process = subprocess.Popen(
        [
            str(agent_binary),
            "serve",
            "--workdir",
            str(tmp_path),
            "--expires",
            str(int(time.time()) + 300),
            "--listen",
            f"127.0.0.1:{port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    await service.http.aclose()
    service.http = httpx.AsyncClient(timeout=httpx.Timeout(10, read=None), trust_env=False)
    for _ in range(100):
        try:
            if (await service.http.get(f"http://127.0.0.1:{port}/health")).is_success:
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.02)
    else:
        process.terminate()
        raise AssertionError("Companion did not start")

    async def forward(pod, target, namespace=None):
        return f"http://127.0.0.1:{port if target == 49321 else target}"

    service.kube.forward = forward
    app = create_app(service, manage_lifecycle=False)
    client = AsyncOpenSandbox("http://head", "a" * 40, transport=httpx.ASGITransport(app=app))
    try:
        yield client, service, cluster, tmp_path, app
    finally:
        await client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


async def test_python_sdk_commands_binary_files_background_and_streaming(roundtrip):
    client, service, cluster, root, _ = roundtrip
    async with await client.create(workdir=str(root)) as sandbox:
        result = await sandbox.exec("printf hello; printf problem >&2; exit 4")
        assert result.stdout == "hello" and result.stderr == "problem" and result.exit_code == 4
        await sandbox.write(str(root / "binary"), b"\x00\xff\x01")
        assert await sandbox.read(str(root / "binary"), binary=True) == b"\x00\xff\x01"
        background = await sandbox.exec_background("printf started; sleep 30")
        assert (await background.refresh()).status == "running"
        await background.kill()
        events = [event async for event in background.stream_logs()]
        assert events[-1]["type"] == "exit"
        assert (await background.wait()).status == "killed"
        assert len(await sandbox.list_processes()) == 2
        timed = await sandbox.exec("sleep 30", timeout=0.1)
        assert timed.status == "timed_out"
        before = sandbox.info.expires_at
        after = await sandbox.set_timeout(7200)
        assert after.expires_at > before
    assert not cluster.jobs


async def test_directory_transfer_roundtrip(roundtrip):
    client, _, _, root, _ = roundtrip
    source = root / "local-project"
    (source / "sub").mkdir(parents=True)
    (source / "sub/data").write_bytes(b"\xff\x00payload")
    (source / "empty").mkdir()
    sandbox = await client.create(workdir=str(root))
    await sandbox.upload(source, str(root / "remote-project"))
    assert (
        await sandbox.read(str(root / "remote-project/sub/data"), binary=True) == b"\xff\x00payload"
    )
    await sandbox.download(str(root / "remote-project"), root / "download")
    assert (root / "download/remote-project/sub/data").read_bytes() == b"\xff\x00payload"
    assert (root / "download/remote-project/empty").is_dir()


async def test_authentication_and_no_validation_secret_echo(roundtrip):
    _, _, _, _, app = roundtrip
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://head"
    ) as raw:
        assert (await raw.get("/v1/health")).status_code == 200
        assert (await raw.get("/v1/sandboxes")).status_code == 401
        response = await raw.post(
            "/v1/sandboxes",
            headers={"Authorization": "Bearer " + "a" * 40},
            json={"cpu": 0, "env": {"SECRET": "dont-echo-this"}},
        )
        assert response.status_code == 422
        assert "dont-echo-this" not in response.text


async def test_signed_proxy_reaches_loopback_and_rejects_forgery(roundtrip):
    client, service, _, root, app = roundtrip
    sandbox = await client.create(workdir=str(root))
    port = free_port()
    (root / "index.html").write_text("hello from sandbox service")
    command = await sandbox.exec_background(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(root),
        ]
    )
    url = await sandbox.get_url(port)
    path = httpx.URL(url).raw_path.decode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://head"
    ) as raw:
        for _ in range(50):
            response = await raw.get(path)
            if response.status_code == 200:
                break
            await asyncio.sleep(0.02)
        assert response.text == "hello from sandbox service"
        assert (await raw.get("/p/forged-token/")).status_code == 401
    with pytest.raises(APIError):
        await sandbox.get_url(49321)
    await command.kill()


async def test_commands_survive_head_service_restart(roundtrip):
    client, service, cluster, root, app = roundtrip
    sandbox = await client.create(workdir=str(root))
    command = await sandbox.exec_background("printf persistent; sleep 30")
    for _ in range(100):
        if (await command.refresh()).stdout == "persistent":
            break
        await asyncio.sleep(0.02)
    await client.close()
    forward = service.kube.forward
    await service.close()
    kube = Kubernetes(
        service.settings,
        httpx.AsyncClient(
            base_url="https://cluster", transport=httpx.MockTransport(cluster.handle)
        ),
    )
    kube.forward = forward
    restarted = Service(service.settings, kube)
    await restarted.start()
    app = create_app(restarted, manage_lifecycle=False)
    replacement = AsyncOpenSandbox("http://head", "a" * 40, transport=httpx.ASGITransport(app=app))
    async with replacement:
        recovered = await replacement.get(sandbox.id)
        processes = await recovered.list_processes()
        assert processes[0].id == command.id
        assert processes[0].stdout == "persistent"
        await recovered.kill_process(command.id)
    await restarted.close()
