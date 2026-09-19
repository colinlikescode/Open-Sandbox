"""Real Linux acceptance tests. Run explicitly after opensandbox init.

No mocked Kubernetes, commands, runtime attestation, DNS, or SDK transport.
Node failure and membership tests require their explicit environment inputs.
"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from opensandbox import OpenSandbox
from opensandbox.cluster.admin import Admin
from tests.opensandbox.test_e2b import python_scenario

pytestmark = pytest.mark.cluster
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cluster():
    if not os.environ.get("E2B_API_URL") or not os.environ.get("E2B_API_KEY"):
        pytest.fail(
            "Configure E2B_API_URL, E2B_SANDBOX_URL and E2B_API_KEY for the installed cluster"
        )
    with OpenSandbox(ca_cert=os.environ.get("SSL_CERT_FILE")) as client:
        yield client


def test_single_node_official_python_sdk(cluster):
    python_scenario(Path("/workspace"))


def test_single_node_official_typescript_sdk(cluster):
    subprocess.run(
        ["node", str(ROOT / "sdk/typescript/test/e2b.integration.mjs")],
        env={k: v for k, v in os.environ.items() if not k.startswith("TEST_")},
        check=True,
        timeout=180,
    )


def test_doctor_checks_real_runtime_network_files_and_proxy(cluster):
    report = cluster.doctor()
    assert report["ok"], json.dumps(report, indent=2)


@pytest.mark.timeout(1800)
def test_dockerfile_build_registry_and_named_e2b_template(cluster, tmp_path):
    from e2b import Sandbox

    name = "acceptance-" + uuid.uuid4().hex[:12]
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim\nWORKDIR /workspace\nCOPY marker.txt /workspace/marker.txt\n"
        "RUN printf 'built inside gVisor' > /workspace/build.txt\n"
    )
    (tmp_path / "marker.txt").write_text(name)
    built = None
    try:
        built = cluster.build(tmp_path, name=name)
        sandbox = Sandbox.create(name, timeout=120)
        try:
            assert sandbox.files.read("/workspace/marker.txt") == name
            assert sandbox.commands.run("cat /workspace/build.txt").stdout == "built inside gVisor"
            assert "gVisor" in sandbox.commands.run("/opt/opensandbox/agent verify").stdout
        finally:
            sandbox.kill()
    finally:
        if built:
            cluster.request("DELETE", "/v1/templates/" + name)
            cluster.request("DELETE", "/v1/images", params={"reference": built["reference"]})


def test_multi_node_placement_and_resource_limits(cluster):
    minimum = int(os.environ.get("OPENSANDBOX_ACCEPT_NODES", "1"))
    nodes = [n for n in cluster.nodes() if n["schedulable"]]
    assert len(nodes) >= minimum
    sandboxes = []
    try:
        # Internal acceptance may constrain a template by node; users never need
        # worker addresses. Cordon others temporarily to prove each placement path.
        admin = Admin()
        previously_cordoned = {
            n["metadata"]["name"]
            for n in json.loads(admin.runner.kubectl("get", "nodes", "-o", "json"))["items"]
            if n.get("spec", {}).get("unschedulable")
        }
        try:
            for target in nodes:
                for node in nodes:
                    admin.runner.kubectl(
                        "uncordon" if node["name"] == target["name"] else "cordon", node["name"]
                    )
                sandbox = cluster.create(cpu=0.25, memory="256Mi", disk="1Gi", timeout=180)
                sandboxes.append(sandbox)
                assert sandbox.info.node == target["name"]
                pods = json.loads(
                    admin.runner.kubectl(
                        "get",
                        "pods",
                        "-n",
                        admin.settings.namespace,
                        "-l",
                        "opensandbox.dev/sandbox=" + sandbox.id,
                        "-o",
                        "json",
                    )
                )["items"]
                pod = pods[0]["spec"]
                assert pod["runtimeClassName"] == "gvisor"
                assert pod["containers"][0]["resources"]["limits"] == {
                    "cpu": "250m",
                    "memory": "268435456",
                    "ephemeral-storage": "1073741824",
                }
                result = sandbox.exec(
                    "test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token && /opt/opensandbox/agent verify"
                )
                assert result.exit_code == 0 and "gVisor" in result.stdout
        finally:
            for node in nodes:
                admin.runner.kubectl(
                    "cordon" if node["name"] in previously_cordoned else "uncordon", node["name"]
                )
        assert len({s.info.node for s in sandboxes}) >= minimum
    finally:
        for sandbox in sandboxes:
            sandbox.destroy()


def test_worker_failure_is_lost_and_not_recreated(cluster):
    target = os.environ.get("OPENSANDBOX_ACCEPT_FAILURE_NODE")
    if not target:
        pytest.skip("Set OPENSANDBOX_ACCEPT_FAILURE_NODE to explicitly select a disposable worker")
    admin = Admin()
    worker = next(
        (
            n
            for n in admin.inventory.workers
            if (n.name or "worker-" + n.host.replace(".", "-")) == target
        ),
        None,
    )
    assert worker, "Failure testing must target a worker, never the head"
    nodes = [n for n in cluster.nodes() if n["schedulable"]]
    sandbox = None
    try:
        for node in nodes:
            if node["name"] != target:
                admin.runner.kubectl("cordon", node["name"])
        sandbox = cluster.create(cpu=0.1, memory="128Mi", timeout=180)
        assert sandbox.info.node == target
        admin.runner.script("systemctl stop k3s-agent\n", worker)
        for _ in range(90):
            if sandbox.refresh().state == "lost":
                break
            time.sleep(1)
        assert sandbox.info.state == "lost"
    finally:
        admin.runner.script("systemctl start k3s-agent\n", worker)
        for node in nodes:
            admin.runner.kubectl("uncordon", node["name"])
        if sandbox:
            sandbox.destroy()


def test_add_and_remove_existing_worker(cluster):
    target = os.environ.get("OPENSANDBOX_ACCEPT_ADD_NODE")
    if not target:
        pytest.skip("Set OPENSANDBOX_ACCEPT_ADD_NODE=user@IP for a spare existing machine")
    admin = Admin()
    added = admin.add(target)
    try:
        assert any(n["name"] == added["name"] and n["schedulable"] for n in cluster.nodes())
    finally:
        removed = admin.remove(added["name"], timeout=30)
        assert removed["machine_terminated"] is False
