"""Stateless signed proxy tokens (HMAC-SHA256)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from opensandbox.errors import AuthenticationError
from opensandbox.utils.clock import utcnow
from opensandbox.utils.ids import new_nonce


@dataclass(frozen=True)
class ProxyClaims:
    sandbox_id: str
    port: int
    expires_at: int
    nonce: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ProxyTokenSigner:
    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(secret.encode()).digest()

    def sign(self, sandbox_id: str, port: int, expires_at: datetime) -> str:
        payload = json.dumps(
            {"s": sandbox_id, "p": int(port), "e": int(expires_at.timestamp()), "n": new_nonce()},
            separators=(",", ":"),
        ).encode()
        sig = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(sig)}"

    def verify(self, token: str, *, now: datetime | None = None) -> ProxyClaims:
        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload = _unb64(payload_b64)
            sig = _unb64(sig_b64)
        except (ValueError, TypeError) as exc:
            raise AuthenticationError("malformed proxy token") from exc
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise AuthenticationError("invalid proxy token signature")
        try:
            data = json.loads(payload)
            claims = ProxyClaims(
                sandbox_id=str(data["s"]),
                port=int(data["p"]),
                expires_at=int(data["e"]),
                nonce=str(data["n"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError("malformed proxy token payload") from exc
        current = int((now or utcnow()).timestamp())
        if claims.expires_at <= current:
            raise AuthenticationError("proxy token expired")
        return claims
