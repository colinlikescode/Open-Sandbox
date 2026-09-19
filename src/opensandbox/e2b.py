"""E2B control and runtime adapters, verified against unmodified SDKs.

The control adapter translates E2B contracts into domain models. The runtime
gateway authenticates and routes Connect JSON and file transfers to the companion.
Neither adapter schedules Kubernetes workloads.
"""

from __future__ import annotations

# E2B wire field names are intentionally preserved in this adapter.
# ruff: noqa: N815
import hashlib
import hmac
import math
import re
from typing import Any
from urllib.parse import parse_qsl, unquote

from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from opensandbox.auth import identity
from opensandbox.cluster.manifests import AGENT_PORT
from opensandbox.errors import AuthenticationError, NotFoundError, OpenSandboxError
from opensandbox.models import CreateSandbox, Model, Network, SandboxInfo
from opensandbox.service import Service
from opensandbox.utils.proxy import build_target, proxy_http, proxy_websocket, websocket_url

ENVD_VERSION = "0.5.7"  # Implemented protocol level, not a claim to run upstream envd.
ENVD_PORT = 49983


class UnsupportedError(OpenSandboxError):
    code = "unsupported"
    http_status = 501


class NewSandbox(Model):
    templateID: str = "base"
    timeout: int = Field(default=300, ge=1, le=604800)
    metadata: dict[str, str] = Field(default_factory=dict)
    envVars: dict[str, str] = Field(default_factory=dict)
    allow_internet_access: bool = True
    network: dict[str, Any] | None = None
    autoPause: bool = False
    autoPauseMemory: bool | None = None
    autoResume: dict[str, bool] | None = None
    secure: bool = True
    mcp: dict | None = None
    iam: dict | None = None
    volumeMounts: list[dict] | None = None


class ConnectSandbox(Model):
    timeout: int | None = Field(default=None, ge=1, le=604800)
    memory: bool | None = None
    autoPause: bool = False


class SandboxTimeout(Model):
    timeout: int = Field(ge=1, le=604800)


def access_token(service: Service, sandbox_id: str, purpose: str = "runtime") -> str:
    return hmac.new(
        service.settings.api_key.get_secret_value().encode(),
        f"e2b:{purpose}:{sandbox_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def sandbox_response(service: Service, info: SandboxInfo) -> dict:
    return {
        "sandboxID": info.id,
        "templateID": info.template,
        "alias": info.template,
        "clientID": "opensandbox",
        "envdVersion": ENVD_VERSION,
        "envdAccessToken": access_token(service, info.id),
        "trafficAccessToken": None
        if info.public_traffic
        else access_token(service, info.id, "traffic"),
        "domain": service.settings.sandbox_domain,
        "startedAt": info.created_at.isoformat(),
        "endAt": info.expires_at.isoformat(),
        "cpuCount": math.ceil(info.cpu),
        "memoryMB": math.ceil(info.memory / 1048576),
        "diskSizeMB": math.ceil(info.disk / 1048576),
        "state": "running",
        "metadata": info.metadata,
        "allowInternetAccess": info.internet,
        "network": {"allowPublicTraffic": info.public_traffic},
        "lifecycle": {"autoResume": False, "onTimeout": "kill"},
        "volumeMounts": [],
    }


def control_router(service: Service, authenticate) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authenticate)])

    async def running(sandbox_id: str):
        info = await service.get(sandbox_id)
        if info.state != "running":
            raise NotFoundError("Sandbox is no longer running")
        return info

    @router.post("/v2/sandboxes", status_code=201)
    @router.post("/sandboxes", status_code=201)
    async def create(body: NewSandbox):
        if body.autoPause or (body.autoResume and body.autoResume.get("enabled")):
            raise UnsupportedError("Pause and resume are not supported; sandboxes are ephemeral")
        if body.mcp or body.iam or body.volumeMounts:
            raise UnsupportedError(
                "Managed MCP, workload identity and volume mounts are not supported"
            )
        net = body.network or {}
        unsupported = {k for k, value in net.items() if k != "allowPublicTraffic" and value}
        if unsupported:
            raise UnsupportedError("Unsupported network options: " + ", ".join(sorted(unsupported)))
        template = await service.store.template(body.templateID)
        info = await service.create(
            CreateSandbox(
                template=body.templateID,
                image=template["image"],
                cpu=template.get("cpu", 1),
                memory=template.get("memory", "1Gi"),
                disk=template.get("disk", "10Gi"),
                timeout=body.timeout,
                env=body.envVars,
                metadata=body.metadata,
                workdir=template.get("workdir", "/workspace"),
                network=Network(internet=body.allow_internet_access),
            )
        )
        info.public_traffic = net.get("allowPublicTraffic", True)
        await service.store.put(info)
        await service.store.audit(info.owner, "sandbox.create", info.id)
        return sandbox_response(service, info)

    @router.get("/sandboxes/{sandbox_id}")
    async def info(sandbox_id: str):
        return sandbox_response(service, await running(sandbox_id))

    @router.post("/v2/sandboxes/{sandbox_id}/connect")
    @router.post("/sandboxes/{sandbox_id}/connect")
    async def connect(sandbox_id: str, body: ConnectSandbox):
        if body.memory is False or body.autoPause:
            raise UnsupportedError("Reboot, pause and resume are not supported")
        info = await running(sandbox_id)
        if body.timeout is not None:
            info = await service.set_timeout(sandbox_id, body.timeout)
        return sandbox_response(service, info)

    @router.post("/sandboxes/{sandbox_id}/timeout", status_code=204)
    async def timeout(sandbox_id: str, body: SandboxTimeout):
        await running(sandbox_id)
        await service.set_timeout(sandbox_id, body.timeout)
        return Response(status_code=204)

    @router.delete("/sandboxes/{sandbox_id}", status_code=204)
    async def kill(sandbox_id: str):
        await service.destroy(sandbox_id)
        return Response(status_code=204)

    @router.get("/v2/sandboxes")
    @router.get("/sandboxes")
    async def listing(request: Request):
        await service.reconcile()
        principal = identity.get()
        assert principal is not None
        items = [
            i
            for i in await service.store.list()
            if i.state == "running" and (principal.admin or i.owner == principal.id)
        ]
        params = request.query_params
        if params.get("state") and "running" not in params.get("state", "").split(","):
            items = []
        if params.get("template"):
            items = [i for i in items if i.template == params["template"]]
        for key, value in parse_qsl(params.get("metadata", "")):
            items = [i for i in items if i.metadata.get(unquote(key)) == unquote(value)]
        if params.get("startedAfter"):
            from datetime import datetime

            try:
                after = datetime.fromisoformat(params["startedAfter"].replace("Z", "+00:00"))
                items = [i for i in items if i.created_at > after]
            except (ValueError, TypeError):
                return JSONResponse(
                    {"code": 400, "message": "Invalid startedAfter"}, status_code=400
                )
        items.sort(key=lambda i: i.created_at, reverse=params.get("order", "desc") != "asc")
        try:
            offset = int(params.get("nextToken", "0"))
            limit = int(params.get("limit", "100"))
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            return JSONResponse({"code": 400, "message": "Invalid pagination"}, status_code=400)
        headers = {"x-total-running": str(len(items))}
        if offset + limit < len(items):
            headers["x-next-token"] = str(offset + limit)
        # Access capabilities are only returned by create/connect/info, not lists.
        rows = [sandbox_response(service, item) for item in items[offset : offset + limit]]
        for row in rows:
            row.pop("envdAccessToken", None)
            row.pop("trafficAccessToken", None)
        return JSONResponse(rows, headers=headers)

    @router.post("/sandboxes/{sandbox_id}/pause")
    @router.post("/sandboxes/{sandbox_id}/snapshots")
    @router.post("/sandboxes/{sandbox_id}/fork")
    async def unsupported(sandbox_id: str):
        await service.get(sandbox_id)
        raise UnsupportedError(
            "Pause, snapshots and fork are not supported for ephemeral sandboxes"
        )

    return router


def routing(scope, service: Service):
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    if "e2b-sandbox-id" in headers:
        return headers["e2b-sandbox-id"], headers.get("e2b-sandbox-port", "")
    host = headers.get("host", "").lower()
    domain = service.settings.sandbox_domain.lower()
    if host.endswith("." + domain):
        match = re.fullmatch(r"(\d+)-(sb-[a-f0-9]{32})", host[: -(len(domain) + 1)])
        if match:
            return match[2], match[1]
    return None


class RuntimeGateway:
    """Route by SDK headers or getHost() hostname before control route matching."""

    def __init__(self, app, service: Service):
        self.app, self.service = app, service
        self.runtime = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.runtime.add_api_route(
            "/{path:path}",
            self.http,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        )
        self.runtime.add_api_websocket_route("/{path:path}", self.websocket)

    async def __call__(self, scope, receive, send):
        route = routing(scope, self.service) if scope["type"] in {"http", "websocket"} else None
        if route is None:
            return await self.app(scope, receive, send)
        scope["e2b.route"] = route
        return await self.runtime(scope, receive, send)

    async def target(self, connection):
        sandbox_id, raw_port = connection.scope["e2b.route"]
        if not re.fullmatch(r"sb-[a-f0-9]{32}", sandbox_id) or not raw_port.isdigit():
            raise NotFoundError("Invalid sandbox route")
        port = int(raw_port)
        if not 1 <= port <= 65535 or port == AGENT_PORT:
            raise NotFoundError("Invalid sandbox port")
        info = await self.service.get(sandbox_id)
        if info.owner != "system" and not await self.service.store.key_active(info.owner):
            raise AuthenticationError("Sandbox owner key was revoked")
        if port == ENVD_PORT or not info.public_traffic:
            purpose = "runtime" if port == ENVD_PORT else "traffic"
            header = "x-access-token" if port == ENVD_PORT else "e2b-traffic-access-token"
            token = connection.headers.get(header, "")
            if not hmac.compare_digest(token, access_token(self.service, sandbox_id, purpose)):
                raise AuthenticationError("Invalid sandbox access token")
        if info.state != "running":
            raise NotFoundError("Sandbox is no longer running")
        return await self.service.endpoint(
            sandbox_id, AGENT_PORT if port == ENVD_PORT else port
        ), port

    async def http(self, request: Request, path: str):
        try:
            target, port = await self.target(request)
            if port == ENVD_PORT:
                if path == "health":
                    return Response(status_code=204)
                if path == "files":
                    path = "e2b/files"
                elif not path.startswith(("process.Process/", "filesystem.Filesystem/")):
                    return JSONResponse(
                        {"code": 501, "message": "Unsupported runtime endpoint"}, status_code=501
                    )
            return await proxy_http(
                request,
                build_target(target, path, request.url.query),
                self.service.http,
                keep_authorization=True,
            )
        except OpenSandboxError as exc:
            status = 502 if isinstance(exc, NotFoundError) else exc.http_status
            return JSONResponse({"code": status, "message": str(exc)}, status_code=status)

    async def websocket(self, websocket: WebSocket, path: str):
        try:
            target, port = await self.target(websocket)
            if port == ENVD_PORT:
                await websocket.close(code=1008)
                return
            await proxy_websocket(
                websocket, websocket_url(build_target(target, path, websocket.url.query))
            )
        except OpenSandboxError:
            await websocket.close(code=1008)
