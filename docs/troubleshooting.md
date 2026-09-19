# Troubleshooting

Start with `opensandbox status`, `opensandbox nodes`, `opensandbox doctor` and
`opensandbox logs` on the head. Use `--json` for machine-readable output.

| Symptom | Check |
| --- | --- |
| Setup rejects the head IP | Run setup on that machine; `head.host` must be one of its IPv4 addresses. |
| SSH or sudo fails | Verify the host key, SSH key/user/port and passwordless sudo on the worker. |
| Existing cluster is rejected | Use clean machines; setup will not overwrite an unmanaged K3s cluster. |
| Worker never joins | Worker → head TCP 6443, node names, CIDR conflicts and `opensandbox logs --node NAME`. |
| Node remains unavailable | Cilium connectivity, Linux kernel support and gVisor smoke-test output. No fallback runtime is enabled. |
| Sandbox creation times out | Available CPU/RAM/disk on one node, image pull credentials, architecture and image compatibility. |
| E2B commands fail to start | The image needs `/bin/bash`. Use the supplied `base` template to establish a baseline. |
| HTTPS certificate error | Trust `api-ca.crt` on the client. Python's native macOS verifier needs Keychain trust; Node supports `NODE_EXTRA_CA_CERTS`. |
| getHost URL cannot resolve | Allow sslip.io DNS or configure your own wildcard DNS and matching `sandbox_domain`. |
| SDK works but app URL fails | Bind the app port, confirm the returned hostname points to the head, and preserve Host in any reverse proxy. |
| Runtime access returns unauthorized | Check the sandbox token, owner-key revocation and sandbox lifetime. API keys and runtime tokens are distinct. |
| Private application returns unauthorized | Send the returned traffic token in `e2b-traffic-access-token`. |
| No internet/DNS in sandbox | Check egress rules and reachability of public DNS. Private/cluster/metadata access is deliberately blocked. |
| Build cannot schedule | One worker needs the build request plus overhead: 2 cores, 4 GiB RAM and 20 GiB disk. |
| Build fails pulling a private base | Configure registry credentials and any external `token_realm`, then rerun setup. |
| Lost sandbox after worker failure | Sandboxes are ephemeral. Create a replacement and restore application data from your own durable store. |
| Node removal times out | It is already cordoned. Wait for workloads or explicitly use `--force`. The machine is never terminated. |
| Image deletion does not free disk | Registry blob garbage collection is a separate maintenance operation. |
| API fails after reboot | Inspect `sudo journalctl -u opensandbox` and `sudo journalctl -u k3s`. Saved state is under `/var/lib/opensandbox`. |

Run `opensandbox restart` after deliberate head configuration changes. Existing
sandbox processes survive API restarts, while builds interrupted by a restart
must be retried. Rerun initialization to refresh the API certificate before its
one-year leaf lifetime expires; the ten-year cluster CA remains unchanged.

For deeper infrastructure diagnostics, administrators can use K3s tools on the
head. Applications do not need Kubernetes configuration or worker addresses.
Do not delete state directories as a general troubleshooting step.
