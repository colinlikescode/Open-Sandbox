"""Synchronous wrappers over the same remote protocol as the async client."""

from opensandbox.sdk._portal import Portal
from opensandbox.sdk.async_client import AsyncOpenSandbox


class OpenSandbox:
    def __init__(self, endpoint=None, api_key=None, **kwargs):
        self._client = AsyncOpenSandbox(endpoint, api_key, **kwargs)
        self._portal = Portal()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self._portal._loop.is_closed():
            self._portal.call(self._client.close())
            self._portal.close()

    def create(self, **kwargs):
        return Sandbox(self, self._portal.call(self._client.create(**kwargs)))

    def get(self, sandbox_id):
        return Sandbox(self, self._portal.call(self._client.get(sandbox_id)))

    def list(self):
        return self._portal.call(self._client.list())

    def build(self, path, **kwargs):
        return self._portal.call(self._client.build(path, **kwargs))

    def request(self, method, path, **kwargs):
        return self._portal.call(self._client.request(method, path, **kwargs))

    def images(self):
        return self._portal.call(self._client.images())

    def status(self):
        return self._portal.call(self._client.status())

    def nodes(self):
        return self._portal.call(self._client.nodes())

    def doctor(self):
        return self._portal.call(self._client.doctor())

    def metrics(self):
        return self._portal.call(self._client.metrics())


class Sandbox:
    def __init__(self, client, sandbox):
        self._client, self._sandbox, self.id = client, sandbox, sandbox.id

    @property
    def info(self):
        return self._sandbox.info

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.destroy()

    def exec(self, command, **kwargs):
        return self._client._portal.call(self._sandbox.exec(command, **kwargs))

    def exec_background(self, command, **kwargs):
        return Command(
            self._client,
            self._client._portal.call(self._sandbox.exec_background(command, **kwargs)),
        )

    def write(self, path, content):
        return self._client._portal.call(self._sandbox.write(path, content))

    def read(self, path, **kwargs):
        return self._client._portal.call(self._sandbox.read(path, **kwargs))

    def upload(self, local, remote):
        return self._client._portal.call(self._sandbox.upload(local, remote))

    def download(self, remote, local):
        return self._client._portal.call(self._sandbox.download(remote, local))

    def list_processes(self):
        return self._client._portal.call(self._sandbox.list_processes())

    def kill_process(self, command_id):
        return self._client._portal.call(self._sandbox.kill_process(command_id))

    def get_url(self, port, **kwargs):
        return self._client._portal.call(self._sandbox.get_url(port, **kwargs))

    def set_timeout(self, timeout):
        return self._client._portal.call(self._sandbox.set_timeout(timeout))

    def refresh(self):
        return self._client._portal.call(self._sandbox.refresh())

    def destroy(self):
        return self._client._portal.call(self._sandbox.destroy())


class Command:
    def __init__(self, client, command):
        self._client, self._command, self.id = client, command, command.id

    def wait(self):
        return self._client._portal.call(self._command.wait())

    def kill(self):
        return self._client._portal.call(self._command.kill())

    def stream_logs(self, **kwargs):
        yield from self._client._portal.iterate(self._command.stream_logs(**kwargs))
