"""Run coroutines from synchronous code on a private background event loop.

The sync SDK is a thin shell over the async SDK; this is the shell.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Iterator
from typing import TypeVar

T = TypeVar("T")


async def _wrap(aw: Awaitable[T]) -> T:
    return await aw


class Portal:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="opensandbox-sdk", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, aw: Awaitable[T]) -> T:
        return asyncio.run_coroutine_threadsafe(_wrap(aw), self._loop).result()

    def iterate(self, agen: AsyncIterator[T]) -> Iterator[T]:
        while True:
            try:
                yield self.call(agen.__anext__())
            except StopAsyncIteration:
                return

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()
