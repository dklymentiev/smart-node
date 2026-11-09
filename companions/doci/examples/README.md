# DOCI — Examples

Runnable, minimal usage samples for the three client surfaces:
REST (curl + Python), MCP (Claude Desktop / Claude Code config),
and the `deep` CLI. Every example targets the **dev** stack
(`docker compose -f docker-compose.dev.yml up -d`); pointing them at
production is just swapping the URL and adding an API key.

| File | What |
|---|---|
| [`curl-rest.sh`](curl-rest.sh) | Walks the document lifecycle (create → list → read → thread → version → delete) using only `curl` + `jq` |
| [`python-rest-client.py`](python-rest-client.py) | Same lifecycle, but as a single-file `DOCIClient` class — copy/paste into agents that don't speak MCP |
| [`mcp-claude-desktop.json`](mcp-claude-desktop.json) | Drop-in MCP server entry for Claude Desktop / Claude Code's `mcp.json` |

## Auth

The dev stack auto-authenticates the browser as user `dev`. For
service-to-service calls you still need the API key:

```bash
export DOCI_API_URL="http://localhost:8080/api"
export DOCI_API_KEY="$(grep DOCI_API_KEY_HASH .env >/dev/null && echo 'see RELEASING.md to generate')"
```

In production, the proxy issues the browser cookie; agents and CI
keep using `X-API-Key`. See
[`docs/07-operations.md`](../docs/07-operations.md) for key
generation and rotation.

## What's NOT here

- Web UI screenshots — the UI is the canonical playground; just
  open `http://localhost:8080`.
- Schema validation — that's the job of
  [`../schemas/`](../schemas/).
- Live benchmark runner — see
  [`docs/06-testing-strategy.md`](../docs/06-testing-strategy.md).
