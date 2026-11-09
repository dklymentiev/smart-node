# Planner

> Self-hostable open-source task management with a real lifecycle
> (intake -> assessment -> approval -> execution -> verification -> delivery),
> hierarchical objectives, and first-class CLI / REST / MCP / Web UI interfaces.

**Status:** v0.1.0 -- first working release. See [CHANGELOG.md](CHANGELOG.md).

## Quick start

```bash
git clone <repo> planner && cd planner
docker compose up -d
# Watch the app log for the root API key (printed once on first start):
docker compose logs app | grep X-API-Key
# Open the UI:
open http://localhost:8080/ui/
```

Sign in with the printed key. From there you can create projects, tasks,
and walk them through the full pipeline.

## What it does

- **Six-stage task pipeline.** Tasks move through `open -> assessing ->
  awaiting_approval -> in_progress -> pending_review -> done`, with paused
  states (`blocked`, `escalated`), terminal `rejected`, and post-delivery
  `incident -> hotfix + post-mortem`. Every transition is validated against
  a matrix and atomically writes an audit log row + a notification queue
  entry.
- **Objectives hierarchy.** Strategy / roadmap / milestone / goal /
  initiative / campaign in any nesting; tasks attach at any level.
  Recursive progress aggregation in one CTE.
- **Four equally capable interfaces** sharing one repo layer:
  - **REST** at `/api/v1/...` (OpenAPI docs at `/docs`)
  - **MCP** stdio server (`python -m planner.mcp_server`) -- 24 tools
  - **CLI** (`planner ...`) talking to REST
  - **Web UI** at `/ui/` -- login, project board (kanban), task detail,
    objective tree, API key management
- **Block / unblock with cycle detection**, cascading unblock when a
  blocker reaches a terminal status, two-strike auto-escalation on review
  bounce-backs, scope conflict detection, and a periodic staleness sweep.
- **API key auth.** SHA-256 hashed at rest. Root key bootstrapped on
  first start.

## Two deployment modes

### 1. Standalone (bundled Postgres via Docker Compose)

```bash
git clone <repo> planner && cd planner
docker compose up -d
```

That's it. The `db` service brings up Postgres 16, the `app` service runs
migrations and starts uvicorn on port 8080.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://planner:planner@db:5432/planner` | DSN to the bundled database |
| `PLANNER_LOG_LEVEL` | `info` | Log verbosity |

### 2. Embedded (your existing Postgres)

The operator already runs Postgres and points Planner at it.

```bash
createdb planner_app
psql planner_app -c 'CREATE SCHEMA IF NOT EXISTS planner;'   # optional

export DATABASE_URL='postgresql://user:pass@host:5432/planner_app'
export PLANNER_SCHEMA=planner       # optional; omit for default schema
python -m planner.install           # apply migrations only

uvicorn planner.app:app --host 0.0.0.0 --port 8080
```

`python -m planner.install` is idempotent. The first start prints a root
API key once -- capture it.

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | DSN to the host database |
| `PLANNER_SCHEMA` | no | Schema name; used as `search_path` |
| `PLANNER_MIGRATIONS_DIR` | no | Override migrations path |

## Verifying an install

```bash
./scripts/install-verify.sh
```

This brings the stack up, waits for `/health` to report `ok`, extracts
the root API key from container logs, creates a project + task via REST,
and asserts everything finishes in under five minutes.

## Tests

```bash
# Unit tests only (no DB required)
pytest tests/ -q -k "not integration and not live"

# Full integration suite against a real Postgres 16
docker exec common-postgres psql -U postgres -c "CREATE DATABASE planner_oss_test;"
PYTHONPATH=src \
  PLANNER_TEST_DSN="postgresql://postgres:PASSWORD@127.0.0.1:5433/planner_oss_test" \
  pytest tests/ -q

# Live tests (uvicorn subprocess + REST/MCP/CLI clients) -- opt in
PYTHONPATH=src \
  PLANNER_TEST_DSN="..." PLANNER_LIVE=1 \
  pytest tests/live/ -q
```

## Documentation

- `docs/01-vision.md` -- product vision
- `docs/02-spec.md` -- functional spec
- `docs/03-architecture.md` -- design decisions, data model
- `docs/data-dictionary.md` -- per-column reference

The roadmap lives in the planner itself, not in a static document.

## License

[MIT](LICENSE)
