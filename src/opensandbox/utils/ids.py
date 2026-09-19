"""Identifier generation.

UUIDv7 is used for all public identifiers (time-ordered, non-sequential).
Python's standard library only gained ``uuid7`` in 3.14, so a small compliant
implementation is provided here rather than adding a dependency.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid

_last_ts_ms = 0
_last_rand = 0


def uuid7() -> uuid.UUID:
    """Return an RFC 9562 UUIDv7 (48-bit ms timestamp + random)."""
    global _last_ts_ms, _last_rand
    ts_ms = time.time_ns() // 1_000_000
    if ts_ms <= _last_ts_ms:
        # Preserve monotonic ordering within the same millisecond.
        ts_ms = _last_ts_ms
        _last_rand += 1
        rand = _last_rand
    else:
        rand = secrets.randbits(74)
        _last_ts_ms = ts_ms
        _last_rand = rand
    rand_a = (rand >> 62) & 0x0FFF
    rand_b = rand & ((1 << 62) - 1)
    value = (ts_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def new_id(prefix: str | None = None) -> str:
    raw = str(uuid7())
    return f"{prefix}_{raw}" if prefix else raw


def short_id(full_id: str, length: int = 8) -> str:
    """Short display form of an identifier.

    Taken from the *end* of the UUID: the leading 12 hex digits of a UUIDv7 are the
    millisecond timestamp, so a prefix would be identical for everything created in
    the same minute (or the same few hours, for 6 characters). The tail is random.
    """
    body = full_id.split("_", 1)[-1] if "_" in full_id else full_id
    return body.replace("-", "")[-length:]


def new_token(nbytes: int = 32) -> str:
    """Cryptographically random URL-safe token (>= 256 bits by default)."""
    return secrets.token_urlsafe(nbytes)


def new_nonce() -> str:
    return os.urandom(8).hex()
