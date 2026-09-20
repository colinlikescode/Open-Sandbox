# OpenSandbox

**Self-hosted sandboxes for AI agents on your own CPU machines.**

## What does it do?

1. Clone this repo onto one of your CPU machines, or onto a dedicated Linux machine that will act as the head node, and run setup.

   That machine becomes the **OpenSandbox API and control plane**.

2. OpenSandbox uses **SSH** to bootstrap the worker machines, installs and configures **Kubernetes (K3s)** to connect and schedule across the cluster, and uses **gVisor** to isolate untrusted code inside each sandbox.

   > Unlike E2B's Firecracker-based stack, OpenSandbox can turn ordinary Linux VMs into a sandbox cluster without requiring KVM or specialized virtualization infrastructure. Give it machines and SSH access, and it sets up K3s + gVisor for you.

3. After setup, normal sandbox operations go through the OpenSandbox API and Kubernetes. SSH is primarily used for cluster setup and administration.

---

OpenSandbox provides an **E2B-compatible API**, making it easy to use existing E2B-style integrations and agent workflows against infrastructure you control.

**Use Python, TypeScript, or the CLI to:**

- create sandboxes
- run commands
- read and write files
- start processes
- expose web apps
- destroy sandboxes

The underlying machines can be cloud VMs, bare-metal servers, or other supported Linux CPU machines. You provide the compute; OpenSandbox turns it into a sandbox service.

## Why?

Give AI agents isolated environments for running code on infrastructure you control, without building the scheduling, container, isolation, image, and sandbox API infrastructure yourself or depending on a hosted sandbox provider.

Daytona moved its production codebase to closed source in June 2026, and its existing open-source repository is no longer maintained. OpenSandbox provides a fully open-source, self-hosted alternative where the control plane and sandbox infrastructure run entirely on machines you control.

Its **E2B-compatible API** also makes it easier to move existing agent workloads onto your own infrastructure without redesigning the sandbox interface from scratch.

---

See [instructions.md](https://chatgpt.com/c/instructions.md) for more details.
