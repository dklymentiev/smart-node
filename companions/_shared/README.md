# _shared -- helpers sourced by companion installers

Shell code that several companion installers share. It is not a component
and installs nothing by itself; a companion carries it as the `_shared`
sidecar (listed in the `sidecars` field of its `*.manifest.json`) and sources
it from beside its own script.

## integration.sh

Registers what a companion provides with core-api, which owns the registries
in `sn_config`. Sourced today by `core-api.sh`, `agent-runtimes.sh`,
`memory.sh`, `documents.sh`, `tracking.sh`, `dispatcher.sh`, `workflows.sh`,
`communication.sh` and `hitl.sh`.

```bash
source "$SCRIPT_DIR/_shared/integration.sh"

sn_integration_register \
  --slug planner --label "Planner" --purpose "Tasks" --kind app \
  --install-uri "http://planner.example.com" \
  --install-path "/opt/smart-node/tracking" \
  --companion-slug tracking \
  [--endpoint URL] [--capabilities '["knowledge"]'] [--tools-json FILE]

sn_mcp_server_register --slug example-mcp --name "Example" \
  --transport http --url "http://localhost:8200/mcp"
sn_mcp_server_register --slug planner --name "Planner" \
  --transport stdio --command /path/to/python --args '["/path/to/server.py"]' \
  [--default-on] [--autoinstall] [--package-name P --version-pin V --package-source S]
```

| Function | core-api call |
|---|---|
| `sn_integration_register` / `sn_integration_unregister [--hard]` | `POST /api/integrations`, `DELETE /api/integrations/<slug>` |
| `sn_mcp_server_register` / `sn_mcp_server_unregister` | `POST /api/mcp/servers`, `DELETE /api/mcp/servers/<slug>` |
| `sn_runtime_register` / `sn_runtime_unregister [--hard]` | `POST /api/runtimes`, `DELETE /api/runtimes/<slug>` |

Behaviour worth knowing:

- Calls go over core-api's unix socket (`SN_CORE_API_SOCKET`, default
  `/run/smart-node/api/core-api.sock`, timeout `SN_CORE_API_TIMEOUT`, default
  5 s). The installer runs as root and the kernel names it on the socket; no
  identity header is sent.
- Failures are non-fatal: the functions log a warning and return 11 (core-api
  unreachable) or 12 (other HTTP error), so a companion install continues and
  an operator can re-run it later. Bad arguments return 2.
- `--default-on` puts an MCP server in every agent's `.mcp.json`;
  `--package-*` fields enable `integration provision <slug>` (see
  `companions/tools/README.md`).
- It reads `SMART_NODE_DOMAIN` from `/etc/smart-node/routing.env` (or
  `node.env`) so callers can build URLs, and sourcing it twice is a no-op.

## Tests

No tests live here. `tests/trusted-origin-callers.test.sh` at the repository
root checks that this file talks to core-api only over the socket and never
types an identity header.
