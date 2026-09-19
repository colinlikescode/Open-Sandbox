"""Clock abstraction so time-dependent logic (timeouts, idle TTLs) is testable."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Manually advanced clock for deterministic tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2030, 1, 1, tzinfo=UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        # Fake sleeps yield control but do not block; time is advanced explicitly.
        self.advance(seconds)
        await asyncio.sleep(0)
