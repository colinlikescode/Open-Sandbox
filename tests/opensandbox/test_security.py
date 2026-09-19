"""Authentication, owner isolation and explicit unsupported-feature boundaries."""

import httpx
import pytest

from opensandbox.api import create_app
from opensandbox.auth import key_hash
from opensandbox.errors import RuntimeError
from opensandbox.models import CreateSandbox


async def test_keys_are_hashed_revocable_and_owner_scoped(running_service):
    service, _ = running_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, manage_lifecycle=False)),
        base_url="http://head",
    ) as http:
        admin = {"X-API-Key": "a" * 40}
        first = (await http.post("/v1/keys", headers=admin, json={"name": "alice"})).json()
        second = (await http.post("/v1/keys", headers=admin, json={"name": "bob"})).json()
        assert first["key"].startswith("e2b_")
        alice, bob = {"X-API-Key": first["key"]}, {"X-API-Key": second["key"]}
        assert first["key"] not in (await http.get("/v1/keys", headers=admin)).text
        async with service.store.db.execute(
            "SELECT hash FROM api_keys WHERE id=?", (first["id"],)
        ) as cur:
            assert (await cur.fetchone())[0] == key_hash(first["key"])
        assert (
            await http.post("/v1/keys", headers=alice, json={"name": "escalate", "admin": True})
        ).status_code == 401
        sandbox = (
            await http.post("/v2/sandboxes", headers=alice, json={"templateID": "base"})
        ).json()
        sid = sandbox["sandboxID"]
        assert (await http.get("/v2/sandboxes", headers=bob)).json() == []
        for method, path, body in [
            ("GET", f"/sandboxes/{sid}", None),
            ("DELETE", f"/sandboxes/{sid}", None),
            ("POST", f"/v2/sandboxes/{sid}/connect", {}),
            ("POST", f"/sandboxes/{sid}/timeout", {"timeout": 60}),
            ("GET", f"/v1/sandboxes/{sid}", None),
        ]:
            assert (await http.request(method, path, headers=bob, json=body)).status_code == 404
        routing = {"E2b-Sandbox-Id": sid, "E2b-Sandbox-Port": "49983"}
        assert (await http.get("/health", headers=routing)).status_code == 401
        routing["X-Access-Token"] = sandbox["envdAccessToken"]
        assert (await http.get("/health", headers=routing)).status_code == 204
        assert (await http.delete("/v1/keys/" + first["id"], headers=admin)).status_code == 204
        assert (await http.get("/sandboxes/" + sid, headers=alice)).status_code == 401
        assert (await http.get("/health", headers=routing)).status_code == 401
        assert (await http.delete("/v1/keys/bootstrap", headers=admin)).status_code == 409


async def test_fail_closed_when_kernel_attestation_is_missing(running_service):
    service, cluster = running_service
    await service.http.aclose()
    service.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True, "gvisor": False})
        )
    )
    with pytest.raises(RuntimeError, match="runtime verification"):
        await service.create(CreateSandbox())
    assert not cluster.jobs and not cluster.workloads and not cluster.policies


async def test_unsupported_lifecycle_and_network_are_explicit(running_service):
    service, _ = running_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, manage_lifecycle=False)),
        base_url="http://head",
        headers={"X-API-Key": "a" * 40},
    ) as http:
        for body in [
            {"autoPause": True},
            {"volumeMounts": [{"name": "volume", "path": "/data"}]},
            {"network": {"allowOut": ["example.com"]}},
        ]:
            response = await http.post("/v2/sandboxes", json={"templateID": "base", **body})
            assert response.status_code == 501
            assert response.json()["code"] == 501
        assert (await http.post("/v2/sandboxes", json={"templateID": "missing"})).status_code == 404


async def test_private_ports_require_scoped_traffic_token(running_service):
    service, _ = running_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, manage_lifecycle=False)),
        base_url="http://head",
        headers={"X-API-Key": "a" * 40},
    ) as http:
        sandbox = (
            await http.post(
                "/v2/sandboxes",
                json={"templateID": "base", "network": {"allowPublicTraffic": False}},
            )
        ).json()
        route = {"E2b-Sandbox-Id": sandbox["sandboxID"], "E2b-Sandbox-Port": "3000"}
        assert (await http.get("/", headers=route)).status_code == 401
        route["e2b-traffic-access-token"] = sandbox["trafficAccessToken"]
        assert (await http.get("/", headers=route)).status_code == 200
        route["E2b-Sandbox-Port"] = "49321"
        assert (await http.get("/health", headers=route)).status_code == 502


async def test_template_resources_and_image_architecture_reach_kubernetes(running_service):
    service, cluster = running_service
    await service.store.image(
        "registry/agent@sha256:test",
        {"reference": "registry/agent@sha256:test", "architecture": "arm64"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, manage_lifecycle=False)),
        base_url="http://head",
        headers={"X-API-Key": "a" * 40},
    ) as http:
        assert (
            await http.put(
                "/v1/templates/python-agent",
                json={"image": "registry/agent@sha256:test", "cpu": 2, "memory": "2Gi"},
            )
        ).status_code == 200
        response = await http.post("/v2/sandboxes", json={"templateID": "python-agent"})
        assert response.status_code == 201
        job = cluster.jobs[response.json()["sandboxID"]]
        pod = job["spec"]["template"]["spec"]
        assert pod["nodeSelector"]["kubernetes.io/arch"] == "arm64"
        assert pod["containers"][0]["resources"]["limits"]["cpu"] == "2000m"
        assert (await http.delete("/v1/templates/base")).status_code == 409


async def test_metrics_and_admin_routes_require_admin_key(running_service):
    service, _ = running_service
    key = await service.store.create_key("application")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, manage_lifecycle=False)),
        base_url="http://head",
    ) as http:
        assert (await http.get("/metrics", headers={"X-API-Key": key["key"]})).status_code == 401
        response = await http.get("/metrics", headers={"X-API-Key": "a" * 40})
        assert response.status_code == 200
        assert 'opensandbox_node_cpu{node="worker"} 4.0' in response.text
