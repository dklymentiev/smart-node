# Instructions for Claude

Project: **DOCI** — a markdown workspace where AI agents read, write, and
discuss documents alongside the user.

## What it is

PHP 8.2 web app + PostgreSQL metadata + git-backed document storage.
- Hierarchical markdown documents (CRUD via REST API).
- Inbox quick-capture; promote to a structured document later.
- Threads anchored to selected passages on a document.
- Versions: every thread creation auto-snapshots the parent into a frozen
  `version` row; explicit snapshots also available.
- AI thread replies via any OpenAI-compatible `/chat/completions`
  endpoint (`ai.php`, `AI_GATEWAY_URL`).
- MCP server (`mcp_server.py`, FastMCP) — 12 tools, all thin wrappers
  over the REST surface.
- Canonical-documents registry: many-to-many `(guid, domain)` mapping
  for "first reach for these" lookups.

## Read these before changing anything substantive

The canonical doc set lives in `docs/`:

- [`docs/01-vision.md`](docs/01-vision.md) — why DOCI exists, non-goals.
- [`docs/02-spec.md`](docs/02-spec.md) — document model, operations.
- [`docs/03-architecture.md`](docs/03-architecture.md) — layers,
  design decisions DD-01…DD-06.
- [`docs/04-roadmap.md`](docs/04-roadmap.md) — phased plan with exit gates.
- [`docs/05-api-reference.md`](docs/05-api-reference.md) — HTTP + CLI + MCP
  contract; the authoritative source for request/response shapes.
- [`docs/06-testing-strategy.md`](docs/06-testing-strategy.md) — pyramid +
  the mandatory Live Benchmark Gate.
- [`docs/07-operations.md`](docs/07-operations.md) — deploy, env vars,
  migrations, incident response.
- [`docs/data-dictionary.md`](docs/data-dictionary.md) — `documents` and
  `key_documents` schemas, migrations 001–003.
- [`RELEASING.md`](RELEASING.md) — release checklist and semver policy.

## Key source files

- `index.php` — web UI + router (5 routes).
- `config.php` — config, auth, DB connection, env-var defaults.
- `documents.php` — repository layer (`registerDocument`,
  `getDocumentByGuid`, `getDocumentHierarchy`, …).
- `api/*.php` — 10 REST endpoints; the canonical contract.
- `lib/git.php`, `lib/validation.php`, `lib/response.php`,
  `lib/markdown.php`, `lib/mesh.php`, `lib/render.php`,
  `lib/key-documents.php` (canonical-docs registry backend).
- `ai.php` — OpenAI-compatible gateway adapter.
- `mcp_server.py` — FastMCP server.
- `migrations/00N_*.sql` — Postgres schema (idempotent, re-applied on
  every container start).
- `files/` — runtime document storage (gitignored at repo level; the
  container init's its own git tree there).

## Local dev

```bash
docker compose -f docker-compose.dev.yml up -d
# UI: http://localhost:8080  (the dev compose sets DOCI_ENV=development +
#                              DOCI_DEV_AUTO_AUTH=true, which auto-auths as "dev")
# DB: psql -h 127.0.0.1 -p 5433 -U doci_app -d doci
make api-test                       # integration suite against the running stack
composer test                       # unit suite
```

## Production rule worth memorising

**Always pass `--project-directory` when running `docker compose` for
the prod stack** (substitute your own project directory):

```bash
docker compose --project-directory /srv/doci \
  -f docker-compose.yml up -d --build doci
```

Running from another CWD drops the Traefik labels and the container
detaches from the reverse proxy. See
[`docs/07-operations.md`](docs/07-operations.md#production).

## Conventions

- Documents identified by **GUID** (stable). Paths are human-readable
  but renameable.
- Every mutation goes through git. `files/.git/` is the authoritative
  source of content; the Postgres `documents` table is a rebuildable
  index (`scripts/index-documents.php`).
- Auth: `X-API-Key` header (SHA-256 compared in constant time against
  `DOCI_API_KEY_HASH`), or external auth via reverse proxy passing
  `Remote-User`. The `Remote-User` header is honoured only when the
  request source IP is listed in `DOCI_TRUSTED_PROXIES`. CSRF token
  required for browser-flow mutations (`DOCI_CSRF_ENABLED=true` is
  the default; opt-out only for API-key-only deployments).
- New endpoints / new MCP tools / new container behaviour all require
  the **Live Benchmark Gate** in
  [`docs/06-testing-strategy.md`](docs/06-testing-strategy.md) before
  the next tag — unit and API tests are not a substitute.
- Migrations are forward-only and idempotent (`IF NOT EXISTS` /
  `OR REPLACE`). Never edit a migration that has shipped — write a new
  one.
