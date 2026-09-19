"""Kubernetes objects with mandatory isolation. There is no alternate runtime."""

from __future__ import annotations

from datetime import datetime

from opensandbox.config import Settings
from opensandbox.models import CreateSandbox
from opensandbox.utils.sizes import parse_bytes, parse_duration

MANAGED = {"app.kubernetes.io/managed-by": "opensandbox"}
VERIFIED = "opensandbox.dev/gvisor"
AGENT_PORT = 49321

# IPv4-only cluster. Include link-local metadata, CGNAT, multicast and reserved space.
NON_PUBLIC = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
]


def runtime_class(settings: Settings):
    return {
        "apiVersion": "node.k8s.io/v1",
        "kind": "RuntimeClass",
        "metadata": {"name": settings.runtime_class, "labels": MANAGED},
        "handler": "runsc",
        "scheduling": {"nodeSelector": {VERIFIED: "verified"}},
        "overhead": {"podFixed": {"cpu": "50m", "memory": "64Mi"}},
    }


def sandbox_pod(settings: Settings, sandbox_id: str, request: CreateSandbox, expires: datetime):
    resources = {
        "cpu": f"{round(request.cpu * 1000)}m",
        "memory": str(parse_bytes(request.memory)),
        "ephemeral-storage": str(parse_bytes(request.disk)),
    }
    context = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": sandbox_id,
            "namespace": settings.namespace,
            "labels": {**MANAGED, "opensandbox.dev/sandbox": sandbox_id},
            "annotations": {"opensandbox.dev/expires-at": expires.isoformat()},
        },
        "spec": {
            "runtimeClassName": settings.runtime_class,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 1,
            "dnsPolicy": "None",
            "dnsConfig": {"nameservers": ["1.1.1.1", "8.8.8.8"]},
            "imagePullSecrets": [{"name": settings.image_pull_secret}],
            "initContainers": [
                {
                    "name": "install-agent",
                    "image": settings.agent_image,
                    "command": ["/agent", "install", "/tools/agent"],
                    "securityContext": context,
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                    "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
                }
            ],
            "containers": [
                {
                    "name": "sandbox",
                    "image": request.image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/opt/opensandbox/agent", "serve"],
                    "args": [
                        "--require-gvisor",
                        "--workdir",
                        request.workdir,
                        "--expires",
                        str(int(expires.timestamp())),
                        "--output-limit",
                        str(settings.max_output_bytes),
                        "--upload-limit",
                        str(settings.max_upload_bytes),
                    ],
                    "env": [{"name": k, "value": v} for k, v in request.env.items()],
                    "resources": {"requests": resources, "limits": resources},
                    "securityContext": {**context, "runAsUser": 0, "runAsGroup": 0},
                    "volumeMounts": [
                        {"name": "tools", "mountPath": "/opt/opensandbox", "readOnly": True}
                    ],
                    "readinessProbe": {
                        "exec": {"command": ["/opt/opensandbox/agent", "health"]},
                        "periodSeconds": 2,
                        "timeoutSeconds": 2,
                    },
                }
            ],
            "volumes": [{"name": "tools", "emptyDir": {"sizeLimit": "32Mi"}}],
        },
    }


def sandbox_job(settings: Settings, sandbox_id: str, request: CreateSandbox, expires: datetime):
    pod = sandbox_pod(settings, sandbox_id, request, expires)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": pod["metadata"],
        "spec": {
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
            "activeDeadlineSeconds": int(parse_duration(request.timeout)),
            "podReplacementPolicy": "Failed",
            "podFailurePolicy": {
                "rules": [
                    {
                        "action": "FailJob",
                        "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
                    },
                    {"action": "FailJob", "onExitCodes": {"operator": "NotIn", "values": [0]}},
                ]
            },
            "template": {"metadata": {"labels": pod["metadata"]["labels"]}, "spec": pod["spec"]},
        },
    }


def network_policy(settings: Settings, sandbox_id: str, internet: bool, node_ips: list[str]):
    # No allowed ingress. The authenticated head reaches loopback with Kubernetes port-forward.
    excluded = sorted(
        set(
            NON_PUBLIC
            + [settings.pod_cidr, settings.service_cidr]
            + [f"{ip}/32" for ip in node_ips]
        )
    )
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": sandbox_id, "namespace": settings.namespace, "labels": MANAGED},
        "spec": {
            "podSelector": {"matchLabels": {"opensandbox.dev/sandbox": sandbox_id}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": excluded}}]}]
            if internet
            else [],
        },
    }


def namespace(name: str):
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {**MANAGED, "pod-security.kubernetes.io/enforce": "baseline"},
        },
    }


def default_deny(name: str):
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"namespace": name, "name": "default-deny", "labels": MANAGED},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    }
