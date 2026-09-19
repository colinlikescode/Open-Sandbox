"""Head product service. Kubernetes owns placement and workload health."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timedelta

import httpx

from opensandbox.auth import owner_id, require_owner
from opensandbox.cluster.kube import Kubernetes
from opensandbox.cluster.manifests import AGENT_PORT, VERIFIED, network_policy, sandbox_job
from opensandbox.config import Settings
from opensandbox.errors import CapacityError, ConflictError, NotFoundError, RuntimeError
from opensandbox.models import CreateSandbox, SandboxInfo
from opensandbox.state import Store
from opensandbox.tokens import ProxyTokenSigner
from opensandbox.utils.clock import utcnow
from opensandbox.utils.sizes import parse_bytes, parse_duration

log = logging.getLogger(__name__)
TERMINAL = {"destroyed", "expired", "failed", "lost"}


def node_ready(node: dict) -> bool:
    return any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in node.get("status", {}).get("conditions", [])
    )


def pod_ready(pod: dict) -> bool:
    return pod.get("status", {}).get("phase") == "Running" and any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in pod.get("status", {}).get("conditions", [])
    )


class Service:
    def __init__(self, settings: Settings, kube: Kubernetes | None = None):
        self.settings = settings
        self.kube = kube or Kubernetes(settings)
        self.store = Store(settings.state_dir / "metadata.db")
        self.signer = ProxyTokenSigner(settings.api_key.get_secret_value())
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), trust_env=False)
        self._create_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._reconciler: asyncio.Task | None = None

    async def start(self):
        await self.store.open()
        await self.store.seed(self.settings.api_key.get_secret_value())
        await self.kube.open()
        await self._cleanup_interrupted_builds()
        await self.reconcile()
        self._reconciler = asyncio.create_task(self._reconcile_loop())

    async def close(self):
        if self._reconciler:
            self._reconciler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconciler
        await self.kube.close()
        await self.http.aclose()
        await self.store.close()

    def lock(self, sandbox_id: str):
        return self._locks.setdefault(sandbox_id, asyncio.Lock())

    async def _reconcile_loop(self):
        while True:
            await asyncio.sleep(self.settings.reconcile_seconds)
            try:
                await self.reconcile()
            except Exception:
                log.exception("Cluster reconciliation failed; retaining last known metadata")

    async def create(self, request: CreateSandbox, key: str | None = None):
        if request.template:
            template = await self.store.template(request.template)
            request = request.model_copy(update={"image": template["image"]})
        if key:
            key = owner_id() + ":" + key
        digest = hashlib.sha256(
            json.dumps(request.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        async with self._create_lock:
            existing = await self.store.by_key(key, digest) if key else None
            if existing:
                return await self.get(existing.id)
            now = utcnow()
            info = SandboxInfo(
                id="sb-" + uuid.uuid4().hex,
                image=request.image,
                cpu=request.cpu,
                memory=parse_bytes(request.memory),
                disk=parse_bytes(request.disk),
                created_at=now,
                expires_at=now + timedelta(seconds=parse_duration(request.timeout)),
                metadata=request.metadata,
                owner=owner_id(),
                template=request.template or request.image,
                internet=request.network.internet,
            )
            await self.store.insert(info, key, digest)
        started = time.monotonic()
        async with self.lock(info.id):
            try:
                nodes = await self.kube.nodes()
                runtime = await self.kube.request(
                    "GET", "/apis/node.k8s.io/v1/runtimeclasses/" + self.settings.runtime_class
                )
                if runtime.get("handler") != "runsc":
                    raise RuntimeError(
                        "Sandbox runtime verification failed: RuntimeClass must use runsc"
                    )
                addresses = [
                    addr["address"]
                    for node in nodes
                    for addr in node.get("status", {}).get("addresses", [])
                    if addr["type"] in {"InternalIP", "ExternalIP"} and ":" not in addr["address"]
                ]
                await self.kube.create(
                    "networkpolicies",
                    network_policy(self.settings, info.id, request.network.internet, addresses),
                )
                job = sandbox_job(self.settings, info.id, request, info.expires_at)
                cached_nodes = [
                    node["metadata"]["name"] for node in nodes if image_cached(node, request.image)
                ]
                spec = job["spec"]["template"]["spec"]
                if cached_nodes:
                    spec["affinity"] = {
                        "nodeAffinity": {
                            "preferredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "weight": 50,
                                    "preference": {
                                        "matchExpressions": [
                                            {
                                                "key": "kubernetes.io/hostname",
                                                "operator": "In",
                                                "values": cached_nodes,
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                built_image = await self.store.image_info(request.image)
                if built_image:
                    spec.setdefault("nodeSelector", {})["kubernetes.io/arch"] = built_image[
                        "architecture"
                    ]
                await self.kube.create("jobs", job)
                while time.monotonic() - started < request.create_timeout:
                    pods = await self.kube.pods(info.id)
                    if pods:
                        pod = pods[0]
                        if pod["spec"].get("runtimeClassName") != self.settings.runtime_class:
                            raise RuntimeError("Sandbox runtime verification failed")
                        info.node = pod["spec"].get("nodeName")
                        if pod_ready(pod):
                            endpoint = await self.kube.forward(pod["metadata"]["name"], AGENT_PORT)
                            health = await self.http.get(endpoint + "/health")
                            health.raise_for_status()
                            if not health.json().get("ok"):
                                raise RuntimeError("Sandbox companion failed readiness")
                            self.verify_runtime(pod, nodes, health.json())
                            info.state = "running"
                            await self.store.put(info)
                            await self.store.count("sandboxes_created_total")
                            await self.store.count("image_cache_lookups_total")
                            if info.node in cached_nodes:
                                await self.store.count("image_cache_hits_total")
                            await self._record_image_pull(pod)
                            await self.store.count(
                                "sandbox_create_seconds_total", time.monotonic() - started
                            )
                            return info
                        if pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
                            raise RuntimeError(
                                pod.get("status", {}).get(
                                    "message", "Sandbox exited before readiness"
                                )
                            )
                        for status in pod.get("status", {}).get("containerStatuses", []) + pod.get(
                            "status", {}
                        ).get("initContainerStatuses", []):
                            waiting = status.get("state", {}).get("waiting", {})
                            if waiting.get("reason") in {
                                "ErrImagePull",
                                "ImagePullBackOff",
                                "InvalidImageName",
                                "CreateContainerConfigError",
                                "RunContainerError",
                            }:
                                raise RuntimeError(waiting.get("message", waiting["reason"]))
                    if utcnow() >= info.expires_at:
                        raise CapacityError("Sandbox lifetime expired before capacity became ready")
                    await asyncio.sleep(0.5)
                raise CapacityError(
                    "No ready sandbox within create_timeout; check capacity, image and node health"
                )
            except BaseException as exc:
                info.state = "failed"
                info.error = str(exc) or "Creation cancelled"
                await self.store.put(info)
                await self.store.count("sandbox_create_failures_total")
                with contextlib.suppress(Exception):
                    await self._cleanup(info.id)
                raise

    def verify_runtime(self, pod, nodes, health):
        node = next(
            (n for n in nodes if n["metadata"]["name"] == pod["spec"].get("nodeName")), None
        )
        if (
            not node
            or node["metadata"].get("labels", {}).get(VERIFIED) != "verified"
            or not health.get("gvisor")
        ):
            raise RuntimeError(
                "Sandbox runtime verification failed: gVisor kernel or node verification missing"
            )

    async def _record_image_pull(self, pod):
        # Kubelet records the actual pull duration in its Pulled event. Missing
        # events must not become a made-up latency or fail a healthy sandbox.
        from opensandbox.errors import OpenSandboxError

        try:
            result = await self.kube.request(
                "GET",
                self.kube.path("events"),
                params={"fieldSelector": "involvedObject.name=" + pod["metadata"]["name"]},
            )
            for event in result["items"]:
                if event.get("reason") != "Pulled" or "initContainers" in event.get(
                    "involvedObject", {}
                ).get("fieldPath", ""):
                    continue
                match = re.search(r" in ([0-9.a-zµ]+) ", event.get("message", ""))
                if match:
                    units = {
                        "ns": 1e-9,
                        "us": 1e-6,
                        "µs": 1e-6,
                        "ms": 1e-3,
                        "s": 1,
                        "m": 60,
                        "h": 3600,
                    }
                    seconds = sum(
                        float(value) * units[unit]
                        for value, unit in re.findall(r"([0-9.]+)(ns|us|µs|ms|s|m|h)", match[1])
                    )
                    await self.store.count("image_pulls_total")
                    await self.store.count("image_pull_seconds_total", seconds)
        except OpenSandboxError:
            pass

    async def _cleanup(self, sandbox_id: str):
        pods = await self.kube.pods(sandbox_id)
        await self.kube.delete("jobs", sandbox_id)
        for pod in pods:
            await self.kube.delete("pods", pod["metadata"]["name"])
            await self.kube.forget(pod["metadata"]["name"])
        await self.kube.delete("networkpolicies", sandbox_id)
        info = await self.store.get(sandbox_id)
        info.cleanup_pending = False
        await self.store.put(info)

    async def _cleanup_interrupted_builds(self):
        namespace = self.settings.build_namespace
        for kind in ("jobs", "pods", "networkpolicies"):
            result = await self.kube.request("GET", self.kube.path(kind, namespace=namespace))
            for item in result["items"]:
                if item["metadata"]["name"].startswith("build-"):
                    await self.kube.delete(kind, item["metadata"]["name"], namespace)

    async def destroy(self, sandbox_id: str):
        async with self.lock(sandbox_id):
            info = await self.store.get(sandbox_id)
            require_owner(info.owner)
            await self._cleanup(sandbox_id)
            info.cleanup_pending = False
            if info.state != "destroyed":
                info.state = "destroyed"
                await self.store.put(info)
                await self.store.count("sandboxes_destroyed_total")
            return info

    async def get(self, sandbox_id: str):
        info = await self.store.get(sandbox_id)
        require_owner(info.owner)
        if info.state not in TERMINAL:
            return await self.refresh(info)
        return info

    async def refresh(self, info: SandboxInfo, nodes: list[dict] | None = None):
        async with self.lock(info.id):
            latest = await self.store.get(info.id)
            if latest.state in TERMINAL:
                return latest
            pods = await self.kube.pods(info.id)
            nodes = nodes if nodes is not None else await self.kube.nodes()
            if utcnow() >= latest.expires_at:
                latest.state = "expired"
            elif not pods:
                latest.state = "lost"
                latest.error = "Sandbox workload no longer exists"
            else:
                pod = pods[0]
                latest.node = pod.get("spec", {}).get("nodeName")
                node = next(
                    (node for node in nodes if node["metadata"]["name"] == latest.node), None
                )
                if latest.node and (node is None or not node_ready(node)):
                    latest.state = "lost"
                    latest.error = "Worker is unavailable; ephemeral sandboxes are not migrated"
                elif pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
                    latest.state = "failed"
                    latest.error = pod.get("status", {}).get("message", "Sandbox process exited")
                elif pod.get("metadata", {}).get("deletionTimestamp"):
                    latest.state = "lost"
                    latest.error = "Sandbox workload was removed"
                elif pod_ready(pod):
                    latest.state = "running"
            await self.store.put(latest)
            if latest.state in TERMINAL:
                await self._cleanup(latest.id)
                latest.cleanup_pending = False
                await self.store.count(f"sandboxes_{latest.state}_total")
            # Caller gets current state, not a stale copy read before reconciliation.
            for field, value in latest.model_dump().items():
                setattr(info, field, value)
            return latest

    async def reconcile(self):
        nodes = await self.kube.nodes()
        for info in await self.store.list():
            if self.lock(info.id).locked():
                continue
            if info.state not in TERMINAL:
                await self.refresh(info, nodes)
            # Retry cleanup after a temporary API outage or interrupted destruction.
            elif info.cleanup_pending:
                await self._cleanup(info.id)

    async def endpoint(self, sandbox_id: str, port: int = AGENT_PORT):
        info = await self.get(sandbox_id)
        if info.state != "running":
            raise ConflictError(f"Sandbox is {info.state}")
        pods = await self.kube.pods(sandbox_id)
        if len(pods) != 1 or not pod_ready(pods[0]):
            raise RuntimeError("Sandbox is not ready")
        return await self.kube.forward(pods[0]["metadata"]["name"], port)

    async def agent(self, sandbox_id: str, method: str, path: str, **kwargs):
        endpoint = await self.endpoint(sandbox_id)
        try:
            response = await self.http.request(method, endpoint + path, **kwargs)
        except httpx.HTTPError as exc:
            raise RuntimeError("Sandbox companion unavailable") from exc
        if response.is_error:
            message = response.json().get("error", {}).get("message", "Sandbox request failed")
            if response.status_code == 404:
                raise NotFoundError(message)
            raise RuntimeError(message)
        return response

    async def set_timeout(self, sandbox_id: str, timeout: str | int):
        # Job deadlines can be extended; Pod activeDeadlineSeconds cannot. The Job
        # controller enforces this independently of both API and untrusted sandbox.
        await self.get(sandbox_id)
        async with self.lock(sandbox_id):
            info = await self.store.get(sandbox_id)
            if info.state != "running":
                raise ConflictError(f"Sandbox is {info.state}")
            expires = utcnow() + timedelta(seconds=parse_duration(timeout))
            job = await self.kube.get("jobs", sandbox_id)
            if not job:
                raise NotFoundError("Sandbox workload not found")
            anchor = job.get("status", {}).get("startTime") or job["metadata"]["creationTimestamp"]
            start = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
            seconds = max(1, math.ceil((expires - start).total_seconds()))
            await self.kube.patch(
                "jobs",
                sandbox_id,
                {
                    "spec": {"activeDeadlineSeconds": seconds},
                    "metadata": {
                        "annotations": {"opensandbox.dev/expires-at": expires.isoformat()}
                    },
                },
            )
            # Persist the authoritative deadline even if a companion is unresponsive.
            info.expires_at = expires
            await self.store.put(info)
            pods = await self.kube.pods(sandbox_id)
            if not pods:
                raise RuntimeError("Sandbox disappeared while setting timeout")
            endpoint = await self.kube.forward(pods[0]["metadata"]["name"], AGENT_PORT)
            response = await self.http.put(
                endpoint + "/timeout", json={"expires": int(expires.timestamp())}
            )
            if response.is_error:
                raise RuntimeError(
                    "Cluster deadline changed, but sandbox companion did not acknowledge it"
                )
            return info

    async def status(self):
        nodes = await self.kube.nodes()
        # Include infrastructure/system pods when calculating schedulable capacity.
        pods = (await self.kube.request("GET", "/api/v1/pods"))["items"]
        result = []
        for node in nodes:
            name = node["metadata"]["name"]
            alloc = node.get("status", {}).get("allocatable", {})
            cpu = cpu_quantity(alloc.get("cpu", "0"))
            memory = parse_bytes(alloc.get("memory", "0"))
            assigned = [
                pod
                for pod in pods
                if pod.get("spec", {}).get("nodeName") == name
                and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            ]
            used_cpu = used_memory = 0.0
            for pod in assigned:
                spec = pod.get("spec", {})
                for resource in ("cpu", "memory"):
                    parse = cpu_quantity if resource == "cpu" else parse_bytes
                    regular = sum(
                        parse(c.get("resources", {}).get("requests", {}).get(resource, "0"))
                        for c in spec.get("containers", [])
                    )
                    init = max(
                        (
                            parse(c.get("resources", {}).get("requests", {}).get(resource, "0"))
                            for c in spec.get("initContainers", [])
                        ),
                        default=0,
                    )
                    requested = max(regular, init) + parse(
                        spec.get("overhead", {}).get(resource, "0")
                    )
                    if resource == "cpu":
                        used_cpu += requested
                    else:
                        used_memory += requested
            schedulable = node_ready(node) and not node.get("spec", {}).get("unschedulable", False)
            schedulable &= node["metadata"].get("labels", {}).get(VERIFIED) == "verified"
            result.append(
                {
                    "name": name,
                    "ready": node_ready(node),
                    "schedulable": schedulable,
                    "cpu": cpu,
                    "memory": memory,
                    "cpu_available": max(0, cpu - used_cpu) if schedulable else 0,
                    "memory_available": max(0, memory - used_memory) if schedulable else 0,
                    "sandboxes": sum(
                        "opensandbox.dev/sandbox" in p["metadata"].get("labels", {})
                        for p in assigned
                    ),
                    "images": node.get("status", {}).get("images", []),
                }
            )
        return {
            "nodes": result,
            "sandboxes": [info.model_dump(mode="json") for info in await self.store.list()],
        }


def cpu_quantity(value: str | int | float) -> float:
    value = str(value)
    for suffix, scale in [("n", 1e-9), ("u", 1e-6), ("m", 1e-3)]:
        if value.endswith(suffix):
            return float(value[:-1]) * scale
    return float(value)


def image_cached(node: dict, image: str) -> bool:
    first, _, rest = image.partition("/")
    normalized = image
    if not rest:
        normalized = "docker.io/library/" + image
    elif "." not in first and ":" not in first and first != "localhost":
        normalized = "docker.io/" + image
    return any(
        name in {image, normalized}
        for record in node.get("status", {}).get("images", [])
        for name in record.get("names", [])
    )
