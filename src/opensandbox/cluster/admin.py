"""Head-only node administration. Machines are joined/removed, never provisioned."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from opensandbox.cluster.bootstrap import Bootstrap, Runner
from opensandbox.cluster.scripts import PREFLIGHT, install_node, write_file
from opensandbox.config import Inventory, NodeSpec, Settings
from opensandbox.errors import ConflictError, NotFoundError, RuntimeError


class Admin:
    def __init__(self):
        # The saved inventory may contain SSH key paths; root owns this file.
        reader = Runner(Inventory(head={"host": "10.0.0.1"}))
        self.inventory = Inventory.model_validate(
            yaml.safe_load(reader.run(["cat", "/etc/opensandbox/nodes.yaml"], root=True))
        )
        self.runner = Runner(self.inventory)
        self.settings = Settings(
            **yaml.safe_load(self.runner.run(["cat", "/etc/opensandbox/config.yaml"], root=True))
        )

    def save(self):
        self.runner.script(
            write_file(
                "/etc/opensandbox/nodes.yaml",
                yaml.safe_dump(self.inventory.model_dump(mode="json")),
            )
        )

    def diagnose_machines(self):
        """Run read-only host checks using the administrator's SSH credentials."""
        checks, details, resources = {}, {}, {}
        for node in [self.inventory.head, *self.inventory.workers]:
            is_head = node == self.inventory.head
            name = node.name or (
                "opensandbox-head" if is_head else "worker-" + node.host.replace(".", "-")
            )
            script = PREFLIGHT + "\n/usr/local/bin/runsc --version\n"
            script += f"systemctl is-active {'k3s' if is_head else 'k3s-agent'}\n"
            script += "/usr/local/bin/k3s crictl info >/dev/null\n"
            if not is_head:
                for port in (6443, 30500):
                    script += (
                        f"timeout 10 bash -c 'echo > /dev/tcp/{self.inventory.head.host}/{port}'\n"
                    )
            try:
                resources[name] = self.runner.script(script, None if is_head else node).strip()
                if not is_head:
                    self.runner.script(f"timeout 10 bash -c 'echo > /dev/tcp/{node.host}/10250'\n")
                checks["machine:" + name] = True
            except (RuntimeError, OSError) as exc:
                checks["machine:" + name] = False
                details["machine:" + name] = str(exc)
        return {"checks": checks, "details": details, "machines": resources}

    def add(self, target: str, *, ssh_key: Path | None = None, ssh_port=22):
        if "@" in target:
            user, host = target.split("@", 1)
        else:
            user, host = self.inventory.ssh_user, target
        node = NodeSpec(host=host, ssh_user=user, ssh_key=ssh_key, ssh_port=ssh_port)
        if any(n.host == node.host for n in [self.inventory.head, *self.inventory.workers]):
            raise ConflictError("Machine is already in the saved inventory")
        token = self.runner.run(
            ["cat", "/var/lib/rancher/k3s/server/node-token"], root=True
        ).strip()
        registry_config = yaml.safe_load(
            self.runner.run(["cat", "/etc/rancher/k3s/registries.yaml"], root=True)
        )
        cert = self.runner.run(["cat", "/etc/opensandbox/registry-ca.crt"], root=True)
        self.runner.script(
            install_node(
                self.inventory, node, token=token, registry_config=registry_config, ca=cert
            ),
            node,
        )
        name = node.name or "worker-" + host.replace(".", "-")
        for _ in range(60):
            nodes = json.loads(self.runner.kubectl("get", "nodes", "-o", "json"))["items"]
            if any(n["metadata"]["name"] == name for n in nodes):
                break
            time.sleep(1)
        self.runner.kubectl(
            "wait", "--for=condition=Ready", "node/" + name, "--timeout=180s", timeout=200
        )
        # Verify only the new worker; do not cordon or relabel existing capacity.
        verifier = Bootstrap(
            self.inventory.model_copy(update={"workers": [node]}), Path.cwd(), self.runner
        )
        verifier._verify_nodes(self.settings, only_workers=True)
        self.inventory.workers.append(node)
        self.save()
        return {"name": name, "host": host, "ready": True}

    def remove(self, name: str, *, force=False, timeout=300):
        head_name = self.inventory.head.name or "opensandbox-head"
        if name == head_name:
            raise ConflictError("The head cannot be removed with node remove")
        node = next(
            (
                n
                for n in self.inventory.workers
                if (n.name or "worker-" + n.host.replace(".", "-")) == name
            ),
            None,
        )
        if node is None:
            raise NotFoundError("Node is not in the saved worker inventory")
        self.runner.kubectl("cordon", name)
        deadline = time.monotonic() + timeout
        while True:
            pods = json.loads(
                self.runner.kubectl(
                    "get",
                    "pods",
                    "-A",
                    "--field-selector",
                    "spec.nodeName=" + name,
                    "-l",
                    "app.kubernetes.io/managed-by=opensandbox",
                    "-o",
                    "json",
                )
            )["items"]
            active = [
                p for p in pods if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            ]
            if not active:
                break
            if force:
                for pod in active:
                    ns = pod["metadata"]["namespace"]
                    for owner in pod["metadata"].get("ownerReferences", []):
                        if owner["kind"] == "Job":
                            self.runner.kubectl(
                                "delete",
                                "job",
                                owner["name"],
                                "-n",
                                ns,
                                "--ignore-not-found",
                                "--wait=false",
                            )
                    self.runner.kubectl(
                        "delete",
                        "pod",
                        pod["metadata"]["name"],
                        "-n",
                        ns,
                        "--grace-period=0",
                        "--wait=false",
                    )
                break
            if time.monotonic() >= deadline:
                raise ConflictError(
                    "Node is cordoned but still has active sandboxes. Wait for them to finish or retry with --force"
                )
            time.sleep(2)
        uninstalled = True
        try:
            self.runner.script(
                "set -eu\nif [ -x /usr/local/bin/k3s-agent-uninstall.sh ]; then /usr/local/bin/k3s-agent-uninstall.sh; fi\n",
                node,
            )
        except RuntimeError:
            if not force:
                raise
            uninstalled = False
        self.runner.kubectl("delete", "node", name, "--ignore-not-found")
        self.inventory.workers.remove(node)
        self.save()
        return {
            "removed": name,
            "machine_terminated": False,
            "agent_uninstalled": uninstalled,
            "note": None
            if uninstalled
            else "Machine was unreachable. Stop/uninstall its old K3s agent before reconnecting it.",
        }

    def logs(self, node_name: str | None = None, lines=200):
        if node_name is None:
            return self.runner.run(
                ["journalctl", "-u", "opensandbox", "-n", str(lines), "--no-pager"], root=True
            )
        node = next(
            (
                n
                for n in self.inventory.workers
                if (n.name or "worker-" + n.host.replace(".", "-")) == node_name
            ),
            None,
        )
        if node is None:
            raise NotFoundError("Worker not found in inventory")
        return self.runner.script(f"journalctl -u k3s-agent -n {int(lines)} --no-pager\n", node)
