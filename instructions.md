# OpenSandbox instructions

OpenSandbox turns existing Linux CPU machines into a self-hosted sandbox service.
Run setup **on the head machine**. Applications use the official E2B SDKs; the
OpenSandbox CLI administers your machines, templates and API keys.

## 1. Prepare the machines

Use fresh Linux machines with systemd, kernel 5.10 or newer, and amd64 or arm64
CPUs. Ubuntu 22.04/24.04 or Debian 12 are suitable starting points. Setup checks
architecture, kernel, cgroups, administrative access, unique node identities,
CPU, memory, free space and conflicting ports before installation.

Each machine needs at least 2 CPU cores, 2 GiB RAM and 30 GiB free under `/var/lib`.
**4 cores and 8 GiB RAM are recommended**, especially on the head. Image builds
request 2 cores, 4 GiB RAM and 20 GiB ephemeral disk on one worker, in addition to
system reservations. Have that capacity available when building images.

The head must reach each worker over SSH. The configured SSH user needs
passwordless `sudo` on workers, or SSH as root; the local setup user needs root
or `sudo` on the head.
Use SSH keys and verify each worker's host key through your normal SSH workflow.
Setup enforces known-host verification.

Allow these connections within the cluster:

| Connection | Purpose |
| --- | --- |
| Head → workers TCP 22, or configured SSH port | Setup and administration |
| Workers → head TCP 6443 | K3s API |
| Head → workers TCP 10250 | Kubelet and sandbox transport |
| Nodes ↔ nodes UDP 8472, TCP 4240 and ICMP | Cilium cluster networking and health |
| Nodes → head TCP 30500 | Authenticated image registry |
| Clients → head TCP 7070 | HTTPS API and sandbox applications |
| Image builders → head TCP 7070 | Temporary image pull gateway |

Machines need outbound HTTPS for installation and image downloads. Sandboxes
with internet access use public DNS at 1.1.1.1 and 8.8.8.8. Pod and service CIDRs
default to `10.42.0.0/16` and `10.43.0.0/16`; change them in the inventory if they
conflict with your network. OpenSandbox does not open cloud firewalls for you.

Kubernetes, containerd, gVisor and a Docker daemon do **not** need to be installed.
Setup installs K3s, containerd, gVisor and Cilium. gVisor uses its **systrap**
platform, so the machines do not need KVM or nested virtualization.

## 2. Clone and initialize on the head

```bash
git clone <repo-url> opensandbox
cd opensandbox

# Install uv first if this machine does not have it:
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv sync
cp examples/nodes.yaml nodes.yaml
```

Edit `nodes.yaml` to use your existing machines:

```yaml
head:
  host: 10.0.0.1
  name: head
  sandboxes: true
workers:
  - host: 10.0.0.2
    name: worker-1
  - host: 10.0.0.3
    name: worker-2
ssh:
  user: ubuntu
  key: ~/.ssh/id_ed25519
```

For **one machine**, use `workers: []` and keep `head.sandboxes: true`. For a
**dedicated control-only head**, set `head.sandboxes: false` and provide workers.
Individual nodes can override `ssh_user`, `ssh_key` and `ssh_port`.

```bash
uv run opensandbox init --nodes nodes.yaml
```

Setup installs the cluster, joins workers, verifies gVisor on every sandbox node,
creates the private registry, builds the companion for both CPU architectures,
registers the default `base` template, installs the persistent API service, and
runs a live sandbox smoke test. It prints the API URL, E2B settings, generated
`e2b_...` administrator key, CA certificate path and cluster capacity.

The API runs as the `opensandbox` system user under systemd. It does not depend
on a terminal staying open. Setup saves its configuration in `/etc/opensandbox`
and metadata in `/var/lib/opensandbox`. Rerunning setup preserves credentials;
it updates the installation and renews the API certificate. Use `uv run
opensandbox ...` from the checkout for the commands below, or activate `.venv`:

```bash
source .venv/bin/activate
opensandbox status
opensandbox nodes
opensandbox doctor
```

## 3. HTTPS and client configuration

Setup serves HTTPS directly on port 7070 using a generated cluster CA. Copy
**only** `/etc/opensandbox/api-ca.crt` to client machines and trust it there.
Do not copy the private keys or disable certificate verification.

For Linux clients, including the head:

```bash
export SSL_CERT_FILE=/path/to/api-ca.crt
export NODE_EXTRA_CA_CERTS=/path/to/api-ca.crt
export E2B_API_KEY=e2b_your_generated_key
export E2B_API_URL=https://10.0.0.1:7070
export E2B_SANDBOX_URL=https://10.0.0.1:7070
```

The pinned Python SDK uses the operating system's certificate verifier. On
macOS, import the CA into Keychain and explicitly trust it for SSL; setting
`SSL_CERT_FILE` alone does not configure that SDK's native macOS transport.
On Windows, use the trusted root certificate store. Node clients can use
`NODE_EXTRA_CA_CERTS`. With an existing publicly trusted certificate, these CA
steps are unnecessary. Configure `endpoint`, `tls_cert`, `tls_key`, and `api_ca`
in the head's `/etc/opensandbox/config.yaml`, then restart the service. The
certificate must cover the API and wildcard sandbox hostnames. Set `api_ca` to
the matching root CA bundle: diagnostics and image builders use it to verify
the head API. Its endpoint hostname must resolve to the head from workers too.
Keep `registry_ca` unchanged; the registry has separate TLS.

`sandbox.getHost(port)` generates a hostname. Setup defaults to
`<port>-<sandbox-id>.<head-ip>.sslip.io:7070`, which resolves to the head using
sslip.io. For private DNS or your own domain, set this before initialization:

```yaml
sandbox_domain: sandbox.example.com:7070
```

Point `*.sandbox.example.com` to the head. Setup's certificate includes that
wildcard. The API and runtime URLs can share the head endpoint; SDK routing
headers identify the sandbox. An HTTPS reverse proxy must preserve Host,
streamed bodies and WebSocket upgrades.

## 4. Use the official E2B SDKs

Compatibility is tested against **Python `e2b==2.51.0` and npm `e2b@2.51.0`**.
The SDKs are unmodified. See the exact [compatibility matrix](docs/e2b-compatibility.md).

```bash
uv pip install 'e2b==2.51.0'
```

```python
from e2b import Sandbox

sandbox = Sandbox.create()
try:
    sandbox.files.write('/workspace/main.py', 'print("hello from OpenSandbox")')
    result = sandbox.commands.run('python3 /workspace/main.py')
    print(result.stdout)
finally:
    sandbox.kill()
```

In your TypeScript application:

```bash
npm install e2b@2.51.0
```

```typescript
import { Sandbox } from 'e2b'

const sandbox = await Sandbox.create()
try {
  await sandbox.files.write('/workspace/main.py', 'print("hello from OpenSandbox")')
  console.log((await sandbox.commands.run('python3 /workspace/main.py')).stdout)
} finally {
  await sandbox.kill()
}
```

Commands support stdout/stderr, exit codes, callbacks/streaming, timeouts,
environment variables, working directories, background execution, process
listing, stdin, stdin closure and termination. PTYs support input, output,
resize, reconnect and close. Files support text and binary reads/writes,
listing, stat, exists, mkdir, rename and removal, including SDK multipart uploads.

Commands run as the sandbox's root user with Linux capabilities dropped.
Switching to a different user is explicitly rejected; it is not silently ignored.
Images used with E2B commands/PTYs need `/bin/bash`. The default `base` image is
`python:3.13-slim`, with `/workspace` as its working directory.

```typescript
await sandbox.commands.run('python3 -m http.server 3000 --bind 0.0.0.0', {
  background: true,
})
console.log(`https://${sandbox.getHost(3000)}`)
```

E2B application ports are public to holders of the random sandbox URL by default.
Create with `network: { allowPublicTraffic: false }` to require the SDK's
`trafficAccessToken` in the `e2b-traffic-access-token` header. Runtime operations
always require a separate sandbox token. All traffic goes through the head;
workers have no public customer API.

## 5. Templates and images

`Sandbox.create()` uses the `base` template. A template maps a name to an OCI
image and resource defaults. Register an existing image or build a Dockerfile:

```bash
opensandbox image register python-agent python:3.13-slim --cpu 2 --memory 2Gi
opensandbox image build ./examples/python-agent --name python-agent
opensandbox image build ./my-project/Dockerfile --name my-agent
opensandbox images
```

Then use `Sandbox.create("python-agent")` in either E2B SDK.

Builds run inside gVisor using Kaniko; no Docker daemon is needed. Project
contexts honor `.dockerignore` or `Dockerfile.dockerignore`. `.git`, `.venv`,
`.cache` and `node_modules` are always excluded. Transfers reject symlinks and
special files. Contexts are limited to 512 MiB and built image archives to 8 GiB.
Build output is published into the head's TLS/authenticated OCI registry.
Workers pull and cache layers through containerd. Kubernetes selects placement,
with a preference for nodes that already cache the requested image. Locally built
images are pinned to the architecture on which they were built.

For private upstream images, add registry credentials through environment
variables on the setup machine, then rerun initialization:

```yaml
registries:
  ghcr.io:
    username: your-user
    password_env: REGISTRY_PASSWORD
    # token_realm: https://your-registry.example.com/token
```

Long-lived credentials stay on the head and in containerd's root-owned registry
configuration. Build sandboxes receive temporary, read-only pull capabilities.
Custom registry token services need an explicit `token_realm` if they use a
different host from the registry.

```bash
opensandbox image delete python-agent          # Remove an alias
opensandbox image delete 'host:30500/images/build@sha256:...'  # Remove an unused built image
```

The default `base` alias cannot be removed. Built images referenced by a template
or active sandbox cannot be deleted. Registry blobs remain until an operator
runs registry garbage collection; deleting an image does not claim instant disk
reclamation. Interrupted builds are stopped when the API restarts and can be retried.

## 6. Administration

```bash
opensandbox start
opensandbox stop
opensandbox restart
opensandbox status
opensandbox nodes
opensandbox sandboxes
opensandbox node add ubuntu@10.0.0.5
opensandbox node remove worker-1
opensandbox doctor
opensandbox logs
opensandbox logs --node worker-2
opensandbox logs <sandbox-id>
opensandbox metrics
```

`stop` and `restart` manage the head API service. Kubernetes and running sandbox
processes remain active; deadlines still apply. Removing a worker cordons it and
waits for sandboxes to finish. Use `--force` to terminate its sandbox workloads.
OpenSandbox never terminates the underlying machine. If a forced removal cannot
reach the worker, the output tells you to stop its old K3s agent before reconnecting it.

On the head, `doctor` also checks each machine's Linux/kernel, CPU, RAM, free
disk, SSH/root access, K3s/containerd, runsc and required TCP connectivity. It
uses the administrator's SSH credentials. Remote API diagnostics run the live
sandbox, networking, registry, E2B and port-routing checks without SSH access.

Use keys with separate ownership for applications:

```bash
opensandbox key create agent-app
opensandbox key list
opensandbox key revoke <key-id>
opensandbox key create another-admin --admin
```

New secrets are shown once; SQLite stores their hashes. Application keys can
access their own sandboxes. Administrator keys manage templates, images, keys
and cluster diagnostics. Revoking a key also disables runtime access to the
sandboxes it owns. The last active administrator cannot be revoked. The initial
administrator credential lives in a protected head configuration for local
administration; use separate keys for clients.

The optional native SDK and CLI support explicit CPU/RAM/disk allocations:

```bash
opensandbox create --image python:3.13-slim --cpu 2 --memory 4Gi --disk 10Gi --timeout 600
opensandbox exec <id> 'python3 --version'
opensandbox cp ./project <id>:/workspace/project
opensandbox cp <id>:/workspace/project ./downloads
opensandbox kill <id>
```

Native directory downloads extract the named remote directory beneath the local
destination. Resource requests and limits are equal. CPU is capped, memory is
cgroup-limited, and ephemeral disk is enforced through Kubernetes accounting and
eviction rather than an instantaneous filesystem quota. The kubelet caps a pod
at 1,024 processes. Retained command output and replay buffers are bounded.

`/metrics` provides authenticated Prometheus metrics for lifecycle counts and
latency, API/command latency, build/pull time, cache observations, capacity,
worker health and resource use. `opensandbox metrics` returns JSON. Missing
Kubernetes utilization data is reported as unavailable instead of zero.
Logs are structured and omit request URLs containing access capabilities.

## 7. Runtime guarantees and limits

Every sandbox and image builder uses RuntimeClass **`gvisor`**, with handler
**`runsc`**. Setup verifies each eligible node. Creation checks the RuntimeClass,
verified node label, pod readiness and a direct kernel-log syscall from the
trusted companion. The companion refuses production startup outside gVisor.
There is no alternative runtime fallback.

Sandbox pods receive no host mounts, host namespaces, container sockets or
Kubernetes service-account token. Namespaces start with default-deny networking;
Cilium additionally blocks traffic to node, cluster and API endpoints. Internet
access is optional; private/metadata networks and other sandboxes stay blocked.

Sandboxes are ephemeral. The Job controller and companion both enforce expiry.
Extending a timeout updates the Job deadline. API restarts recover metadata and
existing workloads. Worker failure marks affected sandboxes **lost**; they are
not presented as migrated or transparently restarted.

Pause/resume, snapshots, fork, persistent volumes, filesystem watchers, file
metadata, managed MCP, workload identity, GPU resources and advanced network
allow/deny rules are not implemented. Requests for unsupported behavior fail
explicitly. The head is a single point of failure; this release does not promise
high availability. Back up head metadata, configuration/keys, registry storage
and K3s state using a consistent maintenance backup.

The [architecture](docs/architecture.md), [security notes](docs/security.md) and
[troubleshooting guide](docs/troubleshooting.md) explain these boundaries.

## 8. Validation

Local checks exercise the real companion and head API with simulated Kubernetes
placement. They do not establish kernel isolation or successful VM deployment.

```bash
uv sync --extra dev
npm --prefix sdk/typescript ci
# Install Go 1.23+ for the companion tests.
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run mypy src
uv run pytest -q
go -C agent test -race ./...
npm --prefix sdk/typescript run lint
npm --prefix sdk/typescript test
npm --prefix sdk/typescript run build
uv build
```

After setup on a **disposable Linux acceptance cluster**, configure the E2B
variables and trusted CA above, then run from the head checkout:

```bash
# One head with sandbox workloads:
OPENSANDBOX_ACCEPT_NODES=1 uv run pytest -m cluster tests/cluster -q

# A head and at least two sandbox-capable workers:
OPENSANDBOX_ACCEPT_NODES=2 uv run pytest -m cluster tests/cluster -q
```

These tests use actual gVisor workloads and unmodified SDKs, including a
Dockerfile build, registry publication, a named template, and real
wildcard DNS/HTTPS port URLs. Placement tests temporarily cordon nodes and
restore their original state. Failure and membership tests require explicit
additional inputs, since they stop a worker agent or install a spare machine:

```bash
export OPENSANDBOX_ACCEPT_FAILURE_NODE=worker-1
export OPENSANDBOX_ACCEPT_ADD_NODE=ubuntu@10.0.0.4
OPENSANDBOX_ACCEPT_NODES=2 uv run pytest -m cluster tests/cluster -q
```

Without those two variables, only those disruptive cases are skipped. Normal
local pytest runs exclude the entire `cluster` marker. On macOS the Python SDK's
self-signed HTTPS case is also skipped because tests do not change Keychain
trust; the TypeScript HTTPS case runs locally, and Linux CI runs both.
