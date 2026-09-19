"""Head configuration, separate from remote client environment variables."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opensandbox.models import Model


class RegistryAuth(Model):
    username: str
    password: SecretStr
    token_realm: str | None = None


class RegistrySource(Model):
    username: str
    password_env: str
    token_realm: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSANDBOX_", extra="forbid")
    state_dir: Path = Path("/var/lib/opensandbox")
    kubeconfig: Path = Path("/etc/opensandbox/kubeconfig")
    kubectl: str = "/usr/local/bin/kubectl"
    api_key: SecretStr
    endpoint: str = "http://127.0.0.1:7070"
    sandbox_domain: str = "127.0.0.1.sslip.io:7070"
    tls_cert: Path | None = None
    tls_key: Path | None = None
    api_ca: Path | None = None
    host: str = "0.0.0.0"
    port: int = Field(default=7070, ge=1, le=65535)
    namespace: str = "opensandbox-sandboxes"
    build_namespace: str = "opensandbox-builds"
    runtime_class: Literal["gvisor"] = "gvisor"
    agent_image: str
    registry: str
    registry_ca: Path | None = None
    registry_username: str = "opensandbox"
    registry_password: SecretStr
    image_pull_secret: str = "opensandbox-registry"
    max_upload_bytes: int = 512 * 1024 * 1024
    max_output_bytes: int = 2 * 1024 * 1024
    pod_cidr: str = "10.42.0.0/16"
    service_cidr: str = "10.43.0.0/16"
    reconcile_seconds: float = 5
    head_ip: str = "10.0.0.1"
    upstream_registries: dict[str, RegistryAuth] = Field(default_factory=dict)
    builder_image: str = "ghcr.io/osscontainertools/kaniko:debug"

    @field_validator("api_key", "registry_password")
    @classmethod
    def require_secret(cls, value):
        if len(value.get_secret_value()) < 32:
            raise ValueError("generated credentials must have at least 32 characters")
        return value

    @classmethod
    def load(cls, path: Path | None = None):
        path = path or Path(os.environ.get("OPENSANDBOX_CONFIG", "/etc/opensandbox/config.yaml"))
        return cls(**yaml.safe_load(path.read_text()))


class NodeSpec(Model):
    host: str
    name: str | None = None
    ssh_user: str | None = None
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_key: Path | None = None
    sandboxes: bool = True

    @field_validator("host")
    @classmethod
    def host_address(cls, value):
        # Literal IPs make routing, TLS SANs and firewall rules unambiguous.
        ip = ipaddress.ip_address(value)
        if ip.version != 4 or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            raise ValueError("node host must be a reachable IPv4 address")
        return str(ip)

    @field_validator("name")
    @classmethod
    def node_name(cls, value):
        if value is not None and (
            len(value) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value)
        ):
            raise ValueError("node name must be a lowercase DNS label of at most 63 characters")
        return value

    @field_validator("ssh_user")
    @classmethod
    def safe_identifier(cls, value):
        if value is not None and not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,62}", value):
            raise ValueError("invalid SSH user")
        return value


class Inventory(Model):
    head: NodeSpec
    workers: list[NodeSpec] = Field(default_factory=list)
    ssh_user: str = "ubuntu"
    ssh_key: Path | None = None
    ssh: dict[str, str] = Field(default_factory=dict)
    sandbox_domain: str | None = None
    k3s_version: str = "v1.34.5+k3s1"
    gvisor_version: str = "latest"
    cilium_version: str = "1.18.14"
    registries: dict[str, RegistrySource] = Field(default_factory=dict)
    pod_cidr: str = "10.42.0.0/16"
    service_cidr: str = "10.43.0.0/16"

    @field_validator("sandbox_domain")
    @classmethod
    def valid_domain(cls, value):
        if value is not None:
            domain, _, port = value.partition(":")
            valid = len(domain) <= 253 and all(
                len(label) <= 63 and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in domain.split(".")
            )
            if ":" in value:
                valid = valid and port.isdigit() and 1 <= int(port) <= 65535
            if not valid:
                raise ValueError(
                    "sandbox_domain must be a lowercase DNS name with an optional valid port"
                )
        return value

    @field_validator("k3s_version", "gvisor_version", "cilium_version")
    @classmethod
    def valid_version(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9.+_-]+", value):
            raise ValueError("invalid release version")
        return value

    @model_validator(mode="after")
    def distinct(self):
        if set(self.ssh) - {"user", "key"}:
            raise ValueError("ssh supports user and key")
        self.ssh_user = self.ssh.get("user", self.ssh_user)
        if self.ssh.get("key"):
            self.ssh_key = Path(self.ssh["key"])
        hosts = [node.host for node in [self.head, *self.workers]]
        if len(hosts) != len(set(hosts)):
            raise ValueError("head and workers must have distinct addresses")
        names = [
            node.name
            or (
                "opensandbox-head" if node is self.head else "worker-" + node.host.replace(".", "-")
            )
            for node in [self.head, *self.workers]
        ]
        if len(names) != len(set(names)):
            raise ValueError("node names must be unique")
        if not self.workers and not self.head.sandboxes:
            raise ValueError("a cluster needs at least one sandbox worker")
        NodeSpec(host=self.head.host, ssh_user=self.ssh_user)
        networks = [ipaddress.ip_network(self.pod_cidr), ipaddress.ip_network(self.service_cidr)]
        if any(n.version != 4 for n in networks) or networks[0].overlaps(networks[1]):
            raise ValueError("pod and service CIDRs must be distinct IPv4 networks")
        if any(ipaddress.ip_address(host) in net for host in hosts for net in networks):
            raise ValueError("node addresses must be outside pod and service networks")
        return self

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate(yaml.safe_load(path.read_text()))
