"""Isolated Dockerfile builds and publication through the head's OCI registry."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import tarfile
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path, PurePosixPath

import httpx

from opensandbox.cluster.manifests import AGENT_PORT, network_policy, sandbox_job
from opensandbox.cluster.registry import Registry
from opensandbox.config import RegistryAuth
from opensandbox.errors import RuntimeError, ValidationError
from opensandbox.models import CreateSandbox
from opensandbox.registry_proxy import RegistryProxy
from opensandbox.service import pod_ready
from opensandbox.utils.clock import utcnow

MAX_IMAGE_BYTES = 8 * 1024**3


def validate_context(path: Path, dockerfile: str, limit: int):
    wanted = PurePosixPath(dockerfile)
    if wanted.is_absolute() or ".." in wanted.parts:
        raise ValidationError("Dockerfile must be inside the uploaded context")
    found = False
    total = 0
    try:
        with tarfile.open(path) as archive:
            for count, member in enumerate(archive):
                name = PurePosixPath(member.name)
                if count > 100000 or name.is_absolute() or ".." in name.parts:
                    raise ValidationError("Unsafe build context path")
                if not member.isfile() and not member.isdir():
                    raise ValidationError("Build contexts cannot contain links or special files")
                total += member.size
                if total > limit:
                    raise ValidationError("Expanded context exceeds upload limit")
                found |= name == wanted and member.isfile()
    except tarfile.TarError as exc:
        raise ValidationError("Invalid build context archive") from exc
    if not found:
        raise ValidationError("Dockerfile not found in uploaded context")


class ImageBuilder:
    def __init__(self, service):
        self.service = service
        self.slots = asyncio.Semaphore(2)
        self.proxy = RegistryProxy(service.settings)

    async def build(self, context: Path, dockerfile: str):
        validate_context(context, dockerfile, self.service.settings.max_upload_bytes)
        async with self.slots:
            return await self._build(context, dockerfile)

    async def _build(self, context: Path, dockerfile: str):
        service = self.service
        settings = service.settings.model_copy(
            update={
                "namespace": service.settings.build_namespace,
                "max_upload_bytes": MAX_IMAGE_BYTES,
            }
        )
        build_id = "build-" + uuid.uuid4().hex
        request = CreateSandbox(
            image=settings.builder_image,
            cpu=2,
            memory="4Gi",
            disk="20Gi",
            timeout=1800,
            workdir="/kaniko",
        )
        started = time.monotonic()
        pod_name = None
        tokens = []
        gateway = httpx.URL(settings.endpoint)
        gateway_host = gateway.netloc.decode()
        gateway_port = gateway.port or (443 if gateway.scheme == "https" else 80)
        try:
            addresses = [
                address["address"]
                for node in await service.kube.nodes()
                for address in node.get("status", {}).get("addresses", [])
                if address["type"] in {"InternalIP", "ExternalIP"} and ":" not in address["address"]
            ]
            policy = network_policy(settings, build_id, True, addresses)
            policy["spec"]["egress"].append(
                {
                    "to": [{"ipBlock": {"cidr": settings.head_ip + "/32"}}],
                    "ports": [{"protocol": "TCP", "port": gateway_port}],
                }
            )
            await service.kube.create("networkpolicies", policy, settings.namespace)
            job = sandbox_job(settings, build_id, request, utcnow() + timedelta(seconds=1800))
            job["spec"]["template"]["spec"]["containers"][0]["securityContext"]["capabilities"][
                "add"
            ] = [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "FSETID",
                "SETUID",
                "SETGID",
                "SETFCAP",
            ]
            await service.kube.create("jobs", job, settings.namespace)
            async with asyncio.timeout(180):
                while True:
                    pods = await service.kube.pods(build_id, settings.namespace)
                    if pods and pod_ready(pods[0]):
                        pod_name = pods[0]["metadata"]["name"]
                        break
                    if pods and pods[0].get("status", {}).get("phase") in {"Failed", "Succeeded"}:
                        raise RuntimeError("Image builder exited before becoming ready")
                    await asyncio.sleep(0.5)
            endpoint = await service.kube.forward(pod_name, AGENT_PORT, settings.namespace)

            async def context_chunks():
                with context.open("rb") as stream:
                    while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
                        yield chunk

            response = await service.http.put(
                endpoint + "/archive",
                params={"path": "/kaniko/opensandbox-context"},
                content=context_chunks(),
            )
            response.raise_for_status()
            command = [
                "/kaniko/executor",
                "--force",
                "--context=dir:///kaniko/opensandbox-context",
                "--dockerfile=/kaniko/opensandbox-context/" + dockerfile,
                "--no-push",
                "--no-push-cache",
                "--tar-path=/kaniko/opensandbox-image.tar",
                "--destination=opensandbox/build:latest",
            ]
            if settings.api_ca:
                response = await service.http.put(
                    endpoint + "/files",
                    params={"path": "/kaniko/opensandbox-api-ca.crt"},
                    content=settings.api_ca.read_bytes(),
                )
                response.raise_for_status()
                command.extend(
                    [
                        "--registry-certificate",
                        f"{gateway_host}=/kaniko/opensandbox-api-ca.crt",
                    ]
                )
            registries = {
                **settings.upstream_registries,
                settings.registry: RegistryAuth(
                    username=settings.registry_username, password=settings.registry_password
                ),
            }
            for host, credential in registries.items():
                token = self.proxy.grant(host, credential)
                tokens.append(token)
                proxy = f"{str(gateway).rstrip('/')}/opensandbox-build/{token}"
                command.extend(["--registry-map", host + "=" + proxy])
            if gateway.scheme == "http":
                command.extend(["--insecure-registry", gateway_host])
            # Registry credentials remain on the head. The untrusted build only
            # produces an archive; publication is performed after leaving gVisor.
            async with asyncio.timeout(1500):
                response = await service.http.post(
                    endpoint + "/commands", json={"command": command, "timeout": 1440}
                )
                response.raise_for_status()
                result = response.json()
            if result.get("exit_code") != 0:
                raise RuntimeError(
                    "Image build failed: "
                    + result.get("stderr", "")[-4000:]
                    + "\n"
                    + result.get("stdout", "")[-4000:]
                )
            with tempfile.TemporaryDirectory(dir=settings.state_dir) as directory:
                archive = Path(directory) / "image.tar"
                size = 0
                async with service.http.stream(
                    "GET", endpoint + "/files", params={"path": "/kaniko/opensandbox-image.tar"}
                ) as response:
                    response.raise_for_status()
                    with archive.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_IMAGE_BYTES:
                                raise RuntimeError("Built image exceeds 8 GiB")
                            await asyncio.to_thread(output.write, chunk)
                verify = (
                    ssl.create_default_context(cafile=str(settings.registry_ca))
                    if settings.registry_ca
                    else True
                )
                registry = Registry(
                    "https://" + settings.registry,
                    settings.registry_username,
                    settings.registry_password.get_secret_value(),
                    verify=verify,
                )
                try:
                    image = await asyncio.to_thread(
                        registry.push_docker_archive, archive, "opensandbox/builds", build_id
                    )
                finally:
                    registry.close()
            image["build_seconds"] = time.monotonic() - started
            image["created_at"] = utcnow().isoformat()
            await service.store.image(image["reference"], image)
            await service.store.count("images_built_total")
            await service.store.count("image_build_seconds_total", image["build_seconds"])
            return image
        except (httpx.HTTPError, TimeoutError) as exc:
            await service.store.count("image_build_failures_total")
            raise RuntimeError("Image build failed or timed out") from exc
        finally:
            self.proxy.revoke(tokens)
            with contextlib.suppress(Exception):
                await service.kube.delete("jobs", build_id, settings.namespace)
                if pod_name:
                    await service.kube.delete("pods", pod_name, settings.namespace)
                    await service.kube.forget(pod_name)
                await service.kube.delete("networkpolicies", build_id, settings.namespace)
