"""OpenSandbox administration on the head and remote sandbox commands."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from opensandbox import OpenSandbox
from opensandbox.cluster.admin import Admin
from opensandbox.cluster.bootstrap import Bootstrap, Runner
from opensandbox.config import Inventory, Settings
from opensandbox.errors import ConfigurationError, OpenSandboxError

app = typer.Typer(
    no_args_is_help=True, help="OpenSandbox: gVisor sandboxes on your Linux CPU machines."
)
node_app = typer.Typer(no_args_is_help=True)
image_app = typer.Typer(no_args_is_help=True)
key_app = typer.Typer(no_args_is_help=True)
app.add_typer(node_app, name="node")
app.add_typer(image_app, name="image")
app.add_typer(key_app, name="key")
console = Console(stderr=True)
JSON = Annotated[bool, typer.Option("--json", help="Print JSON for automation.")]


def encode(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


def output(value, as_json=True):
    # Structured output is also the default so commands remain easy to script.
    typer.echo(json.dumps(value, default=encode, indent=None if as_json else 2))


def client():
    if (
        not (os.environ.get("OPENSANDBOX_API_KEY") or os.environ.get("E2B_API_KEY"))
        and Path("/etc/opensandbox/config.yaml").is_file()
    ):
        try:
            settings = Settings.load()
        except PermissionError:
            import yaml

            reader = Runner(Inventory(head={"host": "10.0.0.1"}))
            settings = Settings(
                **yaml.safe_load(reader.run(["cat", "/etc/opensandbox/config.yaml"], root=True))
            )
        return OpenSandbox(
            settings.endpoint, settings.api_key.get_secret_value(), ca_cert=settings.api_ca
        )
    return OpenSandbox()


def require_head():
    if platform.system() != "Linux":
        raise ConfigurationError("Run this command on your Linux OpenSandbox head machine")


@app.command("init")
def initialize(nodes: Annotated[Path, typer.Option("--nodes", exists=True)], as_json: JSON = False):
    """Bootstrap existing Linux CPU machines from the repository on the head."""
    require_head()
    result = Bootstrap(Inventory.load(nodes), Path.cwd(), progress=console.print).initialize()
    output(result, as_json)


@app.command()
def start(as_json: JSON = False):
    """Start the head API service."""
    require_head()
    subprocess.run(["sudo", "systemctl", "start", "opensandbox"], check=True)
    output({"started": True}, as_json)


@app.command()
def stop(as_json: JSON = False):
    """Stop the API service; Kubernetes keeps sandboxes and deadlines running."""
    require_head()
    subprocess.run(["sudo", "systemctl", "stop", "opensandbox"], check=True)
    output({"stopped": True}, as_json)


@app.command()
def restart(as_json: JSON = False):
    """Restart the head API; running sandbox processes stay on their workers."""
    require_head()
    subprocess.run(["sudo", "systemctl", "restart", "opensandbox"], check=True)
    output({"restarted": True}, as_json)


@app.command()
def status(as_json: JSON = False):
    with client() as api:
        state = api.status()
        if as_json:
            output(state)
            return
        nodes = state["nodes"]
        healthy = sum(n["ready"] for n in nodes)
        typer.echo(
            f"OpenSandbox {'healthy' if nodes and healthy == len(nodes) else 'needs attention'}"
        )
        typer.echo(f"Nodes: {healthy}/{len(nodes)} healthy")
        typer.echo(
            f"CPU: {sum(n['cpu_available'] for n in nodes):g} / {sum(n['cpu'] for n in nodes):g} cores available"
        )
        typer.echo(
            f"RAM: {sum(n['memory_available'] for n in nodes) / 2**30:.1f} / {sum(n['memory'] for n in nodes) / 2**30:.1f} GiB available"
        )
        typer.echo(f"Sandboxes: {sum(s['state'] == 'running' for s in state['sandboxes'])} running")
        typer.echo("Runtime: gVisor")
        typer.echo(f"E2B API: {api._client.http.base_url}")


@app.command("nodes")
def list_nodes(as_json: JSON = False):
    with client() as api:
        nodes = api.nodes()
        if as_json:
            output(nodes)
            return
        table = Table(
            "NODE", "CPU available/total", "RAM GiB available/total", "SANDBOXES", "STATUS"
        )
        for node in nodes:
            table.add_row(
                node["name"],
                f"{node['cpu_available']:g}/{node['cpu']:g}",
                f"{node['memory_available'] / 2**30:.1f}/{node['memory'] / 2**30:.1f}",
                str(node["sandboxes"]),
                "ready"
                if node["schedulable"]
                else "control-only"
                if node["ready"]
                else "unavailable",
            )
        Console().print(table)


@app.command("sandboxes")
def sandboxes(as_json: JSON = False):
    with client() as api:
        output(api.list(), as_json)


@node_app.command("add")
def node_add(
    host: str,
    key: Annotated[Path | None, typer.Option("--ssh-key")] = None,
    port: Annotated[int, typer.Option("--ssh-port", min=1, max=65535)] = 22,
    as_json: JSON = False,
):
    require_head()
    output(Admin().add(host, ssh_key=key, ssh_port=port), as_json)


@node_app.command("remove")
def node_remove(
    name: str,
    force: Annotated[bool, typer.Option("--force")] = False,
    timeout: Annotated[int, typer.Option(min=0)] = 300,
    as_json: JSON = False,
):
    require_head()
    output(Admin().remove(name, force=force, timeout=timeout), as_json)


@app.command("create")
def create(  # noqa: PLR0917 - CLI option parameters
    image: Annotated[str, typer.Option()] = "python:3.13-slim",
    cpu: Annotated[float, typer.Option(min=0.01)] = 1,
    memory: str = "1gb",
    disk: str = "10gb",
    timeout: int = 3600,
    internet: Annotated[bool, typer.Option("--internet/--no-internet")] = True,
    as_json: JSON = False,
):
    with client() as api:
        sandbox = api.create(
            image=image,
            cpu=cpu,
            memory=memory,
            disk=disk,
            timeout=timeout,
            network={"internet": internet},
        )
        output(sandbox.info, as_json)


@app.command("exec")
def execute(
    sandbox_id: str,
    command: str,
    background: Annotated[bool, typer.Option("--background", "-b")] = False,
    timeout: float | None = None,
    as_json: JSON = False,
):
    with client() as api:
        sandbox = api.get(sandbox_id)
        if background:
            output({"id": sandbox.exec_background(command, timeout=timeout).id}, as_json)
        else:
            result = sandbox.exec(command, timeout=timeout)
            if as_json:
                output(result)
            else:
                typer.echo(result.stdout, nl=False)
                typer.echo(result.stderr, err=True, nl=False)
            if result.exit_code:
                raise typer.Exit(result.exit_code if 0 < result.exit_code < 256 else 1)


@app.command("cp")
def copy(source: str, destination: str, as_json: JSON = False):
    with client() as api:
        if ":" in source and source.startswith("sb-"):
            sandbox_id, remote = source.split(":", 1)
            api.get(sandbox_id).download(remote, destination)
        elif ":" in destination and destination.startswith("sb-"):
            sandbox_id, remote = destination.split(":", 1)
            api.get(sandbox_id).upload(source, remote)
        else:
            raise ConfigurationError("Use cp LOCAL SANDBOX:/path or cp SANDBOX:/path LOCAL")
    output({"copied": True}, as_json)


@app.command("kill")
def kill(sandbox_id: str, as_json: JSON = False):
    with client() as api:
        output(api.get(sandbox_id).destroy(), as_json)


@app.command("logs")
def logs(sandbox_id: str | None = None, node: str | None = None, as_json: JSON = False):
    if sandbox_id:
        with client() as api:
            results = api.get(sandbox_id).list_processes()
            if as_json:
                output(results)
            else:
                for result in results:
                    typer.echo(result.stdout, nl=False)
                    typer.echo(result.stderr, err=True, nl=False)
    else:
        require_head()
        text = Admin().logs(node)
        if as_json:
            output({"logs": text})
        else:
            typer.echo(text, nl=False)


@app.command("images")
def images(as_json: JSON = False):
    with client() as api:
        output(api.images(), as_json)


@image_app.command("build")
def build(
    path: Path, name: Annotated[str | None, typer.Option("--name")] = None, as_json: JSON = False
):
    with client() as api:
        output(api.build(path, name=name), as_json)


@image_app.command("register")
def register_image(
    name: str,
    image: str,
    *,
    cpu: float = 1,
    memory: str = "1Gi",
    disk: str = "10Gi",
    as_json: JSON = False,
):
    """Register an existing OCI image as an E2B template."""
    with client() as api:
        output(
            api.request(
                "PUT",
                "/v1/templates/" + name,
                json={"image": image, "cpu": cpu, "memory": memory, "disk": disk},
            ).json(),
            as_json,
        )


@image_app.command("delete")
def delete_image(name: str, as_json: JSON = False):
    """Delete a template alias, or an unused built image by its full reference."""
    with client() as api:
        if "@sha256:" in name:
            api.request("DELETE", "/v1/images", params={"reference": name})
        else:
            api.request("DELETE", "/v1/templates/" + name)
        output({"deleted": name}, as_json)


@key_app.command("create")
def create_key(
    name: str, admin: Annotated[bool, typer.Option("--admin")] = False, as_json: JSON = False
):
    """Issue an E2B-compatible key. Its secret is shown only once."""
    with client() as api:
        output(api.request("POST", "/v1/keys", json={"name": name, "admin": admin}).json(), as_json)


@key_app.command("list")
def list_keys(as_json: JSON = False):
    with client() as api:
        output(api.request("GET", "/v1/keys").json(), as_json)


@key_app.command("revoke")
def revoke_key(key_id: str, as_json: JSON = False):
    with client() as api:
        api.request("DELETE", "/v1/keys/" + key_id)
        output({"revoked": key_id}, as_json)


@app.command("doctor")
def doctor(as_json: JSON = False):
    with client() as api:
        result = api.doctor()
        if platform.system() == "Linux" and Path("/etc/opensandbox/nodes.yaml").is_file():
            hosts = Admin().diagnose_machines()
            result["checks"].update(hosts["checks"])
            result["details"].update(hosts["details"])
            result["machines"] = hosts["machines"]
            result["ok"] = all(result["checks"].values())
        output(result, as_json)
        if not result["ok"]:
            raise typer.Exit(1)


@app.command("metrics")
def metrics(as_json: JSON = False):
    with client() as api:
        output(api.metrics(), as_json)


def main():
    try:
        app()
    except (OpenSandboxError, OSError, ValueError) as exc:
        console.print(str(exc), markup=False)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
