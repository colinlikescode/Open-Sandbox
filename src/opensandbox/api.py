"""Authenticated central HTTP API and signed HTTP/WebSocket sandbox routing."""

from __future__ import annotations

import asyncio
import contextlib
import re
import ssl
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from opensandbox import __version__
from opensandbox.auth import identity, owner_id, require_admin
from opensandbox.cluster.manifests import AGENT_PORT
from opensandbox.config import Settings
from opensandbox.errors import ConflictError, OpenSandboxError, ValidationError
from opensandbox.models import CommandRequest, CreateSandbox, Model, TimeoutRequest
from opensandbox.service import Service
from opensandbox.utils.clock import utcnow
from opensandbox.utils.proxy import build_target, proxy_http, proxy_websocket, websocket_url


class KeyRequest(Model):
    name: str
    admin: bool = False


class TemplateRequest(Model):
    image: str
    cpu: float = 1
    memory: str = "1Gi"
    disk: str = "10Gi"
    workdir: str = "/workspace"


def create_app(service: Service, *, manage_lifecycle: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app):
        if manage_lifecycle:
            await service.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await service.close()

    app = FastAPI(
        title="OpenSandbox",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    from opensandbox.images import ImageBuilder

    builder = ImageBuilder(service)

    @app.exception_handler(OpenSandboxError)
    async def domain_error(request, exc):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.to_dict(), "code": exc.http_status, "message": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's input payload can contain secret env values. Don't echo it.
        message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "invalid_request", "message": message},
                "code": 422,
                "message": message,
            },
        )

    async def authenticate(request: Request):
        key = request.headers.get("x-api-key", "")
        if not key:
            key = request.headers.get("authorization", "").removeprefix("Bearer ")
        principal = await service.store.authenticate(key)
        token = identity.set(principal)
        try:
            yield principal
        finally:
            identity.reset(token)

    auth = [Depends(authenticate)]
    admin_auth = [Depends(authenticate), Depends(require_admin)]

    @app.get("/v1/health")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/openapi.json", dependencies=auth)
    async def openapi():
        return app.openapi()

    @app.post("/v1/sandboxes", dependencies=auth, status_code=201)
    async def create(
        body: CreateSandbox, idempotency_key: str | None = Header(default=None, max_length=256)
    ):
        return await service.create(body, idempotency_key)

    @app.get("/v1/sandboxes", dependencies=auth)
    async def list_sandboxes():
        await service.reconcile()
        principal = identity.get()
        assert principal is not None
        return [s for s in await service.store.list() if principal.admin or s.owner == principal.id]

    @app.get("/v1/sandboxes/{sandbox_id}", dependencies=auth)
    async def get(sandbox_id: str):
        return await service.get(sandbox_id)

    @app.delete("/v1/sandboxes/{sandbox_id}", dependencies=auth)
    async def destroy(sandbox_id: str):
        return await service.destroy(sandbox_id)

    @app.put("/v1/sandboxes/{sandbox_id}/timeout", dependencies=auth)
    async def timeout(sandbox_id: str, body: TimeoutRequest):
        return await service.set_timeout(sandbox_id, body.timeout)

    @app.post("/v1/sandboxes/{sandbox_id}/commands", dependencies=auth)
    async def command(sandbox_id: str, body: CommandRequest):
        start = time.monotonic()
        response = await service.agent(
            sandbox_id, "POST", "/commands", json=body.model_dump(exclude_none=True)
        )
        await service.store.count("commands_started_total")
        await service.store.count("command_request_seconds_total", time.monotonic() - start)
        return response.json()

    @app.get("/v1/sandboxes/{sandbox_id}/commands", dependencies=auth)
    async def commands(sandbox_id: str):
        return (await service.agent(sandbox_id, "GET", "/commands")).json()

    @app.get("/v1/sandboxes/{sandbox_id}/commands/{command_id}", dependencies=auth)
    async def command_status(sandbox_id: str, command_id: str):
        return (await service.agent(sandbox_id, "GET", f"/commands/{command_id}")).json()

    @app.delete("/v1/sandboxes/{sandbox_id}/commands/{command_id}", dependencies=auth)
    async def kill_command(sandbox_id: str, command_id: str):
        return (await service.agent(sandbox_id, "DELETE", f"/commands/{command_id}")).json()

    @app.get("/v1/sandboxes/{sandbox_id}/commands/{command_id}/events", dependencies=auth)
    async def events(
        request: Request, sandbox_id: str, command_id: str, from_seq: int = Query(default=0, ge=0)
    ):
        endpoint = await service.endpoint(sandbox_id)
        return await proxy_http(
            request, f"{endpoint}/commands/{command_id}/events?from_seq={from_seq}", service.http
        )

    async def transfer(request: Request, sandbox_id: str, kind: str, path: str):
        if not path.startswith("/") or "\0" in path:
            raise ValidationError("path must be absolute")
        endpoint = await service.endpoint(sandbox_id)

        async def bounded_body():
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > service.settings.max_upload_bytes:
                    raise ValidationError("Transfer exceeds the configured upload limit")
                yield chunk

        upstream_request = service.http.build_request(
            request.method,
            endpoint + "/" + kind,
            params={"path": path},
            content=bounded_body() if request.method == "PUT" else None,
        )
        upstream = await service.http.send(upstream_request, stream=True)
        if upstream.is_error:
            error_body = await upstream.aread()
            await upstream.aclose()
            return Response(
                error_body, status_code=upstream.status_code, media_type="application/json"
            )

        async def body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/octet-stream"),
        )

    @app.api_route("/v1/sandboxes/{sandbox_id}/files", methods=["GET", "PUT"], dependencies=auth)
    async def files(request: Request, sandbox_id: str, path: str):
        return await transfer(request, sandbox_id, "files", path)

    @app.api_route("/v1/sandboxes/{sandbox_id}/archive", methods=["GET", "PUT"], dependencies=auth)
    async def archive(request: Request, sandbox_id: str, path: str):
        return await transfer(request, sandbox_id, "archive", path)

    @app.post("/v1/sandboxes/{sandbox_id}/ports/{port}", dependencies=auth)
    async def port_url(sandbox_id: str, port: int, ttl: int = Query(default=3600, ge=1, le=86400)):
        if not 1 <= port <= 65535 or port == AGENT_PORT:
            raise ValidationError("port must be 1-65535 and cannot be the companion port")
        info = await service.get(sandbox_id)
        if info.state != "running":
            raise ValidationError(f"Sandbox is {info.state}")
        expires = min(info.expires_at, utcnow() + timedelta(seconds=ttl))
        token = service.signer.sign(sandbox_id, port, expires)
        return {"url": f"{service.settings.endpoint.rstrip('/')}/p/{token}/", "expires_at": expires}

    @app.api_route(
        "/p/{token}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def http_proxy(request: Request, token: str, path: str):
        claims = service.signer.verify(token)
        target = await service.endpoint(claims.sandbox_id, claims.port)
        return await proxy_http(
            request, build_target(target, path, request.url.query), service.http
        )

    @app.websocket("/p/{token}/{path:path}")
    async def websocket_proxy(websocket: WebSocket, token: str, path: str):
        try:
            claims = service.signer.verify(token)
            target = await service.endpoint(claims.sandbox_id, claims.port)
        except OpenSandboxError:
            await websocket.close(code=1008)
            return
        url = websocket_url(build_target(target, path, websocket.url.query))
        await proxy_websocket(websocket, url)

    @app.api_route("/v2/", methods=["GET", "HEAD"])
    async def registry_ping():
        return Response(
            "{}",
            media_type="application/json",
            headers={"Docker-Distribution-API-Version": "registry/2.0"},
        )

    @app.api_route("/v2/opensandbox-build/{token}/{path:path}", methods=["GET", "HEAD"])
    async def build_registry(request: Request, token: str, path: str):
        return await builder.proxy.relay(token, path, request)

    @app.post("/v1/images/build", dependencies=admin_auth)
    async def build_image(
        request: Request, dockerfile: str = "Dockerfile", name: str | None = None
    ):
        if name and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", name):
            raise ValidationError(
                "Template name must be 1-63 lowercase letters, digits, dots, dashes or underscores"
            )
        service.settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=service.settings.state_dir) as directory:
            context = Path(directory) / "context.tar"
            size = 0
            with context.open("wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > service.settings.max_upload_bytes:
                        raise ValidationError("Build context exceeds upload limit")
                    await asyncio.to_thread(output.write, chunk)
            result = await builder.build(context, dockerfile)
            if name:
                await service.store.put_template(
                    name, {"image": result["reference"], "cpu": 1, "memory": "1Gi", "disk": "10Gi"}
                )
                result["name"] = name
            return result

    @app.get("/v1/images", dependencies=auth)
    async def images():
        state = await service.status()
        return {
            "built": await service.store.images(),
            "templates": await service.store.templates(),
            "nodes": [{"name": node["name"], "images": node["images"]} for node in state["nodes"]],
        }

    @app.delete("/v1/images", dependencies=admin_auth, status_code=204)
    async def delete_image(reference: str):
        image = await service.store.image_info(reference)
        if not image:
            raise ValidationError("Only images built by this cluster can be deleted")
        if any(t["image"] == reference for t in await service.store.templates()):
            raise ConflictError("Delete template aliases referencing this image first")
        if any(
            s.image == reference and s.state in {"pending", "running"}
            for s in await service.store.list()
        ):
            raise ConflictError("Image is still used by an active sandbox")
        prefix = service.settings.registry + "/"
        if not reference.startswith(prefix) or "@sha256:" not in reference:
            raise ValidationError("Image does not belong to the cluster registry")
        repository, digest = reference[len(prefix) :].split("@", 1)
        verify = (
            ssl.create_default_context(cafile=str(service.settings.registry_ca))
            if service.settings.registry_ca
            else True
        )
        async with httpx.AsyncClient(verify=verify, trust_env=False) as client:
            response = await client.delete(
                f"https://{service.settings.registry}/v2/{repository}/manifests/{digest}",
                auth=(
                    service.settings.registry_username,
                    service.settings.registry_password.get_secret_value(),
                ),
            )
            if response.status_code != 404:
                response.raise_for_status()
        await service.store.delete_image(reference)
        await service.store.audit(owner_id(), "image.delete", reference)
        return Response(status_code=204)

    @app.get("/v1/status", dependencies=admin_auth)
    async def status():
        return await service.status()

    @app.get("/v1/nodes", dependencies=admin_auth)
    async def nodes():
        return (await service.status())["nodes"]

    @app.get("/v1/metrics", dependencies=admin_auth)
    async def metrics():
        state = await service.status()
        try:
            utilization = await service.kube.request("GET", "/apis/metrics.k8s.io/v1beta1/nodes")
        except OpenSandboxError:
            utilization = {"items": [], "error": "Node utilization metrics unavailable"}
        return {
            "counters": await service.store.counters(),
            "allocation": state["nodes"],
            "utilization": utilization,
        }

    @app.post("/v1/doctor", dependencies=admin_auth)
    async def doctor(request: Request):
        from opensandbox.doctor import diagnose

        key = request.headers.get("x-api-key") or request.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ")
        return await diagnose(service, key)

    @app.get("/metrics", dependencies=admin_auth)
    async def prometheus_metrics():
        from opensandbox.observability import prometheus

        return Response(await prometheus(service), media_type="text/plain; version=0.0.4")

    @app.post("/v1/keys", dependencies=admin_auth, status_code=201)
    async def create_key(body: KeyRequest):
        if not 1 <= len(body.name) <= 128:
            raise ValidationError("Key name must be 1-128 characters")
        result = await service.store.create_key(body.name, body.admin)
        await service.store.audit(owner_id(), "key.create", result["id"])
        return result

    @app.get("/v1/keys", dependencies=admin_auth)
    async def list_keys():
        return await service.store.keys()

    @app.delete("/v1/keys/{key_id}", dependencies=admin_auth, status_code=204)
    async def revoke_key(key_id: str):
        await service.store.revoke_key(key_id)
        await service.store.audit(owner_id(), "key.revoke", key_id)
        return Response(status_code=204)

    @app.put("/v1/templates/{name}", dependencies=admin_auth)
    async def register_template(name: str, body: TemplateRequest):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", name):
            raise ValidationError("Invalid template name")
        CreateSandbox(**body.model_dump())
        await service.store.put_template(name, body.model_dump())
        return await service.store.template(name)

    @app.get("/v1/templates", dependencies=auth)
    async def list_templates():
        return await service.store.templates()

    @app.delete("/v1/templates/{name}", dependencies=admin_auth, status_code=204)
    async def delete_template(name: str):
        await service.store.delete_template(name)
        return Response(status_code=204)

    from opensandbox.e2b import RuntimeGateway, control_router

    app.include_router(control_router(service, authenticate))
    app.add_middleware(RuntimeGateway, service=service)
    from opensandbox.observability import RequestMetrics

    app.add_middleware(RequestMetrics, service=service)
    return app


def main():
    import uvicorn

    from opensandbox.observability import configure_logging

    configure_logging()
    settings = Settings.load()
    # Signed service URLs contain bearer capabilities; do not log their paths.
    uvicorn.run(
        create_app(Service(settings)),
        host=settings.host,
        port=settings.port,
        access_log=False,
        ssl_certfile=str(settings.tls_cert) if settings.tls_cert else None,
        ssl_keyfile=str(settings.tls_key) if settings.tls_key else None,
    )


if __name__ == "__main__":
    main()
