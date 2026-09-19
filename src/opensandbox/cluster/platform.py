"""Head infrastructure objects and the least-privilege API service account."""

import base64
import json

from opensandbox.cluster.manifests import MANAGED, default_deny, namespace


def secret(name, namespace_name, data, secret_type="Opaque"):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace_name},
        "type": secret_type,
        "data": {
            k: base64.b64encode(v if isinstance(v, bytes) else v.encode()).decode()
            for k, v in data.items()
        },
    }


def infrastructure(settings, head_name, cert, key, htpasswd):
    objects = [
        namespace(name)
        for name in ["opensandbox-system", settings.namespace, settings.build_namespace]
    ]
    objects += [
        default_deny(settings.namespace),
        default_deny(settings.build_namespace),
        deny_cluster_access(settings.namespace),
    ]
    objects += [
        secret("registry-tls", "opensandbox-system", {"tls.crt": cert, "tls.key": key}),
        secret("registry-auth", "opensandbox-system", {"htpasswd": htpasswd}),
    ]
    objects.append(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "registry", "namespace": "opensandbox-system"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "local-path",
                "resources": {"requests": {"storage": "20Gi"}},
            },
        }
    )
    objects.append(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "registry", "namespace": "opensandbox-system", "labels": MANAGED},
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": "opensandbox-registry"}},
                "template": {
                    "metadata": {"labels": {"app": "opensandbox-registry"}},
                    "spec": {
                        "nodeSelector": {"kubernetes.io/hostname": head_name},
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "registry",
                                "image": "registry:2.8.3",
                                "ports": [{"containerPort": 5000}],
                                "env": [
                                    {"name": k, "value": v}
                                    for k, v in {
                                        "REGISTRY_HTTP_TLS_CERTIFICATE": "/tls/tls.crt",
                                        "REGISTRY_HTTP_TLS_KEY": "/tls/tls.key",
                                        "REGISTRY_AUTH": "htpasswd",
                                        "REGISTRY_AUTH_HTPASSWD_REALM": "OpenSandbox",
                                        "REGISTRY_AUTH_HTPASSWD_PATH": "/auth/htpasswd",
                                        "REGISTRY_STORAGE_DELETE_ENABLED": "true",
                                    }.items()
                                ],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                                "volumeMounts": [
                                    {"name": "data", "mountPath": "/var/lib/registry"},
                                    {"name": "tls", "mountPath": "/tls", "readOnly": True},
                                    {"name": "auth", "mountPath": "/auth", "readOnly": True},
                                ],
                                "readinessProbe": {
                                    "tcpSocket": {"port": 5000},
                                    "initialDelaySeconds": 2,
                                    "periodSeconds": 2,
                                },
                            }
                        ],
                        "volumes": [
                            {"name": "data", "persistentVolumeClaim": {"claimName": "registry"}},
                            {"name": "tls", "secret": {"secretName": "registry-tls"}},
                            {"name": "auth", "secret": {"secretName": "registry-auth"}},
                        ],
                    },
                },
            },
        }
    )
    objects.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "registry", "namespace": "opensandbox-system"},
            "spec": {
                "type": "NodePort",
                "selector": {"app": "opensandbox-registry"},
                "ports": [{"port": 5000, "targetPort": 5000, "nodePort": 30500}],
            },
        }
    )
    auth = base64.b64encode(
        f"{settings.registry_username}:{settings.registry_password.get_secret_value()}".encode()
    ).decode()
    for name in [settings.namespace, settings.build_namespace]:
        objects.append(
            secret(
                settings.image_pull_secret,
                name,
                {".dockerconfigjson": json.dumps({"auths": {settings.registry: {"auth": auth}}})},
                "kubernetes.io/dockerconfigjson",
            )
        )
    return objects


def api_rbac(settings):
    ns = "opensandbox-system"
    objects = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "api", "namespace": ns},
            "automountServiceAccountToken": False,
        }
    ]
    subjects = [{"kind": "ServiceAccount", "name": "api", "namespace": ns}]
    for namespace_name in [settings.namespace, settings.build_namespace]:
        objects.extend(
            [
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {"name": "opensandbox-api", "namespace": namespace_name},
                    "rules": [
                        {
                            "apiGroups": [""],
                            "resources": ["pods", "pods/log", "pods/portforward"],
                            "verbs": ["get", "list", "create", "delete"],
                        },
                        {
                            "apiGroups": ["batch"],
                            "resources": ["jobs"],
                            "verbs": ["get", "list", "create", "patch", "delete"],
                        },
                        {
                            "apiGroups": ["networking.k8s.io"],
                            "resources": ["networkpolicies"],
                            "verbs": ["get", "create", "delete"],
                        },
                    ],
                },
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": "opensandbox-api", "namespace": namespace_name},
                    "subjects": subjects,
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "Role",
                        "name": "opensandbox-api",
                    },
                },
            ]
        )
    objects.extend(
        [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "opensandbox-observer"},
                "rules": [
                    {
                        "apiGroups": ["node.k8s.io"],
                        "resources": ["runtimeclasses"],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["nodes", "pods", "events"],
                        "verbs": ["get", "list"],
                    },
                    {
                        "apiGroups": ["metrics.k8s.io"],
                        "resources": ["nodes", "pods"],
                        "verbs": ["get", "list"],
                    },
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "opensandbox-observer"},
                "subjects": subjects,
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "opensandbox-observer",
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "api-token",
                    "namespace": ns,
                    "annotations": {"kubernetes.io/service-account.name": "api"},
                },
                "type": "kubernetes.io/service-account-token",
            },
        ]
    )
    return objects


def network_chart(inventory):
    import yaml

    return {
        "apiVersion": "helm.cattle.io/v1",
        "kind": "HelmChart",
        "metadata": {"name": "cilium", "namespace": "kube-system"},
        "spec": {
            "chart": "cilium",
            "repo": "https://helm.cilium.io/",
            "version": inventory.cilium_version,
            "targetNamespace": "kube-system",
            "bootstrap": True,
            "valuesContent": yaml.safe_dump(
                {
                    "ipam": {"mode": "kubernetes"},
                    "kubeProxyReplacement": False,
                    "k8sServiceHost": inventory.head.host,
                    "k8sServicePort": 6443,
                    "operator": {"replicas": 1},
                    "routingMode": "tunnel",
                    "tunnelProtocol": "vxlan",
                    "ipv6": {"enabled": False},
                    "policyCIDRMatchMode": "nodes",
                    "extraConfig": {"allow-localhost": "policy"},
                }
            ),
        },
    }


def deny_cluster_access(namespace_name):
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "deny-cluster-access", "namespace": namespace_name},
        "spec": {
            "endpointSelector": {},
            "egressDeny": [
                {"toEntities": ["host", "remote-node", "kube-apiserver", "cluster"]},
            ],
        },
    }
