"""Clone-on-head installer. All machine changes are explicit administrative actions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import shlex
import shutil
import ssl
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import yaml

from opensandbox.cluster.manifests import VERIFIED, runtime_class
from opensandbox.cluster.platform import api_rbac, infrastructure, network_chart
from opensandbox.cluster.registry import Registry
from opensandbox.cluster.scripts import PREFLIGHT, install_node, write_file
from opensandbox.config import Inventory, NodeSpec, RegistryAuth, Settings
from opensandbox.errors import ConfigurationError, RuntimeError


class Runner:
    def __init__(self, inventory: Inventory):
        self.inventory = inventory

    def run(self, args: list[str], *, data: str | bytes | None = None, timeout=900, root=False):
        if root and os.geteuid() != 0:
            args = ["sudo", "-n", *args]
        result = subprocess.run(
            args,
            input=data.encode() if isinstance(data, str) else data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            # Never include stdin: it can contain join tokens or registry credentials.
            detail = result.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(
                f"{Path(args[0]).name} failed with exit {result.returncode}: {detail}"
            )
        return result.stdout.decode(errors="replace")

    def script(self, script: str, node: NodeSpec | None = None):
        if node is None:
            return self.run(["bash", "-s"], data=script, root=True)
        user = node.ssh_user or self.inventory.ssh_user
        args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-p",
            str(node.ssh_port),
        ]
        key = node.ssh_key or self.inventory.ssh_key
        if key:
            args += ["-i", str(key.expanduser())]
        args += [f"{user}@{node.host}", "bash -s" if user == "root" else "sudo -n bash -s"]
        return self.run(args, data=script)

    def kubectl(self, *args: str, data=None, timeout=180):
        return self.run(
            ["/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml", *args],
            data=data,
            timeout=timeout,
            root=True,
        )

    def apply(self, objects):
        self.kubectl("apply", "-f", "-", data=yaml.safe_dump_all(objects))


class Bootstrap:
    def __init__(
        self, inventory: Inventory, checkout: Path, runner: Runner | None = None, progress=print
    ):
        self.inventory = inventory
        self.checkout = checkout.resolve()
        self.runner = runner or Runner(inventory)
        self.progress = progress
        self.config_dir = Path("/etc/opensandbox")

    def initialize(self):
        if platform.system() != "Linux":
            raise ConfigurationError(
                "Clone this repository onto your Linux cluster head and run init there"
            )
        if (
            not (self.checkout / "agent/go.mod").is_file()
            or not (self.checkout / "pyproject.toml").is_file()
        ):
            raise ConfigurationError("Run init from the cloned OpenSandbox repository")
        if os.geteuid() != 0:
            # Acquire sudo once in the foreground; unattended runs need passwordless sudo.
            subprocess.run(["sudo", "-v"], check=True)
        addresses = json.loads(self.runner.run(["ip", "-j", "address"]))
        local_ips = {
            a["local"]
            for entry in addresses
            for a in entry.get("addr_info", [])
            if a.get("family") == "inet"
        }
        if self.inventory.head.host not in local_ips:
            raise ConfigurationError(
                "nodes.yaml head.host must be an address on this machine; run init on the head"
            )
        self.progress(
            "Checking resources, Linux support and administrative access on every machine"
        )
        self.runner.script(PREFLIGHT)
        for worker in self.inventory.workers:
            self.progress(f"Checking SSH and machine requirements: {worker.host}")
            self.runner.script(PREFLIGHT, worker)
        self.progress("Checking head and installing K3s + gVisor")
        self.runner.script(install_node(self.inventory, self.inventory.head, head=True))
        self._wait_api()
        self.progress("Installing cluster networking and host-access policy enforcement")
        # The Helm bootstrap job uses host networking, so it can install the CNI
        # before normal pods or nodes become Ready.
        self.runner.apply([network_chart(self.inventory)])
        for _ in range(180):
            chart = json.loads(
                self.runner.kubectl("get", "daemonsets", "-n", "kube-system", "-o", "json")
            )
            if any(item["metadata"]["name"] == "cilium" for item in chart["items"]):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Cilium installation did not create its node agent")
        self.runner.kubectl(
            "rollout",
            "status",
            "daemonset/cilium",
            "-n",
            "kube-system",
            "--timeout=180s",
            timeout=200,
        )
        self.runner.kubectl(
            "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=180s", timeout=200
        )
        self.runner.script(
            "set -eu\ngetent passwd opensandbox >/dev/null || useradd --system --home /var/lib/opensandbox --shell /usr/sbin/nologin opensandbox\ninstall -d -m 0700 -o opensandbox -g opensandbox /var/lib/opensandbox\n"
        )
        saved_text = self.runner.script(
            "if [ -s /etc/opensandbox/config.yaml ]; then cat /etc/opensandbox/config.yaml; elif [ -s /etc/opensandbox/bootstrap-secrets.yaml ]; then cat /etc/opensandbox/bootstrap-secrets.yaml; fi\n"
        )
        if saved_text.strip():
            saved = yaml.safe_load(saved_text)
            api_key, registry_password = saved["api_key"], saved["registry_password"]
        else:
            api_key, registry_password = (
                "e2b_" + secrets.token_hex(32),
                secrets.token_urlsafe(40),
            )
        # Persist before starting the registry, so interrupted setup never rotates
        # credentials behind already-running components.
        self.runner.script(
            write_file(
                "/etc/opensandbox/bootstrap-secrets.yaml",
                yaml.safe_dump({"api_key": api_key, "registry_password": registry_password}),
            )
        )
        upstream = {}
        for host, source in self.inventory.registries.items():
            password = os.environ.get(source.password_env)
            if not password:
                raise ConfigurationError(
                    f"Set {source.password_env} before initializing private registry access"
                )
            upstream[host] = RegistryAuth(
                username=source.username, password=password, token_realm=source.token_realm
            )
        settings = Settings(
            head_ip=self.inventory.head.host,
            upstream_registries=upstream,
            api_key=api_key,
            registry_password=registry_password,
            endpoint=f"https://{self.inventory.head.host}:7070",
            sandbox_domain=self.inventory.sandbox_domain
            or f"{self.inventory.head.host}.sslip.io:7070",
            tls_cert=self.config_dir / "api.crt",
            tls_key=self.config_dir / "api.key",
            api_ca=self.config_dir / "api-ca.crt",
            registry=f"{self.inventory.head.host}:30500",
            registry_ca=self.config_dir / "registry-ca.crt",
            agent_image="pending",
            pod_cidr=self.inventory.pod_cidr,
            service_cidr=self.inventory.service_cidr,
        )
        cert, key = self._certificate()
        api_ca = self._api_certificate(settings)
        import bcrypt

        htpasswd = (
            "opensandbox:" + bcrypt.hashpw(registry_password.encode(), bcrypt.gensalt()).decode()
        )
        head_name = self.inventory.head.name or "opensandbox-head"
        self.progress("Starting authenticated private image registry")
        self.runner.apply(infrastructure(settings, head_name, cert, key, htpasswd))
        self.runner.kubectl(
            "rollout",
            "status",
            "deployment/registry",
            "-n",
            "opensandbox-system",
            "--timeout=180s",
            timeout=200,
        )
        registry_config = {
            "configs": {
                settings.registry: {
                    "auth": {"username": settings.registry_username, "password": registry_password},
                    "tls": {"ca_file": str(settings.registry_ca)},
                }
            }
        }
        for host, credential in upstream.items():
            registry_config["configs"][host] = {
                "auth": {
                    "username": credential.username,
                    "password": credential.password.get_secret_value(),
                }
            }
        self.runner.script(
            write_file("/etc/rancher/k3s/registries.yaml", yaml.safe_dump(registry_config))
            + write_file(str(settings.registry_ca), cert, "0644")
            + "systemctl restart k3s\n"
        )
        self._wait_api()
        self.runner.kubectl(
            "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=180s", timeout=200
        )
        token = self.runner.run(
            ["cat", "/var/lib/rancher/k3s/server/node-token"], root=True
        ).strip()
        for worker in self.inventory.workers:
            self.progress(f"Joining worker {worker.host}")
            self.runner.script(
                f"timeout 10 bash -c 'echo > /dev/tcp/{self.inventory.head.host}/6443'\n", worker
            )
            self.runner.script(
                install_node(
                    self.inventory, worker, token=token, registry_config=registry_config, ca=cert
                ),
                worker,
            )
            self._wait_node(worker)
            self.runner.script(f"timeout 10 bash -c 'echo > /dev/tcp/{worker.host}/10250'\n")
        self._verify_nodes(settings)
        self.progress("Building and publishing sandbox companion for amd64 and arm64")
        settings.agent_image = self._publish_agent(settings, cert)
        self.runner.apply(api_rbac(settings))
        kubeconfig = self._service_kubeconfig()
        self._save_settings(settings, kubeconfig)
        self._install_service()
        self.progress("Running end-to-end cluster smoke test")
        headers = {"Authorization": "Bearer " + api_key}
        with httpx.Client(
            base_url="https://127.0.0.1:7070",
            headers=headers,
            timeout=180,
            trust_env=False,
            verify=ssl.create_default_context(cadata=api_ca),
        ) as client:
            for _attempt in range(60):
                try:
                    if client.get("/v1/health").is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Head API did not start; inspect journalctl -u opensandbox")
            result = client.post("/v1/doctor")
            result.raise_for_status()
            if not result.json().get("ok"):
                raise RuntimeError("Cluster smoke test failed: " + json.dumps(result.json()))
            status = client.get("/v1/status")
            status.raise_for_status()
        return {
            "endpoint": settings.endpoint,
            "api_key": api_key,
            "e2b_api_url": settings.endpoint,
            "e2b_sandbox_url": settings.endpoint,
            "sandbox_domain": settings.sandbox_domain,
            "ca_certificate": str(settings.api_ca),
            "runtime": "gVisor",
            "cluster": status.json(),
        }

    def _wait_api(self):
        for _ in range(90):
            try:
                self.runner.kubectl("get", "--raw=/readyz", timeout=5)
                return
            except RuntimeError:
                time.sleep(1)
        raise RuntimeError("K3s API did not become ready on port 6443")

    def _wait_node(self, node):
        name = node.name or "worker-" + node.host.replace(".", "-")
        for _ in range(90):
            nodes = json.loads(self.runner.kubectl("get", "nodes", "-o", "json"))["items"]
            if any(n["metadata"]["name"] == name for n in nodes):
                self.runner.kubectl(
                    "wait", "--for=condition=Ready", "node/" + name, "--timeout=180s", timeout=200
                )
                return
            time.sleep(1)
        raise RuntimeError(
            f"{name} did not join; check connectivity to head port 6443 and journalctl -u k3s-agent"
        )

    def _api_certificate(self, settings):
        domain = settings.sandbox_domain.split(":", 1)[0]
        extensions = "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        extensions += f"subjectAltName=IP:{settings.head_ip},IP:127.0.0.1,DNS:localhost,DNS:{domain},DNS:*.{domain}\n"
        script = "set -eu\numask 077\ncd /etc/opensandbox\n"
        script += "if [ ! -s api-ca.crt ]; then openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 -keyout api-ca.key -out api-ca.crt -subj /CN=OpenSandbox-CA -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign; fi\n"
        # Regenerating the leaf permits inventory domain changes while keeping CA trust.
        script += write_file("/etc/opensandbox/api.ext", extensions)
        script += "openssl req -new -newkey rsa:3072 -sha256 -nodes -keyout api.key -out api.csr -subj /CN=OpenSandbox\n"
        script += "openssl x509 -req -in api.csr -CA api-ca.crt -CAkey api-ca.key -CAcreateserial -out api.crt -days 365 -sha256 -extfile api.ext\n"
        script += "chown opensandbox:opensandbox api.key api.crt\nchmod 0600 api.key\nchmod 0644 api.crt api-ca.crt\n"
        self.runner.script(script)
        return self.runner.run(["cat", str(settings.api_ca)], root=True)

    def _certificate(self):
        cert_path = self.config_dir / "registry-ca.crt"
        key_path = self.config_dir / "registry.key"
        script = "set -eu\n"
        script += f"if [ ! -s {shlex.quote(str(cert_path))} ]; then\n"
        script += "umask 077\nopenssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 "
        script += f"-keyout {shlex.quote(str(key_path))} -out {shlex.quote(str(cert_path))} "
        script += f"-subj /CN=opensandbox-registry -addext {shlex.quote('subjectAltName=IP:' + self.inventory.head.host)}\nfi\n"
        self.runner.script(script)
        return (
            self.runner.run(["cat", str(cert_path)], root=True),
            self.runner.run(["cat", str(key_path)], root=True),
        )

    def _verify_nodes(self, settings, *, only_workers=False):
        # Bootstrap verifier deliberately has no verified-node selector.
        self.runner.apply(
            [
                {
                    "apiVersion": "node.k8s.io/v1",
                    "kind": "RuntimeClass",
                    "metadata": {"name": "opensandbox-verify"},
                    "handler": "runsc",
                }
            ]
        )
        for node in (
            self.inventory.workers
            if only_workers
            else [self.inventory.head, *self.inventory.workers]
        ):
            name = node.name or (
                "opensandbox-head"
                if node == self.inventory.head
                else "worker-" + node.host.replace(".", "-")
            )
            self.runner.kubectl("label", "node", name, VERIFIED + "-", "--overwrite")
            if not node.sandboxes:
                continue
            self.progress(f"Verifying gVisor on {name}")
            pod_name = "verify-" + secrets.token_hex(5)
            self.runner.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {"name": pod_name, "namespace": "opensandbox-system"},
                        "spec": {
                            "nodeName": name,
                            "runtimeClassName": "opensandbox-verify",
                            "restartPolicy": "Never",
                            "activeDeadlineSeconds": 90,
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": "verify",
                                    "image": "busybox:1.37",
                                    "command": [
                                        "/bin/sh",
                                        "-ec",
                                        "dmesg | grep -i gvisor; test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token",
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "32Mi"},
                                        "limits": {"cpu": "100m", "memory": "64Mi"},
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                }
                            ],
                        },
                    }
                ]
            )
            try:
                self.runner.kubectl(
                    "wait",
                    "--for=jsonpath={.status.phase}=Succeeded",
                    "pod/" + pod_name,
                    "-n",
                    "opensandbox-system",
                    "--timeout=100s",
                    timeout=120,
                )
            finally:
                self.runner.kubectl(
                    "delete",
                    "pod",
                    pod_name,
                    "-n",
                    "opensandbox-system",
                    "--ignore-not-found",
                    "--wait=false",
                )
            self.runner.kubectl("label", "node", name, VERIFIED + "=verified", "--overwrite")
        self.runner.apply([runtime_class(settings)])

    def _publish_agent(self, settings, cert):
        cache = self.checkout / ".cache" / "bootstrap"
        cache.mkdir(parents=True, exist_ok=True)
        go = shutil.which("go")
        if not go:
            self.progress("Downloading Go toolchain and verifying its checksum")
            arch = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
            with httpx.Client(follow_redirects=True, timeout=180) as client:
                releases = client.get("https://go.dev/dl/?mode=json")
                releases.raise_for_status()
                artifact = next(
                    f
                    for f in releases.json()[0]["files"]
                    if f["os"] == "linux" and f["arch"] == arch and f["kind"] == "archive"
                )
                payload = client.get("https://go.dev/dl/" + artifact["filename"])
                payload.raise_for_status()
                if hashlib.sha256(payload.content).hexdigest() != artifact["sha256"]:
                    raise RuntimeError("Go toolchain checksum mismatch")
                archive = cache / artifact["filename"]
                archive.write_bytes(payload.content)
            with tarfile.open(archive) as tar:
                tar.extractall(cache, filter="data")
            go = str(cache / "go/bin/go")
        registry = Registry(
            "https://" + settings.registry,
            settings.registry_username,
            settings.registry_password.get_secret_value(),
            verify=ssl.create_default_context(cadata=cert),
        )
        tag = "v0.2.0-" + secrets.token_hex(4)
        manifests = []
        try:
            for arch in ["amd64", "arm64"]:
                binary = cache / ("agent-" + arch)
                self.runner.run(
                    [
                        "env",
                        "CGO_ENABLED=0",
                        "GOOS=linux",
                        "GOARCH=" + arch,
                        go,
                        "-C",
                        str(self.checkout / "agent"),
                        "build",
                        "-trimpath",
                        "-ldflags=-s -w",
                        "-o",
                        str(binary),
                        ".",
                    ]
                )
                manifests.append(registry.push_agent(binary.read_bytes(), arch, tag))
            return registry.push_agent_index(manifests, tag)
        finally:
            registry.close()

    def _service_kubeconfig(self):
        for _attempt in range(30):
            value = json.loads(
                self.runner.kubectl(
                    "get", "secret", "api-token", "-n", "opensandbox-system", "-o", "json"
                )
            )
            if value.get("data", {}).get("token"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Kubernetes did not issue the API service account token")
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "opensandbox",
            "clusters": [
                {
                    "name": "opensandbox",
                    "cluster": {
                        "server": "https://127.0.0.1:6443",
                        "certificate-authority-data": value["data"]["ca.crt"],
                    },
                }
            ],
            "users": [
                {
                    "name": "api",
                    "user": {"token": base64.b64decode(value["data"]["token"]).decode()},
                }
            ],
            "contexts": [
                {"name": "opensandbox", "context": {"cluster": "opensandbox", "user": "api"}}
            ],
        }

    def _save_settings(self, settings, kubeconfig):
        data = settings.model_dump(mode="json")
        data["api_key"] = settings.api_key.get_secret_value()
        data["registry_password"] = settings.registry_password.get_secret_value()
        for host, credential in settings.upstream_registries.items():
            data["upstream_registries"][host]["password"] = credential.password.get_secret_value()
        script = write_file(str(self.config_dir / "config.yaml"), yaml.safe_dump(data))
        script += write_file(str(settings.kubeconfig), yaml.safe_dump(kubeconfig))
        script += write_file(
            str(self.config_dir / "nodes.yaml"),
            yaml.safe_dump(self.inventory.model_dump(mode="json")),
        )
        script += "chown opensandbox:opensandbox /etc/opensandbox/config.yaml /etc/opensandbox/kubeconfig\n"
        script += "chmod 0755 /etc/opensandbox\n"
        self.runner.script(script)

    def _install_service(self):
        uv = shutil.which("uv")
        if not uv:
            raise ConfigurationError("uv is required to install the head service")
        self.progress("Installing the persistent head API service")
        # Install a self-contained Python environment; the service doesn't depend on
        # the bootstrap user's home directory permissions or shell environment.
        self.runner.run(
            [uv, "python", "install", "--install-dir", "/opt/opensandbox/python", "3.13"], root=True
        )
        self.runner.run(
            [
                "env",
                "UV_PYTHON_INSTALL_DIR=/opt/opensandbox/python",
                uv,
                "venv",
                "--allow-existing",
                "--python",
                "3.13",
                "/opt/opensandbox/venv",
            ],
            root=True,
        )
        self.runner.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                "/opt/opensandbox/venv/bin/python",
                str(self.checkout),
            ],
            root=True,
        )
        unit = """[Unit]
Description=OpenSandbox sandbox API
After=network-online.target k3s.service
Wants=network-online.target
Requires=k3s.service

[Service]
User=opensandbox
Group=opensandbox
ExecStart=/opt/opensandbox/venv/bin/python -m opensandbox.api
Environment=OPENSANDBOX_CONFIG=/etc/opensandbox/config.yaml
WorkingDirectory=/var/lib/opensandbox
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/opensandbox
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
        self.runner.script(
            write_file("/etc/systemd/system/opensandbox.service", unit, "0644")
            + "systemctl daemon-reload\nsystemctl enable opensandbox\nsystemctl restart opensandbox\n"
        )
