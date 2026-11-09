# DOCI — Quickstart

The fastest path from `git clone` to a document an agent can read,
write, and discuss. README has the marketing pitch; this is the
operator's-eye view: every step explained, every gotcha called out.

For production deploy, monitoring, and incident response, see
[`07-operations.md`](07-operations.md). For the full HTTP/CLI/MCP
contract, see [`05-api-reference.md`](05-api-reference.md).

## 1. Boot the dev stack

```bash
git clone https://github.com/dklymentiev/doci.git
cd doci
docker compose -f docker-compose.dev.yml up -d
```

What that does:

- Builds the `doci` image (Apache 2.4 + PHP 8.2).
- Starts an embedded `postgres:16-alpine` on host port **5433**
  (production stacks attach to an external Postgres instead).
- Applies every migration in `migrations/*.sql` via
  `docker-entrypoint-initdb.d` on first start; the app re-applies
  them idempotently on every restart.
- Seeds whatever sits under `files/` into git and indexes each file
  into the `documents` table.

Confirm it came up:

```bash
curl -fsS http://localhost:8080/api/health.php | jq .
# → {"status":"ok", "db":"connected", "files_writable":true, ...}
```

Visit <http://localhost:8080>. Because the dev compose sets
`DOCI_ENV=development` and `DOCI_DEV_AUTO_AUTH=true`, you are
auto-authenticated as user `dev` — no API key needed for the browser
flow. (Production refuses to honour `DOCI_DEV_AUTO_AUTH=true` without
the explicit `DOCI_ENV=development` assertion.)

## 2. Create your first document via API

The dev stack ships with a known `DOCI_API_KEY_HASH` (sha256 of
`dev-test-key-not-for-production`), so any value in `X-API-Key`
fails — pass the documented dev key, or generate your own hash for
prod (see [`07-operations.md`](07-operations.md#configuration)).

```bash
export DOCI_API_URL=http://localhost:8080/api
export DOCI_API_KEY=dev-key

curl -fsS -H "X-API-Key: $DOCI_API_KEY" -H "Content-Type: application/json" \
  -X POST "$DOCI_API_URL/documents.php" \
  -d '{"path":"notes/first","title":"My first DOCI doc","content":"# Hello\n\nFrom DOCI."}'
```

The response gives you a `guid` and a stable URL. Browse to
`http://localhost:8080/<guid>` to see it rendered.

Want the full lifecycle in one runnable file? See
[`../examples/curl-rest.sh`](../examples/curl-rest.sh) and
[`../examples/python-rest-client.py`](../examples/python-rest-client.py).

## 3. Wire it to an MCP agent

`mcp_server.py` is a FastMCP server. Twelve tools, each a thin
wrapper over the REST surface above — see
[`05-api-reference.md#mcp-tools`](05-api-reference.md) for the table.

Claude Desktop / Claude Code config (copy from
[`../examples/mcp-claude-desktop.json`](../examples/mcp-claude-desktop.json)):

```json
{
  "mcpServers": {
    "doci": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "/absolute/path/to/doci",
      "env": {
        "DOCI_API_URL": "http://localhost:8080/api",
        "DOCI_API_KEY": "dev-key"
      }
    }
  }
}
```

Restart the agent. It now sees `doci_create`, `doci_get`,
`doci_search`, etc.

Alternative transport for IDEs that prefer SSE:

```bash
DOCI_API_URL=http://localhost:8080/api \
DOCI_API_KEY=dev-key \
python mcp_server.py --transport sse  # SSE on port 8200
```

## 4. Use the `deep` CLI for quick capture

```bash
./deep inbox "Idea: chain doci with mesh-memory for semantic search."
./deep list                   # last 20 inbox items
./deep promote <guid> notes/ideas/semantic-search
```

`deep` is a POSIX shell wrapper over the same REST API; the
behaviour matches whatever the API does today.

## 5. Verify with the integration suite

```bash
make api-test                # or: ./tests/api-test.sh
```

That walks create → read → thread → reply → version → delete and
fails loud on any drift. CI (`.github/workflows/test.yml`) runs the
unit suite + PHPStan + `php -l` lint on every push matrixed across
PHP 8.2 and 8.3; the Docker-backed integration stage runs locally for
now (Phase 6 wires it into CI).

## Where to look next

| You want to … | Read |
|---|---|
| Understand WHY DOCI exists at all | [`01-vision.md`](01-vision.md) |
| See every operation and field | [`02-spec.md`](02-spec.md) |
| Understand the design decisions | [`03-architecture.md`](03-architecture.md) |
| Plan a deploy | [`07-operations.md`](07-operations.md) |
| Hook monitoring up | [`monitoring.md`](monitoring.md) |
| Cut a release | [`../RELEASING.md`](../RELEASING.md) |
| Validate request payloads | [`../schemas/`](../schemas/) |

## Common stumbles

- **Port 8080 already in use.** Edit the `ports:` line in
  `docker-compose.dev.yml`, restart.
- **`/api/health.php` returns 503.** Postgres is still starting,
  or `files/` is not writable. `docker logs doci-dev` shows the
  precise cause.
- **`make psql` connects but tables are empty.** The first-run
  migrations only run on a *fresh* postgres volume. To reset:
  `docker compose -f docker-compose.dev.yml down -v && up -d`.
- **MCP server can't see DOCI.** Check `DOCI_API_URL` is reachable
  *from the agent's host* (Claude Desktop ≠ Docker network). On
  Linux, `http://localhost:8080` works; on macOS / Windows MCP
  with Linux DOCI, point at the host IP.
