"""Short-lived read-only registry capabilities for isolated Dockerfile builders.

Actual upstream credentials remain on the head. A build may read authorized base
images through its temporary capability, but never learns the registry password.
"""

from __future__ import annotations

import re
import secrets
import ssl
import time
from urllib.parse import urlparse
from urllib.request import parse_http_list, parse_keqv_list

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from opensandbox.config import RegistryAuth, Settings
from opensandbox.errors import AuthenticationError, RuntimeError, ValidationError


class RegistryProxy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.grants: dict[str, tuple[str, RegistryAuth, float]] = {}

    def grant(self, host: str, credential: RegistryAuth) -> str:
        token = secrets.token_urlsafe(32)
        self.grants[token] = (host, credential, time.monotonic() + 1800)
        return token

    def revoke(self, tokens):
        for token in tokens:
            self.grants.pop(token, None)

    async def relay(self, token: str, path: str, request: Request):
        grant = self.grants.get(token)
        if not grant or grant[2] <= time.monotonic():
            raise AuthenticationError("Build image capability is invalid or expired")
        host, credential, _ = grant
        if not re.fullmatch(
            r"[a-z0-9._/-]+/(manifests|blobs)/[A-Za-z0-9_.:+-]+", path
        ) or ".." in path.split("/"):
            raise ValidationError("Only image manifests and blobs may be read")
        upstream_host = "registry-1.docker.io" if host in {"docker.io", "index.docker.io"} else host
        verify = (
            ssl.create_default_context(cafile=str(self.settings.registry_ca))
            if host == self.settings.registry and self.settings.registry_ca
            else True
        )
        client = httpx.AsyncClient(
            verify=verify,
            timeout=60,
            trust_env=False,
            follow_redirects=True,
            event_hooks={"request": [require_https]},
        )
        url = f"https://{upstream_host}/v2/{path}"
        headers = {"Accept": request.headers.get("accept", "*/*")}
        try:
            # Challenge first. Don't send a long-lived password pre-emptively.
            response = await client.send(
                client.build_request(request.method, url, headers=headers), stream=True
            )
            if response.status_code == 401:
                challenge = response.headers.get("www-authenticate", "")
                await response.aclose()
                scheme, _, parameters = challenge.partition(" ")
                if scheme.lower() == "basic":
                    response = await client.send(
                        client.build_request(request.method, url, headers=headers),
                        auth=(credential.username, credential.password.get_secret_value()),
                        stream=True,
                    )
                elif scheme.lower() == "bearer":
                    values = parse_keqv_list(parse_http_list(parameters))
                    realm = values.get("realm", "")
                    allowed = credential.token_realm
                    realm_url = urlparse(realm)
                    trusted_host = (
                        "auth.docker.io"
                        if upstream_host == "registry-1.docker.io"
                        else upstream_host
                    )
                    if realm_url.scheme != "https" or realm_url.username or realm_url.password:
                        raise RuntimeError("Registry returned an invalid token endpoint")
                    if (allowed and realm != allowed) or (
                        not allowed and realm_url.netloc != trusted_host
                    ):
                        raise RuntimeError(
                            "Configure token_realm for this registry authentication service"
                        )
                    repository = (
                        path.rsplit("/manifests/", 1)[0]
                        if "/manifests/" in path
                        else path.rsplit("/blobs/", 1)[0]
                    )
                    # Ignore client-supplied scopes and upstream push permissions.
                    auth = await client.get(
                        realm,
                        params={
                            "service": values.get("service", upstream_host),
                            "scope": f"repository:{repository}:pull",
                        },
                        auth=(credential.username, credential.password.get_secret_value()),
                        follow_redirects=False,
                    )
                    auth.raise_for_status()
                    access_token = auth.json().get("token") or auth.json().get("access_token")
                    if not access_token:
                        raise RuntimeError("Registry did not issue a pull token")
                    headers["Authorization"] = "Bearer " + access_token
                    response = await client.send(
                        client.build_request(request.method, url, headers=headers), stream=True
                    )
                else:
                    raise RuntimeError("Registry authentication scheme is unsupported")
            if response.is_error:
                status = response.status_code
                await response.aclose()
                await client.aclose()
                return Response(status_code=status)
        except BaseException:
            await client.aclose()
            raise

        async def body():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "content-encoding", "docker-content-digest", "etag"}
        }
        return StreamingResponse(body(), status_code=response.status_code, headers=headers)


async def require_https(request):
    if request.url.scheme != "https":
        raise RuntimeError("Registry redirects must preserve HTTPS")
