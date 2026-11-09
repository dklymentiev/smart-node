# DOCI — Architecture

## Layers

```
                    +-----------------------------------------+
HTTP clients ----->  reverse proxy (auth, TLS)                |
                    +-----+-----------------+-----------------+
                          |                 |
                          v                 v
                +----------------+   +----------------+
                |  Web UI        |   |  REST API      |
                |  index.php     |   |  api/*.php     |
                +-------+--------+   +-------+--------+
                        |                    |
                        +---------+----------+
                                  |
                                  v
                +----------------------------------+
                |  Repository layer                |
                |  documents.php, lib/git.php,     |
                |  lib/validation.php, lib/mesh.php|
                +-------+----------------+---------+
                        |                |
                        v                v
                 +-------------+   +-------------+
                 | PostgreSQL  |   | files/      |
                 | metadata    |   | git working |
                 +-------------+   |   tree      |
                                   +-------------+

         MCP clients (Claude, etc.)  ---HTTP--->  REST API
         CLI (deep)                  ---HTTP--->  REST API
```

## Components

### `index.php` — Web UI + router

Single front controller. Routes:

1. `path` parameter looks like a GUID? -> lookup in `documents`, serve
   the file body with markdown -> HTML rendering (or sandboxed iframe
   for full-page HTML).
2. `files/<path>.md` exists? -> serve.
3. `files/<path>.html` exists? -> serve in iframe sandbox.
4. `files/<path>/` is a directory? -> render folder index.
5. Else -> 404.

Renders Markdown with `Parsedown` + `ParsedownExtended`. Full HTML
documents (starting with `<!DOCTYPE html>` or `<html>`) render in a
sandboxed `<iframe>` (sandbox omits `allow-same-origin`, so the iframe
cannot read DOCI's session).

### `api/*.php` — REST endpoints

One file per resource. Each endpoint:

1. Calls `requireAuth()` (in `config.php`) — API key or `Remote-User`.
2. Validates CSRF (`config.php::requireCsrf()`) for mutating methods
   when not API-key-authed.
3. Calls into the repository layer (`documents.php` + `lib/`).
4. Returns JSON via `lib/response.php`.

Endpoints in v0.1:

| File | Methods | Purpose |
|---|---|---|
| `documents.php` | GET/POST/PUT/DELETE | Document CRUD |
| `inbox.php` | GET/POST | Inbox add/list/get/promote |
| `thread.php` | GET/POST | Thread create + list |
| `thread-reply.php` | POST | Append reply to thread |
| `versions.php` | GET/POST | Snapshot + list |
| `register.php` | POST | Index existing file |
| `ai-response.php` | POST | Trigger AI reply for a pending thread |
| `key-document.php` | GET/POST/DELETE | Curated canonical-documents registry |
| `validate-selection.php` | POST | Verify a text selection can anchor a thread |
| `health.php` | GET | Liveness probe |

### Repository layer

`documents.php` — pure data functions: `registerDocument`,
`getDocumentByGuid`, `getDocumentByPath`, `getDocumentHierarchy`,
`updateDocumentMetadata`, `softDeleteDocument`. Each accepts an open
PDO; none open their own transactions (the caller decides).

`lib/git.php` — `gitCommitFile($path, $message, $author)`. Shells out
to `git` from the working tree. Authenticates the git author from
`Remote-User` when present, falls back to `doci@<domain>`.

`lib/validation.php` — path canonicalization, traversal guard, GUID
format check.

`lib/response.php` — JSON helpers, error envelopes, HTTP status mapping.

`lib/mesh.php` — optional Mesh integration. `meshUpsertDocument()` and
`meshSearch()`. Both no-op when `MESH_API_URL` is unset or unreachable;
errors are logged, never raised. See
[mesh-memory](https://github.com/dklymentiev/mesh-memory).

### `mcp_server.py` — MCP server

FastMCP-based. Tools call the REST API over HTTP using the API key from
`DOCI_API_KEY` env var. Auto-discovers `DOCI_API_URL` by `docker inspect`
on the `doci` container if env var is unset (dev convenience). Tools:

- `doci_create`, `doci_get`, `doci_update`, `doci_delete`
- `doci_inbox`, `doci_inbox_list`
- `doci_thread`, `doci_threads`, `doci_reply`
- `doci_versions`, `doci_search`
- `doci_health`

Runs as a separate process (`python mcp_server.py`). Not packaged in
the Apache image.

### `deep` — CLI

POSIX shell wrapper around `curl` + `jq`. Reads `DOCI_URL` and
`DOCI_API_KEY` from env. Used for inbox-heavy workflows; mostly
demonstrative of "everything is REST".

## Storage

### Git working tree (`files/`)

```
files/
|-- inbox/                       # quick captures
|   `-- 20260124-143022.md
|-- .data/
|   `-- versions/<orig>/<ver>/index.md
|-- <user folders...>            # any structure the user wants
`-- .gitkeep / example/          # placeholders so the dir exists
```

Each document mutation triggers one commit. The repo is local to the
container by default (init'd on first start); operators can configure
an `origin` for backups. The whole UI is git-ignorant beyond commit
messages — branches, tags, and remote refs are operator concerns.

### PostgreSQL

Two tables: `documents` (everything content-bearing) and `key_documents`
(curated canonical-doc registry; many-to-many onto domain strings).
Migrations:

- `migrations/001_initial_schema.sql` — `documents` + indexes.
- `migrations/002_key_documents.sql` — `key_documents` + indexes.
- `migrations/003_hierarchy_full_chain.sql` — rewrite
  `get_document_hierarchy()` to walk both `parent_guid` (thread → version)
  and `original_guid` (version → source), so an AI prompt built from
  ancestry sees the full chain even when a `version` row sits in the
  middle. See [`data-dictionary.md`](data-dictionary.md#functions).

Why a separate metadata layer when files are the source of truth?

- The GUID has to be stable across renames; the filesystem path cannot
  serve that role.
- Soft delete needs a tombstone independent of file existence
  (operators sometimes restore from backup; tombstone trumps file).
- Tag search wants an index that doesn't require walking the tree.
- Threads/versions are themselves documents, but their relationship to
  the parent isn't expressible in path alone.

The `documents` table is rebuildable from disk —
`scripts/index-documents.php` walks `files/` and re-registers
everything by path. Lost rows are not catastrophic; lost
`key_documents` rows are not recoverable from `files/` (they were
authored through the API).

## Design decisions

### DD-01 — File-first, DB-derived

Files are authoritative. Postgres is an index. If they disagree, the
files win and the index gets rebuilt. This keeps "where is my content"
answerable without an app being up.

### DD-02 — One front controller, no MVC framework

`index.php` routes everything. `api/*.php` each handle one resource.
No router lib, no templating engine, no DI container. Reading the
source has low overhead; the cost is duplicated boilerplate per
endpoint.

### DD-03 — Auth at the edge

DOCI does not own users, passwords, or sessions. A reverse proxy
authenticates and passes `Remote-User`; the API-key path is a fallback
for service-to-service. This punts a hard problem to an off-the-shelf
component.

### DD-04 — Optional Mesh, not required

Semantic search is delegated to mesh-memory. DOCI saves still work
without it; the search endpoint just falls back to title/tag matching.
The wire format is a plain HTTP PUT/POST, so any compatible service
works.

### DD-05 — MCP and REST share one implementation

`mcp_server.py` is a thin client over REST, not a parallel
implementation. Adding a feature to REST is enough to expose it via
MCP after one wrapper. The cost is one extra hop in the agent path
(MCP -> HTTP -> PHP).

### DD-06 — No build step

PHP + plain JS + plain CSS. No bundler, no node toolchain. `composer
install` for PHP deps and `pip install -r requirements.txt` for the
MCP server are the entire build.
