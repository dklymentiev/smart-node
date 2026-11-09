# core-api

The node's controller: a FastAPI service that owns the write side of the
`sn_config` / `sn_core` schema in the node's Postgres. The operator panel,
Studio, the installers and the agents all go through it to read and change
agents, grants, tools, integrations, MCP servers, crons, policies, secrets,
messages, sessions and the event log. Without it the panel renders but cannot
write.

This directory is the source of truth (see `VENDORED`; the separate
`smart-node-core` repository was archived on 2026-08-15). Licence: the root
`LICENSE` (AGPL-3.0). The version is in `VERSION`, reported by `version.py`.

## How it fits the node

- Installed by `companions/core-api.sh` (manifest `core-api.manifest.json`, a
  core companion). The script syncs this directory to
  `/opt/smart-node/core-api`, builds a venv from `requirements.txt` and runs
  it as the native systemd unit `smart-node-core-api.service`.
- `serve.py` starts one uvicorn process on two doors: TCP port 8030 (override
  with `SMART_NODE_CORE_API_PORT`; services such as brin, voice and Studio use
  it) and the unix socket `/run/smart-node/api/core-api.sock`, where the
  kernel (SO_PEERCRED) names the calling agent or root.
- Traefik routes `panel.<domain>/api/*` to it behind the auth middleware.
- Postgres is reached over the local socket with peer auth (`db.py`); there is
  no `.env` and no password.
- The schema is NOT created here. Bootstrap stage B02 bakes it from
  `bootstrap/B02-sn-core-schema.sql`; `migrations/` is a reference record and
  nothing on a node executes it.

## Where to start reading

| Path | What it is |
|---|---|
| `main.py` | App, lifespan (pool, boot event, background loops), router list |
| `serve.py` | The TCP + unix-socket runner the unit starts |
| `auth.py` | Who the caller is: browser session, kernel-proved, or asserted |
| `permissions.py` | `requires("<permission>")`: one check per route against `sn_config.role_permissions`; a route with no declared permission stops startup |
| `routes/` | One module per API area (`agents.py`, `mcp.py`, `crons.py`, `planner.py` proxy, `hitl.py`, ...) |
| `schemas/` | Pydantic models for the registries |
| `lib/` | Shared logic: secret store, crontab projection, supervisor, system chat |
| `mcp_agents.py`, `mcp_reminders.py`, `mcp_secrets.py` | stdio MCP servers spawned for agents; `core-api.sh` registers them as `agents`, `reminders` and `secrets` |
| `seed-*.py` | Idempotent seeds run as `ExecStartPre` on every start |
| `companions/` | Host-side helpers: crontab and tool-registry renderers, prompt recomposition, grant sync, heartbeat |
| `role-library/` | Role templates; laid down read-only at `/srv/smart-node/specialists` |

Interactive API docs are served at `/api/_docs`; health at `/api/health`.

## Tests

`tests/` is a pytest suite (`pip install -r requirements-dev.txt`). Some tests
use the app against a live database (`conftest.py`); the permission tests
start a throwaway Postgres from the bootstrap SQL via `tests/disposable_pg.py`
and need `initdb` (set `PG_BIN`). `tests/live/` holds scripts meant to run on a
node against the real schema. From the repository root,
`tests/axis-b-core-api.test.sh` runs the `tests/axis_b` subset, and
`tests/run-source.sh` runs the source-level checks that cover this tree.

`Dockerfile` builds a local test image only; nodes do not run core-api in a
container. Changes are listed in `CHANGELOG.md`.
