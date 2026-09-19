# E2B compatibility

Target: published, unmodified **Python e2b 2.51.0** and **npm e2b 2.51.0**.
Configure `E2B_API_KEY`, `E2B_API_URL` and `E2B_SANDBOX_URL`; no SDK patch is used.
The adapter implements the envd 0.5.7 protocol surface listed below. OpenSandbox
runs its own companion, not E2B's envd binary.

| Feature | Official SDK methods (TS / Python) | Status | Integration coverage |
| --- | --- | --- | --- |
| Lifecycle | `Sandbox.create`, `connect`, `list`, `getInfo/get_info`, `isRunning/is_running`, `setTimeout/set_timeout`, `kill` | Supported | Both official SDK suites |
| Commands | `commands.run`, output callbacks, exit codes, envs, cwd | Supported | Both suites |
| Processes | background, `commands.list`, `kill`, stdin, close stdin | Supported | Both suites |
| Command reconnect | `commands.connect` | Supported; bounded retained output | Both official SDK suites |
| Files | `read`, `write`, `list`, `exists`, `getInfo/get_info`, `makeDir/make_dir`, `rename`, `remove` | Supported | Both suites, text/binary and missing-file errors |
| Uploads/downloads | SDK multipart and octet-stream `/files` | Supported; 512 MiB bound | Both suites and companion tests |
| Terminals | `pty.create`, input, `resize`, `kill`, `connect` | Supported | Both suites, including reconnect |
| Port hosts | `getHost/get_host` | Supported; wildcard hostname points to head | Both suites; Host routing locally, real DNS in cluster tests |
| HTTP/WebSocket apps | head port proxy | Supported | HTTP in both suites; WebSocket integration test |
| Templates | default `base`, named template passed to `create` | Supported, mapped to OCI images | Template/resource contract test |
| Resource selection | template defaults; native API CPU/RAM/disk | Supported | Kubernetes manifest/resource tests and live placement test |
| Internet/private ingress | `allowInternetAccess/allow_internet_access`, `network.allowPublicTraffic` | Supported | Policy and private-token tests; live doctor |
| Users | default root, explicit same user | Supported; other users rejected | Companion permission checks |
| Pause/resume, snapshots, fork | lifecycle and snapshot methods | Unsupported; explicit error | Unsupported-feature tests |
| Persistent volumes, IAM, managed MCP | creation options | Unsupported; explicit error | Unsupported-feature tests |
| Filesystem watch and metadata | watch methods, metadata options | Unsupported | Protocol returns unimplemented / SDK version gate |
| Advanced egress rules, HTTPS upstream ports | network options | Unsupported; explicit error | Network option rejection test |
| E2B template build API / code interpreter | hosted template API, notebook kernels | Outside this compatibility surface | Use `opensandbox image build`; no notebook API claim |

Local integration: [test_e2b.py](../tests/opensandbox/test_e2b.py) runs published
SDKs against the real HTTP server and real Go companion. Only Kubernetes and
kernel attestation are simulated. [TypeScript scenario](../sdk/typescript/test/e2b.integration.mjs)
also runs directly against an installed cluster. [Cluster acceptance](../tests/cluster/test_acceptance.py)
uses actual scheduling, gVisor, DNS and port routing.

HTTPS uses normal certificate verification. Tests generate a CA and leaf
certificate. On macOS the Python HTTPS case requires Keychain trust and is
explicitly skipped without modifying the machine's trust store. Linux CI runs
that case; TypeScript HTTPS is tested locally using `NODE_EXTRA_CA_CERTS`.

Compatibility means this documented surface, not every E2B service or future SDK
release. Pin SDK versions until a newer release passes these scenarios.

## Sources inspected

- [Official E2B OpenAPI](https://github.com/e2b-dev/E2B/blob/ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b/spec/openapi.yml)
- [TypeScript SDK](https://github.com/e2b-dev/E2B/tree/ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b/packages/js-sdk)
- [Python SDK](https://github.com/e2b-dev/E2B/tree/ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b/packages/python-sdk)
- [Runtime protocol definitions](https://github.com/e2b-dev/E2B/tree/ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b/spec/envd)
- [envd implementation and local development instructions](https://github.com/e2b-dev/infra/tree/fad70f393e800cee0278669a63976c3aaa00871b/packages/envd)
