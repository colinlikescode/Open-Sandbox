"""Structured diagnostics and authenticated Prometheus metrics."""

import json
import logging
import time

from opensandbox.service import cpu_quantity
from opensandbox.utils.sizes import parse_bytes


class JSONFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("route", "status", "seconds", "sandbox_id"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # HTTP clients otherwise log full signed URLs and build capability tokens.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class RequestMetrics:
    def __init__(self, app, service):
        self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            elapsed = time.monotonic() - started
            await self.service.store.count("api_requests_total")
            await self.service.store.count("api_request_seconds_total", elapsed)
            if scope.get("path") == "/process.Process/Start":
                await self.service.store.count("commands_started_total")
                await self.service.store.count("command_request_seconds_total", elapsed)
            logging.getLogger("opensandbox.requests").info(
                "request",
                extra={
                    "route": getattr(scope.get("route"), "path", "sandbox-runtime"),
                    "status": status,
                    "seconds": round(elapsed, 6),
                },
            )


async def prometheus(service):
    lines = []

    def metric(name, value, **labels):
        suffix = (
            "{" + ",".join(f"{k}={json.dumps(str(v))}" for k, v in labels.items()) + "}"
            if labels
            else ""
        )
        lines.append(f"opensandbox_{name}{suffix} {float(value)}")

    for name, value in (await service.store.counters()).items():
        metric(name, value)
    state = await service.status()
    for status in ["pending", "running", "failed", "lost", "expired", "destroyed"]:
        metric("sandboxes", sum(s["state"] == status for s in state["sandboxes"]), state=status)
    for node in state["nodes"]:
        for key in [
            "cpu",
            "cpu_available",
            "memory",
            "memory_available",
            "sandboxes",
            "ready",
            "schedulable",
        ]:
            metric("node_" + key, node[key], node=node["name"])
    from opensandbox.errors import OpenSandboxError

    try:
        utilization = await service.kube.request("GET", "/apis/metrics.k8s.io/v1beta1/nodes")
        for node in utilization["items"]:
            metric(
                "node_cpu_usage_cores",
                cpu_quantity(node["usage"]["cpu"]),
                node=node["metadata"]["name"],
            )
            metric(
                "node_memory_usage_bytes",
                parse_bytes(node["usage"]["memory"]),
                node=node["metadata"]["name"],
            )
        metric("utilization_available", 1)
    except OpenSandboxError:
        metric("utilization_available", 0)
    return "\n".join(lines) + "\n"
