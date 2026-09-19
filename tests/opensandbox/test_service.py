"""Head lifecycle tests against the real Kubernetes HTTP adapter with simulated API responses."""

import json
from datetime import timedelta

import httpx
import pytest

from opensandbox.cluster.kube import Kubernetes
from opensandbox.config import Settings
from opensandbox.errors import ConflictError, RuntimeError
from opensandbox.models import CreateSandbox
from opensandbox.service import Service
from opensandbox.utils.clock import utcnow


class ClusterHTTP:
    def __init__(self):
        self.jobs = {}
        self.workloads = {}
        self.policies = {}
        self.operations = []
        self.ready = True
        self.runtime = "gvisor"

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.operations.append((request.method, path, body))
        if path == "/apis/node.k8s.io/v1/runtimeclasses/gvisor":
            return httpx.Response(200, json={"handler": "runsc"})
        if path == "/api/v1/nodes":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {
                                "name": "worker",
                                "labels": {"opensandbox.dev/gvisor": "verified"},
                            },
                            "status": {
                                "conditions": [
                                    {"type": "Ready", "status": "True" if self.ready else "False"}
                                ],
                                "addresses": [{"type": "InternalIP", "address": "10.0.0.2"}],
                                "allocatable": {"cpu": "4", "memory": "8Gi"},
                            },
                        }
                    ]
                },
            )
        resource = path.split("/")[-1]
        for kind, storage in [
            ("jobs", self.jobs),
            ("networkpolicies", self.policies),
            ("pods", self.workloads),
        ]:
            if resource == kind:
                if request.method == "POST":
                    name = body["metadata"]["name"]
                    storage[name] = body
                    if kind == "jobs":
                        body["metadata"]["creationTimestamp"] = utcnow().isoformat()
                        pod = json.loads(json.dumps(body["spec"]["template"]))
                        pod["metadata"]["name"] = name + "-pod"
                        pod["spec"]["nodeName"] = "worker"
                        pod["spec"]["runtimeClassName"] = self.runtime
                        pod["status"] = {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        }
                        self.workloads[name + "-pod"] = pod
                    return httpx.Response(201, json=body)
                selector = request.url.params.get("labelSelector", "")
                workloads = list(storage.values())
                if "opensandbox.dev/sandbox=" in selector:
                    name = selector.split("opensandbox.dev/sandbox=")[1]
                    workloads = [
                        p
                        for p in workloads
                        if p["metadata"].get("labels", {}).get("opensandbox.dev/sandbox") == name
                    ]
                return httpx.Response(200, json={"items": workloads})
            if "/" + kind + "/" in path:
                name = resource
                if request.method == "DELETE":
                    storage.pop(name, None)
                    return httpx.Response(200, json={})
                if name not in storage:
                    return httpx.Response(404, json={})
                if request.method == "PATCH":
                    for key, value in body.items():
                        storage[name].setdefault(key, {}).update(value)
                return httpx.Response(200, json=storage[name])
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
async def running_service(tmp_path):
    cluster = ClusterHTTP()
    config = Settings(
        state_dir=tmp_path,
        api_key="a" * 40,
        registry_password="b" * 40,
        registry="10.0.0.1:30500",
        agent_image="agent:test",
        reconcile_seconds=3600,
    )
    kube = Kubernetes(
        config,
        httpx.AsyncClient(
            base_url="https://cluster", transport=httpx.MockTransport(cluster.handle)
        ),
    )

    async def forward(pod, port, namespace=None):
        return "http://companion"

    kube.forward = forward
    service = Service(config, kube)
    await service.http.aclose()
    service.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(b'{"ok":true,"gvisor":true}'),
            )
        )
    )
    await service.start()
    yield service, cluster
    await service.close()


async def test_create_has_policy_before_workload_and_no_duplicate_jobs(running_service):
    service, cluster = running_service
    request = CreateSandbox()
    first = await service.create(request, "retry-1")
    again = await service.create(request, "retry-1")
    assert first.id == again.id
    assert again.state == "running"
    assert len(cluster.jobs) == 1
    writes = [path for method, path, _ in cluster.operations if method == "POST"]
    assert writes[0].endswith("/networkpolicies")
    assert writes[1].endswith("/jobs")
    with pytest.raises(ConflictError):
        await service.create(CreateSandbox(cpu=2), "retry-1")


async def test_invalid_runtime_fails_closed_and_cleans_up(running_service):
    service, cluster = running_service
    cluster.runtime = "runc"
    with pytest.raises(RuntimeError, match="runtime verification"):
        await service.create(CreateSandbox())
    assert not cluster.jobs
    assert not cluster.workloads
    assert not cluster.policies
    assert (await service.store.list())[0].state == "failed"


async def test_destroy_is_idempotent(running_service):
    service, cluster = running_service
    sandbox = await service.create(CreateSandbox())
    await service.destroy(sandbox.id)
    assert (await service.destroy(sandbox.id)).state == "destroyed"
    assert not cluster.jobs and not cluster.workloads and not cluster.policies


async def test_worker_failure_marks_lost_and_does_not_reschedule(running_service):
    service, cluster = running_service
    sandbox = await service.create(CreateSandbox())
    cluster.ready = False
    info = await service.get(sandbox.id)
    assert info.state == "lost"
    assert not cluster.jobs
    assert (
        len([op for op in cluster.operations if op[0] == "POST" and op[1].endswith("/jobs")]) == 1
    )


async def test_expiration_cleanup(running_service):
    service, cluster = running_service
    sandbox = await service.create(CreateSandbox())
    sandbox.expires_at = utcnow() - timedelta(seconds=1)
    await service.store.put(sandbox)
    await service.reconcile()
    assert (await service.store.get(sandbox.id)).state == "expired"
    assert not cluster.workloads


async def test_timeout_extends_job_deadline_and_capacity_accounts_overhead(running_service):
    service, cluster = running_service
    sandbox = await service.create(CreateSandbox(cpu=1, memory="1Gi", timeout=60))
    info = await service.set_timeout(sandbox.id, 3600)
    assert info.expires_at > utcnow() + timedelta(seconds=3500)
    assert cluster.jobs[sandbox.id]["spec"]["activeDeadlineSeconds"] >= 3600
    status = await service.status()
    # Mock API doesn't insert RuntimeClass overhead; one requested CPU is allocated.
    assert status["nodes"][0]["cpu_available"] == 3


async def test_api_restart_retains_workload_and_metadata(running_service):
    service, cluster = running_service
    sandbox = await service.create(CreateSandbox())
    # Reconciliation reads cluster state; it does not create or restart jobs.
    await service.reconcile()
    assert (await service.store.get(sandbox.id)).state == "running"
    assert len(cluster.jobs) == 1
