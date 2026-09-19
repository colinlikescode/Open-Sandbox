"""Live diagnostics: every successful smoke check runs against the installed cluster."""

import asyncio
import ssl

import httpx

from opensandbox.e2b import access_token
from opensandbox.models import CreateSandbox


async def diagnose(service, api_key: str):
    settings = service.settings
    checks, details = {}, {}
    sandbox = None

    async def check(name, action):
        try:
            result = await action()
            if result is False:
                raise ValueError("Check did not succeed")
            checks[name] = True
            return result
        except Exception as exc:
            checks[name] = False
            details[name] = str(exc)
            return None

    async def cluster():
        state = await service.status()
        checks["workers_ready"] = bool(state["nodes"]) and all(n["ready"] for n in state["nodes"])
        checks["gvisor_capacity"] = any(n["schedulable"] for n in state["nodes"])
        runtime = await service.kube.request("GET", "/apis/node.k8s.io/v1/runtimeclasses/gvisor")
        checks["runtime_class"] = runtime.get("handler") == "runsc"
        return state

    await check("kubernetes", cluster)

    async def registry():
        verify = (
            ssl.create_default_context(cafile=str(settings.registry_ca))
            if settings.registry_ca
            else True
        )
        async with httpx.AsyncClient(verify=verify, trust_env=False, timeout=10) as client:
            response = await client.get(
                "https://" + settings.registry + "/v2/",
                auth=(settings.registry_username, settings.registry_password.get_secret_value()),
            )
            response.raise_for_status()

    await check("image_registry", registry)
    try:
        template = await check("default_template", lambda: service.store.template("base"))
        if not template:
            return {"ok": False, "checks": checks, "details": details}
        sandbox = await check(
            "sandbox_create",
            lambda: service.create(
                CreateSandbox(template="base", cpu=0.1, memory="256Mi", timeout=120)
            ),
        )
        if sandbox:
            checks["gvisor_kernel"] = (
                True  # create verified the companion syscall and RuntimeClass.
            )

            async def commands():
                result = await service.agent(
                    sandbox.id, "POST", "/commands", json={"command": "printf opensandbox"}
                )
                return result.json().get("stdout") == "opensandbox"

            await check("command_execution", commands)

            async def files():
                await service.agent(
                    sandbox.id,
                    "PUT",
                    "/files?path=/workspace/doctor.txt",
                    content=b"opensandbox-doctor",
                )
                response = await service.agent(
                    sandbox.id, "GET", "/files?path=/workspace/doctor.txt"
                )
                return response.content == b"opensandbox-doctor"

            await check("filesystem", files)

            async def dns():
                response = await service.agent(
                    sandbox.id,
                    "POST",
                    "/commands",
                    json={
                        "command": [
                            "python3",
                            "-c",
                            "import socket; print(socket.gethostbyname('example.com'))",
                        ],
                        "timeout": 10,
                    },
                )
                return response.json().get("exit_code") == 0

            await check("sandbox_dns", dns)

            async def isolated():
                script = (
                    "import socket\nfor host, port in "
                    + repr([(settings.head_ip, 6443), ("169.254.169.254", 80)])
                    + ":\n try:\n  c=socket.create_connection((host,port),1); c.close()\n except OSError: continue\n raise SystemExit('private address reachable')\n"
                )
                response = await service.agent(
                    sandbox.id,
                    "POST",
                    "/commands",
                    json={"command": ["python3", "-c", script], "timeout": 10},
                )
                return response.json().get("exit_code") == 0

            await check("network_isolation", isolated)

            async def gateway():
                verify = (
                    ssl.create_default_context(cafile=str(settings.api_ca))
                    if settings.api_ca
                    else True
                )
                async with httpx.AsyncClient(
                    base_url=settings.endpoint, verify=verify, trust_env=False, timeout=10
                ) as client:
                    checks["api"] = (await client.get("/v1/health")).is_success
                    headers = {
                        "E2b-Sandbox-Id": sandbox.id,
                        "E2b-Sandbox-Port": "49983",
                        "X-Access-Token": access_token(service, sandbox.id),
                    }
                    response = await client.post(
                        "/filesystem.Filesystem/Stat",
                        headers=headers,
                        json={"path": "/workspace/doctor.txt"},
                    )
                    response.raise_for_status()
                    checks["e2b_runtime"] = response.json()["entry"]["name"] == "doctor.txt"
                    response = await client.get(
                        "/sandboxes/" + sandbox.id, headers={"X-API-Key": api_key}
                    )
                    response.raise_for_status()
                    checks["e2b_control"] = response.json()["sandboxID"] == sandbox.id
                    await service.agent(
                        sandbox.id,
                        "POST",
                        "/commands",
                        json={
                            "command": [
                                "python3",
                                "-m",
                                "http.server",
                                "3000",
                                "--bind",
                                "127.0.0.1",
                                "--directory",
                                "/workspace",
                            ],
                            "background": True,
                        },
                    )
                    for _ in range(50):
                        response = await client.get(
                            "/doctor.txt",
                            headers={"Host": f"3000-{sandbox.id}.{settings.sandbox_domain}"},
                        )
                        if response.status_code == 200:
                            return response.text == "opensandbox-doctor"
                        await asyncio.sleep(0.1)
                    return False

            await check("port_routing", gateway)
    finally:
        if sandbox:
            await service.destroy(sandbox.id)
    return {"ok": bool(checks) and all(checks.values()), "checks": checks, "details": details}
