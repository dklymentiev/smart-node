# tools -- node command-line tools shipped with core-api

Small host-side tools that `companions/core-api.sh` installs on the node. The
directory reaches the node as the `companions/tools:tools` sidecar of the
core-api companion (see `core-api.manifest.json`).

## integration

`integration` is a Python 3 script installed as `/usr/local/bin/integration`
(root, 0755) by `install_integration_cli` in `core-api.sh`. It provisions
MCP servers registered in `sn_config.mcp_servers` that are shipped as pip
packages ("Pattern A").

```
integration provision <slug>    provision the MCP server for this slug
integration provision --all     provision every server with autoinstall=true
integration list                list registered MCP servers
```

How it works:

- It reads the registry from core-api over the unix socket
  `/run/smart-node/api/core-api.sock` (override with `SN_CORE_API_SOCKET`),
  using `GET /api/mcp/servers` and `GET /api/mcp/servers/<slug>`. Run as
  root, the kernel identifies the caller on that socket.
- For a server whose `config` has `package_name`, it creates one venv per
  integration at `/srv/integrations/<slug>/venv/` (or under
  `config.install_path`) and pip-installs `package_name`, pinned to
  `version_pin` if set.
- `package_source` may be a local directory (used with `--find-links
  --no-index`), a pip-compatible URL, or absent (install from PyPI).
- A row without `package_name` is skipped as a manually installed server.
- It is idempotent: if the pinned version is already installed it exits 0.

Rows get their provisioning fields from `sn_mcp_server_register
--package-name/--version-pin/--package-source` in
`companions/_shared/integration.sh`. `core-api.sh` uses this path for the
Google Drive MCP server, building its wheel into
`/srv/integrations/packages/`, and runs `integration provision --all` during
its own install.

## Tests

There is no test directory here. `tests/trusted-origin-callers.test.sh` at
the repository root includes a check on `integration` (that it reaches
core-api over the socket).
