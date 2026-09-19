import httpx
import pytest

from opensandbox.errors import ValidationError
from opensandbox.models import SandboxInfo
from opensandbox.sdk.async_client import AsyncOpenSandbox, AsyncSandbox
from opensandbox.utils.clock import utcnow


async def test_download_enforces_limit_while_streaming_and_closes_response(monkeypatch):
    import opensandbox.sdk.async_client as module

    class Stream(httpx.AsyncByteStream):
        closed = False
        count = 0

        async def __aiter__(self):
            for _ in range(100):
                self.count += 1
                yield b"four"

        async def aclose(self):
            self.closed = True

    stream = Stream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(module, "MAX_TRANSFER", 8)
    async with AsyncOpenSandbox("http://head", "secret", transport=transport) as client:
        info = SandboxInfo(
            id="sb-test",
            image="base",
            cpu=1,
            memory=1024,
            disk=1024,
            created_at=utcnow(),
            expires_at=utcnow(),
        )
        sandbox = AsyncSandbox(client, info)
        with pytest.raises(ValidationError, match="Download exceeds"):
            await sandbox.read("/large", binary=True)
    assert stream.count == 3
    assert stream.closed
