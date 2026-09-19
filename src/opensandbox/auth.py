"""API-key identities and request scope, independent of either public API."""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass

from opensandbox.errors import AuthenticationError, NotFoundError


@dataclass(frozen=True)
class Principal:
    id: str
    admin: bool = False


identity: ContextVar[Principal | None] = ContextVar("identity", default=None)


def key_hash(key: str) -> str:
    # Keys contain 256 random bits, so a slow password KDF adds no guessing resistance.
    return hashlib.sha256(key.encode()).hexdigest()


def owner_id() -> str:
    principal = identity.get()
    return principal.id if principal else "system"


def require_owner(owner: str) -> None:
    principal = identity.get()
    if principal is not None and not principal.admin and owner != principal.id:
        raise NotFoundError("Sandbox not found")


def require_admin() -> None:
    principal = identity.get()
    if principal is not None and not principal.admin:
        raise AuthenticationError("An administrator API key is required")
