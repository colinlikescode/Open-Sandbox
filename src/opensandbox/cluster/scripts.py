"""Root setup scripts executed locally on the head or over SSH on workers."""

from __future__ import annotations

import base64
import shlex
from typing import Any

import yaml

from opensandbox.config import Inventory, NodeSpec

PREFLIGHT = r"""
set -euo pipefail
[ "$(uname -s)" = Linux ] || { echo 'Linux is required' >&2; exit 1; }
case "$(uname -m)" in x86_64|aarch64) ;; *) echo 'amd64 or arm64 is required' >&2; exit 1;; esac
command -v systemctl >/dev/null || { echo 'systemd is required' >&2; exit 1; }
[ -d /sys/fs/cgroup ] || { echo 'cgroups are required' >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'Root installation privileges are required' >&2; exit 1; }
version=$(uname -r | cut -d- -f1)
[ "$(printf '%s\n' 5.10 "$version" | sort -V | head -n1)" = 5.10 ] || { echo 'Linux 5.10+ is required' >&2; exit 1; }
free_kb=$(df -Pk /var/lib | tail -1 | awk '{print $4}')
[ "$free_kb" -ge 31457280 ] || { echo 'At least 30 GiB free in /var/lib is required' >&2; exit 1; }
[ "$(getconf _NPROCESSORS_ONLN)" -ge 2 ] || { echo 'At least 2 CPU cores are required' >&2; exit 1; }
[ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -ge 2097152 ] || { echo 'At least 2 GiB RAM is required' >&2; exit 1; }
if [ ! -e /etc/opensandbox/managed ]; then
  if [ -e /etc/rancher/k3s/config.yaml ] || [ -e /etc/systemd/system/k3s.service ] || [ -e /etc/systemd/system/k3s-agent.service ]; then
    echo 'An unmanaged K3s installation exists; bootstrap requires clean machines.' >&2; exit 1
  fi
  if command -v ss >/dev/null && ss -H -ltn | awk '{print $4}' | grep -Eq ':(6443|7070|30500|10250)$'; then
    echo 'A required port is already occupied (6443, 7070, 30500 or 10250).' >&2; exit 1
  fi
fi
printf 'CPU=%s MEMORY_KiB=%s FREE_DISK_KiB=%s ARCH=%s KERNEL=%s\n' "$(getconf _NPROCESSORS_ONLN)" "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" "$free_kb" "$(uname -m)" "$(uname -r)"
"""

PRECHECK = (
    PREFLIGHT
    + r"""
if command -v apt-get >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates tar bzip2 iptables conntrack openssl
elif command -v dnf >/dev/null; then
  dnf install -y curl ca-certificates tar bzip2 iptables conntrack-tools openssl
elif command -v zypper >/dev/null; then
  zypper --non-interactive install curl ca-certificates tar bzip2 iptables conntrack-tools openssl
else
  for binary in curl tar bzip2 iptables openssl; do command -v "$binary" >/dev/null; done
fi
sysctl -w net.ipv4.ip_forward=1 >/dev/null
install -d -m 0755 /etc/sysctl.d
printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/90-opensandbox.conf
"""
)

CONTAINERD = """{{ template "base" . }}
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc.options]
  TypeUrl = "io.containerd.runsc.v1.options"
  ConfigPath = "/etc/containerd/runsc.toml"
"""


def write_file(path: str, content: str | bytes, mode="0600") -> str:
    if isinstance(content, str):
        content = content.encode()
    encoded = base64.b64encode(content).decode()
    return f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}\nchmod {mode} {shlex.quote(path)}\n"


def install_node(
    inventory: Inventory, node: NodeSpec, *, head=False, token="", registry_config=None, ca=""
):
    name = node.name or ("opensandbox-head" if head else "worker-" + node.host.replace(".", "-"))
    config: dict[str, Any] = {
        "node-name": name,
        "node-ip": node.host,
        "kubelet-arg": [
            "pod-max-pids=1024",
            "system-reserved=cpu=250m,memory=256Mi",
            "kube-reserved=cpu=250m,memory=256Mi",
        ],
        "node-label": ["opensandbox.dev/managed=true"],
    }
    if head:
        config.update(
            {
                "write-kubeconfig-mode": "0600",
                "tls-san": [node.host],
                "advertise-address": node.host,
                "disable": ["traefik", "servicelb"],
                "cluster-cidr": inventory.pod_cidr,
                "service-cidr": inventory.service_cidr,
                "secrets-encryption": True,
                "flannel-backend": "none",
                "disable-network-policy": True,
            }
        )
    else:
        config.update({"server": f"https://{inventory.head.host}:6443", "token": token})
    script = (
        PRECHECK
        + r"""
if [ -e /etc/rancher/k3s/config.yaml ] && [ ! -e /etc/opensandbox/managed ]; then
  echo 'An unmanaged K3s installation already exists; use clean VMs for bootstrap.' >&2
  exit 1
fi
install -d -m 0755 /etc/opensandbox
install -d -m 0700 /etc/rancher/k3s
install -d -m 0755 /etc/containerd /var/lib/rancher/k3s/agent/etc/containerd
touch /etc/opensandbox/managed
"""
    )
    script += write_file("/etc/rancher/k3s/config.yaml", yaml.safe_dump(config))
    script += write_file(
        "/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl", CONTAINERD, "0644"
    )
    script += write_file(
        "/etc/containerd/runsc.toml",
        '[runsc_config]\n  platform = "systrap"\n  network = "sandbox"\n',
        "0644",
    )
    if registry_config:
        script += write_file("/etc/rancher/k3s/registries.yaml", yaml.safe_dump(registry_config))
        script += write_file("/etc/opensandbox/registry-ca.crt", ca, "0644")
    gvisor = shlex.quote(inventory.gvisor_version)
    script += (
        f"OPENSANDBOX_GVISOR_VERSION={gvisor}\n"
        + r"""
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
arch=$(uname -m)
base="https://storage.googleapis.com/gvisor/releases/release/${OPENSANDBOX_GVISOR_VERSION}/${arch}"
curl --fail --location --retry 3 "$base/gvisor.tar.bz2" -o "$staging/gvisor.tar.bz2"
curl --fail --location --retry 3 "$base/gvisor.tar.bz2.sha512" -o "$staging/gvisor.tar.bz2.sha512"
(cd "$staging" && sha512sum -c gvisor.tar.bz2.sha512)
tar -xjf "$staging/gvisor.tar.bz2" -C /usr/local/bin
/usr/local/bin/runsc --version
/usr/local/bin/containerd-shim-runsc-v1 --version
curl --fail --location --retry 3 https://get.k3s.io -o "$staging/install-k3s.sh"
"""
    )
    script += f'INSTALL_K3S_VERSION={shlex.quote(inventory.k3s_version)} INSTALL_K3S_EXEC={"server" if head else "agent"} sh "$staging/install-k3s.sh"\n'
    script += "touch /etc/opensandbox/managed\n"
    script += f"systemctl restart {'k3s' if head else 'k3s-agent'}\n"
    return script
