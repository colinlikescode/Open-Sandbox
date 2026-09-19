from datetime import timedelta

import pytest
from pydantic import ValidationError

from opensandbox.cluster.manifests import default_deny, network_policy, sandbox_job
from opensandbox.config import Inventory, Settings
from opensandbox.models import CreateSandbox, TimeoutRequest
from opensandbox.utils.clock import utcnow


def settings(tmp_path):
    return Settings(
        state_dir=tmp_path,
        api_key="a" * 40,
        registry_password="r" * 40,
        registry="10.0.0.1:30500",
        agent_image="10.0.0.1:30500/agent@sha256:abc",
    )


@pytest.mark.parametrize(
    "options",
    [
        {"cpu": 0},
        {"cpu": float("inf")},
        {"memory": "0"},
        {"disk": -1},
        {"timeout": 0},
        {"timeout": "8d"},
        {"workdir": "../tmp"},
        {"network": {"privateNetwork": True}},
        {"env": {"OPENSANDBOX_TOKEN": "value"}},
        {"privileged": True},
        {"image": ""},
    ],
)
def test_rejects_invalid_or_privileged_requests(options):
    with pytest.raises((ValidationError, ValueError)):
        CreateSandbox(**options)


def test_expiration_updates_validate_lifetime():
    with pytest.raises((ValidationError, ValueError)):
        TimeoutRequest(timeout=0)


def test_job_enforces_isolation_and_deadline(tmp_path):
    config = settings(tmp_path)
    job = sandbox_job(
        config,
        "sb-test",
        CreateSandbox(cpu=2, memory="4gb", disk="20gb", timeout=300),
        utcnow() + timedelta(seconds=300),
    )
    assert job["spec"]["activeDeadlineSeconds"] == 300
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["podReplacementPolicy"] == "Failed"
    pod = job["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "gvisor"
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert not any(v.get("hostPath") for v in pod["volumes"])
    assert not pod.get("hostNetwork")
    assert not pod.get("hostPID")
    for container in pod["containers"] + pod["initContainers"]:
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    resources = pod["containers"][0]["resources"]
    assert resources["limits"] == resources["requests"]
    assert resources["limits"]["ephemeral-storage"] == "20000000000"
    assert "activeDeadlineSeconds" not in pod  # Pod deadlines cannot be extended.


def test_network_has_default_deny_and_excludes_cluster_public_nodes(tmp_path):
    config = settings(tmp_path)
    assert default_deny(config.namespace)["spec"]["egress"] == []
    policy = network_policy(config, "sb-test", True, ["54.1.2.3"])["spec"]
    assert policy["ingress"] == []
    excluded = policy["egress"][0]["to"][0]["ipBlock"]["except"]
    for network in [
        "54.1.2.3/32",
        "169.254.0.0/16",
        "10.0.0.0/8",
        config.pod_cidr,
        config.service_cidr,
    ]:
        assert network in excluded
    assert network_policy(config, "sb-test", False, [])["spec"]["egress"] == []


def test_inventory_rejects_duplicate_or_overlapping_hosts():
    with pytest.raises(ValidationError):
        Inventory(head={"host": "10.0.0.1"}, workers=[{"host": "10.0.0.1"}])
    with pytest.raises(ValidationError):
        Inventory(head={"host": "10.42.0.1"})
    with pytest.raises(ValidationError):
        Inventory(head={"host": "10.0.0.1", "sandboxes": False})


@pytest.mark.parametrize(
    "options",
    [
        {"head": {"host": "10.0.0.1", "name": "invalid_name"}},
        {"sandbox_domain": "sandbox.example:65536"},
        {"sandbox_domain": "sandbox..example"},
    ],
)
def test_inventory_rejects_names_and_ports_that_cannot_be_installed(options):
    with pytest.raises(ValidationError):
        Inventory.model_validate({"head": {"host": "10.0.0.1"}, **options})
