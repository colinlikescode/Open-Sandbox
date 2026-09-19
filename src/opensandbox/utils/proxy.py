"""HTTP and WebSocket reverse-proxy helpers shared by the worker and control plane."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from opensandbox.errors import NetworkProxyError

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "proxy-connection",
}

_STRIP_REQUEST_HEADERS = HOP_BY_HOP | {
    "host",
    "content-length",
    "authorization",
    "x-api-key",
    "x-access-token",
    "e2b-sandbox-id",
    "e2b-sandbox-port",
    "e2b-traffic-access-token",
}
_STRIP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}


def filter_request_headers(
    headers: Mapping[str, str], *, keep_authorization: bool = False
) -> dict[str, str]:
    strip = _STRIP_REQUEST_HEADERS - ({"authorization"} if keep_authorization else set())
    return {k: v for k, v in headers.items() if k.lower() not in strip}


def filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_RESPONSE_HEADERS}


async def proxy_http(
    request: Request,
    target_url: str,
    client: httpx.AsyncClient,
    *,
    extra_headers: Mapping[str, str] | None = None,
    keep_authorization: bool = False,
) -> Response:
    """Forward ``request`` to ``target_url`` and stream the response back."""
    headers = filter_request_headers(dict(request.headers), keep_authorization=keep_authorization)
    if extra_headers:
        headers.update(extra_headers)

    async def body() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            if chunk:
                yield chunk

    has_body = request.method not in {"GET", "HEAD", "OPTIONS"} or request.headers.get(
        "content-length"
    ) not in (None, "0")
    req = client.build_request(
        request.method,
        target_url,
        headers=headers,
        content=body() if has_body else None,
        params=None,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError as exc:
        raise NetworkProxyError(f"Could not connect to upstream service: {exc}") from exc
    except httpx.HTTPError as exc:
        raise NetworkProxyError(f"Proxy request failed: {exc}") from exc

    async def iterate() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    if request.method == "HEAD":
        await upstream.aclose()
        return Response(
            status_code=upstream.status_code, headers=filter_response_headers(upstream.headers)
        )
    return StreamingResponse(
        iterate(),
        status_code=upstream.status_code,
        headers=filter_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


async def proxy_websocket(
    client_ws: WebSocket,
    target_url: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> None:
    """Bidirectionally relay frames between ``client_ws`` and the WebSocket at ``target_url``."""
    subprotocols = client_ws.headers.get("sec-websocket-protocol")
    protocols = [p.strip() for p in subprotocols.split(",")] if subprotocols else None
    try:
        upstream = await websockets.connect(
            target_url,
            additional_headers=dict(extra_headers or {}),
            subprotocols=protocols,  # type: ignore[arg-type]
            open_timeout=10,
            max_size=None,
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await client_ws.close(code=1011, reason="upstream unavailable")
        raise NetworkProxyError(f"WebSocket upstream connection failed: {exc}") from exc

    await client_ws.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await client_ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
                elif message.get("text") is not None:
                    await upstream.send(message["text"])
        except WebSocketDisconnect:
            pass

    async def upstream_to_client() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, bytes | bytearray):
                    await client_ws.send_bytes(bytes(frame))
                else:
                    await client_ws.send_text(frame)
        except Exception:
            pass

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            await upstream.close()
        if client_ws.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await client_ws.close()


def build_target(base: str, path: str, query: str) -> str:
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if query:
        url += "?" + query
    return url


def websocket_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://") :]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://") :]
    return http_url
