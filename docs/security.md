# Security boundaries

All user sandboxes and image builders require RuntimeClass `gvisor` with the
`runsc` handler. Nodes receive the scheduling label only after a successful
runtime smoke test. Creation checks the configured handler, node label, running
pod and companion kernel attestation. The production companion refuses to start
unless its direct syslog syscall identifies the gVisor kernel.

User pods have no host mounts, container socket, host namespaces or Kubernetes
service-account token. They drop Linux capabilities and prohibit privilege
escalation. The sandbox user is root **inside gVisor**; changing to another user
is currently rejected. Image builders receive the additional capabilities needed
to unpack image layers, still inside gVisor. Head services and the registry are
trusted infrastructure, not user sandboxes.

CPU and memory use Kubernetes limits. Ephemeral disk uses kubelet accounting and
eviction, so a burst may exceed the requested size before eviction. Pod process
host PID count is limited to 1,024, and the companion accepts up to 128 active
command sessions. gVisor tasks do not map one-to-one to host PIDs; this is not
a separate 1,024-task guest quota ([gVisor resource model](https://gvisor.dev/docs/architecture_guide/resources/)).
API bodies, archive expansion and retained process
output are bounded. File transfers reject tar traversal, symlinks and special
files; E2B filesystem operations deliberately address the sandbox's own filesystem.

Namespaces have default-deny ingress/egress. Public internet egress explicitly
excludes private, metadata, reserved, pod, service and node addresses. Cilium
also denies host, remote-node, API and cluster entities. The default DNS servers
are public. Image builders have a narrow additional exception to the head's
pull gateway; production user sandboxes do not receive it.

The API requires bearer or E2B API-key authentication. Key secrets are random;
SQLite stores SHA-256 hashes. Application keys own separate sandbox records.
Administrator keys can inspect and destroy all sandboxes and manage templates
and keys. Runtime tokens are scoped to one sandbox and are rejected after its
owner key is revoked or its lifetime ends. Template/image catalogs are shared
within this administrator-managed cluster; they are not separate tenant registries.

E2B application URLs allow public traffic by default, matching the SDK contract.
Private application URLs require a separate traffic token. Native signed port
URLs expire and never permit access to the internal companion port. Proxy code
removes control-plane credentials before forwarding application requests.

Head credentials, registry authentication and the scoped Kubernetes kubeconfig
are protected files. No control-plane secret is mounted into user containers.
The protected bootstrap administrator key also seeds runtime token signatures;
rotate application keys through the key API. Rotating this master configuration
secret invalidates existing runtime capabilities and needs planned maintenance.

The head and its administrators are trusted. K3s, containerd, gVisor and Cilium
need security updates. A single head is not highly available, and this repository
has not been independently security audited. The live acceptance suite verifies
installed runtime behavior; local simulated-cluster tests do not prove isolation.
