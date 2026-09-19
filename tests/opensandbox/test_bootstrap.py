"""Installer checks that can run without changing the host or claiming a live cluster."""

import subprocess
from pathlib import Path

import pytest

from opensandbox.cluster.bootstrap import Bootstrap, Runner
from opensandbox.cluster.manifests import VERIFIED
from opensandbox.cluster.scripts import PREFLIGHT, install_node, write_file
from opensandbox.config import Inventory, Settings
from opensandbox.errors import RuntimeError


def test_generated_install_scripts_parse_and_write_exact_bytes(tmp_path):
    inventory = Inventory(head={"host": "10.0.0.1"}, workers=[{"host": "10.0.0.2"}])
    for node in [inventory.head, *inventory.workers]:
        script = install_node(
            inventory,
            node,
            head=node == inventory.head,
            token="token'with$quotes",
            registry_config={"configs": {"registry": {"auth": {"password": "literal'$(false)"}}}},
            ca="certificate\n",
        )
        subprocess.run(["bash", "-n"], input=script.encode(), check=True)
    # Exercise just the isolated file writer, never installation commands.
    destination = tmp_path / "filename with ' quotes"
    content = "literal $HOME `false` $(false)\nsecond line\n"
    subprocess.run(["bash", "-s"], input=write_file(str(destination), content).encode(), check=True)
    assert destination.read_text() == content
    assert destination.stat().st_mode & 0o777 == 0o600


def test_all_machine_preflights_finish_before_installation(monkeypatch, tmp_path):
    import opensandbox.cluster.bootstrap as module

    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/go.mod").write_text("module test")
    (tmp_path / "pyproject.toml").write_text("")
    inventory = Inventory(head={"host": "10.0.0.1"}, workers=[{"host": "10.0.0.2"}])
    runner = Runner(inventory)
    scripts = []

    def script(value, node=None):
        scripts.append(value)
        if node:
            raise RuntimeError("worker unavailable")
        return "head resources"

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runner, "run", lambda *a, **kw: '[{"addr_info":[{"family":"inet","local":"10.0.0.1"}]}]'
    )
    monkeypatch.setattr(runner, "script", script)
    with pytest.raises(RuntimeError, match="worker unavailable"):
        Bootstrap(inventory, tmp_path, runner, progress=lambda _: None).initialize()
    assert scripts == [PREFLIGHT, PREFLIGHT]


@pytest.mark.parametrize("failure", [False, True])
def test_runtime_verification_removes_stale_eligibility_and_fails_closed(tmp_path, failure):
    inventory = Inventory(head={"host": "10.0.0.1"})
    config = Settings(
        api_key="k" * 40,
        registry_password="r" * 40,
        state_dir=tmp_path,
        registry="10.0.0.1:30500",
        agent_image="10.0.0.1:30500/agent@sha256:test",
    )
    calls, applied = [], []

    class FakeRunner:
        def apply(self, objects):
            applied.extend(objects)

        def kubectl(self, *args, **kwargs):
            calls.append(args)
            if args[0] == "wait" and failure:
                raise RuntimeError("gVisor smoke failed")

    bootstrap = Bootstrap(inventory, Path.cwd(), FakeRunner(), progress=lambda _: None)
    if failure:
        with pytest.raises(RuntimeError, match="gVisor smoke failed"):
            bootstrap._verify_nodes(config)
    else:
        bootstrap._verify_nodes(config)
    assert calls[0] == ("label", "node", "opensandbox-head", VERIFIED + "-", "--overwrite")
    assert any(call[0:2] == ("delete", "pod") for call in calls)
    enabled = ("label", "node", "opensandbox-head", VERIFIED + "=verified", "--overwrite")
    assert (enabled in calls) is not failure
    assert (
        any(o["kind"] == "RuntimeClass" and o["metadata"]["name"] == "gvisor" for o in applied)
        is not failure
    )


@pytest.mark.parametrize("user", ["root", "ubuntu"])
def test_ssh_root_does_not_require_sudo(monkeypatch, user):
    inventory = Inventory(
        head={"host": "10.0.0.1"}, workers=[{"host": "10.0.0.2", "ssh_user": user}]
    )
    runner = Runner(inventory)
    calls = []
    monkeypatch.setattr(runner, "run", lambda args, **kw: calls.append(args))
    runner.script(PREFLIGHT, inventory.workers[0])
    assert calls[0][-1] == ("bash -s" if user == "root" else "sudo -n bash -s")
    assert "StrictHostKeyChecking=yes" in calls[0]
