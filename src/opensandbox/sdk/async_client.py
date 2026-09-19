"""Remote asynchronous SDK. Clients never start a control plane."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
from pathlib import Path

import httpx

from opensandbox.errors import ConfigurationError, OpenSandboxError, ValidationError
from opensandbox.models import CommandInfo, SandboxInfo
from opensandbox.sdk.files import MAX_TRANSFER, pack, unpack


class APIError(OpenSandboxError):
    def __init__(self, status: int, payload: dict):
        error = payload.get("error", {})
        super().__init__(error.get("message", f"API request failed ({status})"))
        self.http_status = status
        self.code = error.get("code", "api_error")


class AsyncOpenSandbox:
    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        ca_cert: str | Path | None = None,
    ):
        endpoint = (
            endpoint or os.environ.get("OPENSANDBOX_ENDPOINT") or os.environ.get("E2B_API_URL")
        )
        api_key = api_key or os.environ.get("OPENSANDBOX_API_KEY") or os.environ.get("E2B_API_KEY")
        ca_cert = ca_cert or os.environ.get("OPENSANDBOX_CA_CERT")
        if not endpoint or not api_key:
            raise ConfigurationError(
                "Set OPENSANDBOX_ENDPOINT and OPENSANDBOX_API_KEY, or pass endpoint and api_key"
            )
        if not endpoint.startswith(("http://", "https://")):
            raise ConfigurationError("endpoint must be an HTTP(S) URL")
        self.http = httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            headers={"Authorization": "Bearer " + api_key},
            timeout=httpx.Timeout(30, read=None),
            transport=transport,
            follow_redirects=False,
            verify=ssl.create_default_context(cafile=str(ca_cert)) if ca_cert else True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        await self.check(response)
        return response

    @staticmethod
    async def check(response: httpx.Response):
        if response.is_error:
            await response.aread()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise APIError(response.status_code, payload)

    async def create(
        self, *, image="python:3.13-slim", idempotency_key: str | None = None, **kwargs
    ):
        if image.startswith(("./", "../", "/", "~")) or await asyncio.to_thread(Path(image).exists):
            image = (await self.build(image))["reference"]
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        response = await self.request(
            "POST", "/v1/sandboxes", json={"image": image, **kwargs}, headers=headers
        )
        return AsyncSandbox(self, SandboxInfo.model_validate(response.json()))

    async def get(self, sandbox_id: str):
        response = await self.request("GET", f"/v1/sandboxes/{sandbox_id}")
        return AsyncSandbox(self, SandboxInfo.model_validate(response.json()))

    async def list(self):
        return [
            SandboxInfo.model_validate(item)
            for item in (await self.request("GET", "/v1/sandboxes")).json()
        ]

    async def build(self, path: str | Path, *, name: str | None = None):
        archive, dockerfile = await asyncio.to_thread(pack, Path(path), context=True)
        return (
            await self.request(
                "POST",
                "/v1/images/build",
                params={"dockerfile": dockerfile, **({"name": name} if name else {})},
                content=archive,
                headers={"Content-Type": "application/x-tar"},
            )
        ).json()

    async def images(self):
        return (await self.request("GET", "/v1/images")).json()

    async def status(self):
        return (await self.request("GET", "/v1/status")).json()

    async def nodes(self):
        return (await self.request("GET", "/v1/nodes")).json()

    async def doctor(self):
        return (await self.request("POST", "/v1/doctor")).json()

    async def metrics(self):
        return (await self.request("GET", "/v1/metrics")).json()


class AsyncSandbox:
    def __init__(self, client: AsyncOpenSandbox, info: SandboxInfo):
        self.client, self.info, self.id = client, info, info.id
        self.path = f"/v1/sandboxes/{self.id}"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.destroy()

    async def refresh(self):
        self.info = (await self.client.get(self.id)).info
        return self.info

    async def exec(self, command: str | list[str], *, timeout=None, cwd=None, env=None):
        response = await self.client.request(
            "POST",
            self.path + "/commands",
            json={
                "command": command,
                "timeout": timeout,
                "cwd": cwd,
                "env": env or {},
                "background": False,
            },
        )
        return CommandInfo.model_validate(response.json())

    async def exec_background(self, command: str | list[str], *, timeout=None, cwd=None, env=None):
        response = await self.client.request(
            "POST",
            self.path + "/commands",
            json={
                "command": command,
                "timeout": timeout,
                "cwd": cwd,
                "env": env or {},
                "background": True,
            },
        )
        return AsyncCommand(self, CommandInfo.model_validate(response.json()))

    async def list_processes(self):
        return [
            CommandInfo.model_validate(item)
            for item in (await self.client.request("GET", self.path + "/commands")).json()
        ]

    async def kill_process(self, command_id: str):
        return CommandInfo.model_validate(
            (await self.client.request("DELETE", self.path + "/commands/" + command_id)).json()
        )

    async def write(self, path: str, content: str | bytes):
        data = content.encode() if isinstance(content, str) else content
        if len(data) > MAX_TRANSFER:
            raise ValidationError("File exceeds 512 MiB")
        await self.client.request(
            "PUT",
            self.path + "/files",
            params={"path": path},
            content=data,
        )

    async def _read_transfer(self, route: str, path: str):
        data = bytearray()
        async with self.client.http.stream(
            "GET", self.path + route, params={"path": path}
        ) as response:
            await self.client.check(response)
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_TRANSFER:
                    raise ValidationError("Download exceeds 512 MiB")
                data.extend(chunk)
        return bytes(data)

    async def read(self, path: str, *, binary=False):
        data = await self._read_transfer("/files", path)
        return data if binary else data.decode("utf-8", errors="replace")

    async def upload(self, local: str | Path, remote: str):
        source = await asyncio.to_thread(Path(local).expanduser)
        if await asyncio.to_thread(source.is_symlink):
            raise ValidationError("Transfers do not follow symlinks")
        if await asyncio.to_thread(source.is_file):
            if (await asyncio.to_thread(source.stat)).st_size > MAX_TRANSFER:
                raise ValidationError("File exceeds 512 MiB")
            await self.write(remote, await asyncio.to_thread(source.read_bytes))
        else:
            archive, _ = await asyncio.to_thread(pack, source)
            # Directory transfers put the contents at the requested destination.
            import io
            import tarfile

            output = io.BytesIO()
            with (
                tarfile.open(fileobj=io.BytesIO(archive)) as src,
                tarfile.open(fileobj=output, mode="w") as dst,
            ):
                for member in src:
                    member.name = str(Path(member.name).relative_to(source.name))
                    dst.addfile(member, src.extractfile(member) if member.isfile() else None)
            await self.client.request(
                "PUT", self.path + "/archive", params={"path": remote}, content=output.getvalue()
            )

    async def download(self, remote: str, local: str | Path):
        data = await self._read_transfer("/archive", remote)
        destination = await asyncio.to_thread(Path(local).expanduser)
        await asyncio.to_thread(unpack, data, destination)

    async def get_url(self, port: int, *, ttl=3600):
        return (
            await self.client.request("POST", f"{self.path}/ports/{port}", params={"ttl": ttl})
        ).json()["url"]

    async def set_timeout(self, timeout: str | int):
        self.info = SandboxInfo.model_validate(
            (
                await self.client.request("PUT", self.path + "/timeout", json={"timeout": timeout})
            ).json()
        )
        return self.info

    async def destroy(self):
        self.info = SandboxInfo.model_validate(
            (await self.client.request("DELETE", self.path)).json()
        )
        return self.info


class AsyncCommand:
    def __init__(self, sandbox: AsyncSandbox, info: CommandInfo):
        self.sandbox, self.info, self.id = sandbox, info, info.id
        self.path = sandbox.path + "/commands/" + self.id

    async def refresh(self):
        self.info = CommandInfo.model_validate(
            (await self.sandbox.client.request("GET", self.path)).json()
        )
        return self.info

    async def wait(self):
        while (await self.refresh()).finished_at is None:
            await asyncio.sleep(0.1)
        return self.info

    async def kill(self):
        return await self.sandbox.kill_process(self.id)

    async def stream_logs(self, *, from_seq=0):
        async with self.sandbox.client.http.stream(
            "GET", self.path + "/events", params={"from_seq": from_seq}
        ) as response:
            await self.sandbox.client.check(response)
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])
