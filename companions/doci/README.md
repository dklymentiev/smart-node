# DOCI

> Vendored in Smart Node from the upstream project
> [github.com/dklymentiev/doci](https://github.com/dklymentiev/doci) (MIT),
> with Smart Node integration additions (a Python MCP sidecar and node wiring).

**A markdown workspace where AI agents read, write, and discuss documents alongside you.**

Open a document, highlight a paragraph, ask an agent for feedback — the
conversation lives anchored to that exact passage. Every edit is a git
commit. Every thread is itself a document the agent can find later.

That's the whole pitch.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![PHP 8.2+](https://img.shields.io/badge/php-8.2%2B-blue.svg)](https://www.php.net/)
[![PostgreSQL 14+](https://img.shields.io/badge/postgres-14%2B-blue.svg)](https://www.postgresql.org/)

**Status:** v0.2.0-rc — canonical-documents feature + full doc canon +
the security hardening pass from the rein pre-release audit. See
[CHANGELOG.md](CHANGELOG.md).

## Pick DOCI if you

- run multiple AI agents and want them sharing one document store
- want every change tracked in `git log`, not a SaaS audit you can't grep
- want discussions tied to specific sentences, not floating page comments
- self-host on your own server

## Pick something else if you want

- a personal knowledge base with plugins and a graph view → **Obsidian**
- a team wiki with rich blocks and templates → **Notion** or **Outline**
- a published documentation site → **GitBook** or **MkDocs**
- real-time collaborative editing → **HedgeDoc**

## 60-second start

```bash
git clone https://github.com/dklymentiev/doci.git
cd doci
docker compose -f docker-compose.dev.yml up -d
# UI: http://localhost:8080
# DB: psql -h 127.0.0.1 -p 5433 -U doci_app -d doci
./tests/api-test.sh
```

The dev environment embeds PostgreSQL, applies migrations on first
start, seeds whatever is in `files/`, and auto-authenticates as user
`dev` — click around without an API key.

For production with external Postgres + Traefik, see
[`docker-compose.yml`](docker-compose.yml) and
[`docs/07-operations.md`](docs/07-operations.md).

## Features

- **Documents.** Markdown CRUD with stable GUID identifiers, human-readable
  paths, soft delete, tags, parent/child links.
- **Inbox.** Quick capture of free-form notes; promote to a structured
  document at a chosen path.
- **Threads.** Right-click any selection in a document to start a
  discussion anchored to that exact quote. Threads are themselves
  documents (`doc_type='thread'`).
- **Versions.** Starting a thread auto-snapshots the parent document.
  The version bar at the top of every page (`Original | DV-1 | DV-2`)
  navigates the snapshot chain.
- **AI thread replies.** Tick a checkbox when creating a thread or
  posting a reply — the configured AI gateway (any OpenAI-compatible
  `/chat/completions` endpoint) drafts a response inline.
- **HTML documents.** Files whose body starts with `<!DOCTYPE html>` or
  `<html>` render in a sandboxed `<iframe>` next to markdown docs in
  the same folder.
- **MCP server.** `mcp_server.py` (FastMCP) exposes 12 tools for AI
  agents: `doci_create`, `doci_get`, `doci_update`, `doci_delete`,
  `doci_search`, `doci_inbox`, `doci_inbox_list`, `doci_thread`,
  `doci_threads`, `doci_reply`, `doci_versions`, `doci_health`.
- **REST API.** Ten endpoints under `/api/`. The MCP server and the
  `deep` CLI are thin clients over the same surface.
- **CLI (`deep`).** POSIX shell wrapper for inbox-heavy workflows.
- **Git-backed storage.** Every create / update / delete is one commit
  in `files/.git/`. The commit log doubles as audit trail.
- **Optional Mesh integration.** When [`mesh-memory`](https://github.com/dklymentiev/mesh-memory)
  is reachable at `MESH_API_URL`, documents are upserted into it for
  semantic search.
- **Auth at the edge.** Reverse-proxy `Remote-User` header (Traefik /
  Authum / any forward-auth) or per-call `X-API-Key`. DOCI owns no
  user store of its own.
- **Canonical documents.** Many-to-many "first reach for these"
  registry scoped by domain (`marketing`, `support`, `incidents`, …).
  Marked documents render bold + underlined in the sidebar; the
  **Canonical only** toggle at the top of the sidebar collapses the
  tree to just those.

## Architecture

```
doci/
├── index.php              # Web UI + router (5 routes)
├── config.php             # Configuration, auth, DB connection
├── documents.php          # Document repository layer
├── api/                   # REST endpoints — canonical contract
│   ├── documents.php      # Document CRUD
│   ├── inbox.php          # Quick capture / list / promote
│   ├── thread.php         # Create / list threads
│   ├── thread-reply.php   # Append reply
│   ├── versions.php       # Snapshot / list versions
│   ├── register.php       # Index out-of-band files
│   ├── ai-response.php    # Trigger AI reply on pending thread
│   ├── key-document.php   # Curated canonical-docs registry
│   ├── validate-selection.php  # Validate a thread anchor
│   └── health.php         # Liveness probe
├── ai.php                 # AI gateway adapter (OpenAI-compatible)
├── mcp_server.py          # FastMCP server (12 tools over the REST API)
├── deep                   # POSIX shell CLI
├── lib/                   # git, validation, response, mesh, markdown, render, key-documents
├── migrations/            # PostgreSQL schema (idempotent)
├── scripts/               # entrypoint, index-documents, normalize-links
├── files/                 # Document storage (gitignored; user content)
│   ├── inbox/
│   ├── .data/
│   │   ├── threads/<parent>/<thread>/index.md
│   │   └── versions/<orig>/<version>/index.md
│   └── <user folders…>
└── tests/                 # PHPUnit Unit/ + api-test.sh
```

Full design decisions in [`docs/03-architecture.md`](docs/03-architecture.md).
Document model and operations in [`docs/02-spec.md`](docs/02-spec.md).

## API, CLI, and MCP

The full HTTP contract (10 endpoints), the `deep` CLI commands, and
the 12 MCP tools are documented in one place:
**[`docs/05-api-reference.md`](docs/05-api-reference.md)**.

Auth, in brief:

```bash
# Service-to-service (CLI, MCP, agents)
curl -H "X-API-Key: $DOCI_API_KEY" http://localhost:8080/api/documents.php?path=docs/note

# Browser flow — reverse proxy sets Remote-User; CSRF token required
# for POST/PUT/DELETE (token returned in the UI session)
```

MCP example (Claude Desktop, Claude Code, any MCP client):

```bash
export DOCI_API_URL="http://localhost:8080/api"
export DOCI_API_KEY="<secret>"
python mcp_server.py                  # stdio
python mcp_server.py --transport sse  # SSE on port 8200
```

## Routing

Documents are accessible by:
- **GUID** — `/a1b2c3d4-5678-…` — stable across renames, recommended.
- **Path** — `/docs/my-document` — human-readable.

Router logic (in order):
1. Path looks like a GUID → lookup in `documents` table.
2. `files/<path>.md` exists → serve markdown.
3. `files/<path>.html` exists → serve in sandboxed iframe.
4. `files/<path>/` is a directory → render folder cards.
5. Else → 404.

## Operator shortcuts

```bash
make help          # list every target
make up            # start dev stack
make api-test      # run the integration suite
make analyse       # phpstan level 5 on lib/ + api/
make test          # PHPUnit unit suite
make psql          # open psql against the dev DB
make logs          # tail container logs
make deploy        # build + restart prod (with --project-directory)
```

Mass index rebuild (when files were added out-of-band):

```bash
docker compose exec doci php scripts/index-documents.php
```

## Database

See [`docs/data-dictionary.md`](docs/data-dictionary.md) for the schema.
Two tables today: `documents` and `key_documents`. Migrations applied
automatically on container start (idempotent).

```bash
psql -h 127.0.0.1 -p 5433 -U doci_app -d doci
SELECT guid, doc_type, path, title FROM documents WHERE deleted_at IS NULL;
```

## Documentation

The documentation follows a software product template (v1.1): vision,
spec, architecture, roadmap, API reference, testing, operations, changelog
and `09-doc-pipeline.md`, in `docs/`.

### Level 0 — open-source baseline

| Doc | Status |
|---|---|
| [README](README.md) | this file |
| [LICENSE](LICENSE) | MIT |
| [CHANGELOG](CHANGELOG.md) | curated per release |
| [CONTRIBUTING](CONTRIBUTING.md) | how to contribute |
| [SECURITY](SECURITY.md) | vulnerability disclosure |
| [CODE_OF_CONDUCT](CODE_OF_CONDUCT.md) | contributor covenant |
| [CLAUDE.md](CLAUDE.md) | AI-agent working instructions |

### Level 1 — professional product

| Doc | Purpose |
|---|---|
| [`01-vision`](docs/01-vision.md) | Problem, audience, value, non-goals |
| [`02-spec`](docs/02-spec.md) | Document model + every operation |
| [`03-architecture`](docs/03-architecture.md) | Components, layers, design decisions |
| [`04-roadmap`](docs/04-roadmap.md) | Phased delivery with exit gates |
| [`06-testing-strategy`](docs/06-testing-strategy.md) | Test pyramid + mandatory live benchmark gate |
| [`RELEASING`](RELEASING.md) | Release checklist + semver policy |
| [`Makefile`](Makefile) | Operator shortcuts |

### Level 2 — enterprise-grade

| Doc | Purpose |
|---|---|
| [`05-api-reference`](docs/05-api-reference.md) | HTTP, CLI, MCP — the full contract |
| [`07-operations`](docs/07-operations.md) | Deploy, monitor, backup, incidents |
| [`09-doc-pipeline`](docs/09-doc-pipeline.md) | How docs are produced and kept current |
| [`data-dictionary`](docs/data-dictionary.md) | Database schema reference |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues:
[SECURITY.md](SECURITY.md). Release process:
[RELEASING.md](RELEASING.md).

## License

MIT — see [LICENSE](LICENSE).

---

Created by [Dmytro Klymentiev](https://klymentiev.com).
