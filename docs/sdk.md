# SDKs

Applications should use the official Python or TypeScript **E2B 2.51.0** SDK.
See [setup and examples](../instructions.md#4-use-the-official-e2b-sdks) and the
[compatibility matrix](e2b-compatibility.md).

The optional native Python SDK is included in this repository:

```python
from opensandbox import OpenSandbox

with OpenSandbox() as client:
    with client.create(cpu=1, memory='1Gi', timeout=300) as sandbox:
        print(sandbox.exec('printf hello').stdout)
```

It reads `OPENSANDBOX_ENDPOINT` / `OPENSANDBOX_API_KEY`, falling back to
`E2B_API_URL` / `E2B_API_KEY`. `OPENSANDBOX_CA_CERT` or the constructor's `ca_cert`
sets a custom CA. `AsyncOpenSandbox` offers the same operations asynchronously.
Neither client starts a server on the caller's machine.

The native TypeScript package lives in `sdk/typescript`; build/install it from
the checkout. Its name is `@opensandbox/sdk`; publication to a package registry
is not part of setup. It offers create/get/list, commands, file transfers,
signed port URLs, image builds, diagnostics and metrics.

Native downloads extract archive contents beneath the destination directory.
An archive contains the remote basename. Both SDKs enforce bounded transfers and
reject links/path traversal. Local Dockerfile builds upload their context to the
head; compilation happens in a gVisor sandbox on the cluster.
