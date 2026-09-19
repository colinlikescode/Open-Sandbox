"""Async Kubernetes REST and loopback port forwarding. No worker SSH transport."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import ssl
from urllib.parse import quote

import httpx
import yaml

from opensandbox.config import Settings
from opensandbox.errors import ConfigurationError, NotFoundError, RuntimeError


class Kubernetes:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client
        self._forwards: dict[tuple[str, str, int], tuple[asyncio.subprocess.Process, int]] = {}
        self._forward_lock = asyncio.Lock()

    async def open(self):
        if self.client is not None:
            return
        config = yaml.safe_load(self.settings.kubeconfig.read_text())
        contexts = {c["name"]: c["context"] for c in config["contexts"]}
        context = contexts[config["current-context"]]
        cluster = next(c["cluster"] for c in config["clusters"] if c["name"] == context["cluster"])
        user = next(c["user"] for c in config["users"] if c["name"] == context["user"])
        if not cluster["server"].startswith("https://") or cluster.get("insecure-skip-tls-verify"):
            raise ConfigurationError("The head requires a TLS-verified Kubernetes API")
        ca = base64.b64decode(cluster["certificate-authority-data"]).decode()
        token = user.get("token")
        if not token:
            raise ConfigurationError(
                "Run init to create the scoped head service-account kubeconfig"
            )
        self.client = httpx.AsyncClient(
            base_url=cluster["server"],
            verify=ssl.create_default_context(cadata=ca),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=False,
        )

    async def request(self, method: str, path: str, *, missing_ok=False, **kwargs):
        assert self.client is not None
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RuntimeError("Kubernetes API unavailable") from exc
        if response.status_code == 404:
            if missing_ok:
                return None
            raise NotFoundError("Kubernetes resource not found")
        if response.is_error:
            try:
                message = response.json().get("message", response.reason_phrase)
            except ValueError:
                message = response.reason_phrase
            raise RuntimeError(f"Kubernetes {method} failed ({response.status_code}): {message}")
        return response.json() if response.content else {}

    def path(self, resource: str, name: str = "", namespace: str | None = None):
        group = {"jobs": "/apis/batch/v1", "networkpolicies": "/apis/networking.k8s.io/v1"}.get(
            resource, "/api/v1"
        )
        namespace = namespace or self.settings.namespace
        path = f"{group}/namespaces/{quote(namespace, safe='')}/{resource}"
        return path + ("/" + quote(name, safe="") if name else "")

    async def create(self, resource: str, body: dict, namespace: str | None = None):
        return await self.request("POST", self.path(resource, namespace=namespace), json=body)

    async def get(self, resource: str, name: str, namespace: str | None = None):
        return await self.request("GET", self.path(resource, name, namespace), missing_ok=True)

    async def delete(self, resource: str, name: str, namespace: str | None = None):
        return await self.request(
            "DELETE",
            self.path(resource, name, namespace),
            missing_ok=True,
            json={"propagationPolicy": "Background", "gracePeriodSeconds": 0},
        )

    async def patch(self, resource: str, name: str, data: dict, namespace: str | None = None):
        return await self.request(
            "PATCH",
            self.path(resource, name, namespace),
            json=data,
            headers={"Content-Type": "application/merge-patch+json"},
        )

    async def pods(self, sandbox_id: str | None = None, namespace: str | None = None):
        selector = "app.kubernetes.io/managed-by=opensandbox"
        if sandbox_id:
            selector += f",opensandbox.dev/sandbox={sandbox_id}"
        data = await self.request(
            "GET", self.path("pods", namespace=namespace), params={"labelSelector": selector}
        )
        return data["items"]

    async def nodes(self):
        return (await self.request("GET", "/api/v1/nodes"))["items"]

    async def forward(self, pod: str, port: int, namespace: str | None = None) -> str:
        namespace = namespace or self.settings.namespace
        key = (namespace, pod, port)
        async with self._forward_lock:
            cached = self._forwards.get(key)
            if cached and cached[0].returncode is None:
                return f"http://127.0.0.1:{cached[1]}"
            process = await asyncio.create_subprocess_exec(
                self.settings.kubectl,
                "--kubeconfig",
                str(self.settings.kubeconfig),
                "--namespace",
                namespace,
                "port-forward",
                "--address=127.0.0.1",
                f"pod/{pod}",
                f":{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                async with asyncio.timeout(20):
                    assert process.stdout is not None
                    while True:
                        line = await process.stdout.readline()
                        if not line:
                            raise RuntimeError("Kubernetes port forwarding failed")
                        match = re.search(rb"Forwarding from 127\.0\.0\.1:(\d+)", line)
                        if match:
                            local_port = int(match.group(1))
                            break
            except BaseException:
                await self._stop(process)
                raise
            self._forwards[key] = process, local_port
            # kubectl writes one line per connection; drain it to avoid a full pipe.
            asyncio.create_task(self._drain(process))
            return f"http://127.0.0.1:{local_port}"

    async def _drain(self, process):
        assert process.stdout is not None
        while await process.stdout.read(4096):
            pass

    async def forget(self, pod: str):
        async with self._forward_lock:
            for key in [key for key in self._forwards if key[1] == pod]:
                await self._stop(self._forwards.pop(key)[0])

    @staticmethod
    async def _stop(process):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def close(self):
        for process, _ in self._forwards.values():
            await self._stop(process)
        self._forwards.clear()
        if self.client:
            await self.client.aclose()
