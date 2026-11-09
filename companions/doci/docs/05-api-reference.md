# DOCI — API Reference

Index of every external surface DOCI exposes: HTTP API, CLI (`deep`),
and MCP tools. The HTTP API is the canonical contract — CLI and MCP
are thin clients over it (see DD-05 in
[`03-architecture.md`](03-architecture.md)).

For the conceptual model behind documents, threads, versions, and
inbox see [`02-spec.md`](02-spec.md). For the storage layout see
[`data-dictionary.md`](data-dictionary.md).

## Auth schemes

Every request must pass one of two checks:

| Scheme | How it travels | Where the secret lives | Use when |
|--------|----------------|------------------------|----------|
| **Reverse proxy** (`Remote-User: <id>`) | Header set by Traefik / Authum / forward-auth middleware | Outside DOCI — in the proxy's user store | Browser sessions for humans |
| **API key** (`X-API-Key: <secret>`) | Header | `DOCI_API_KEY_HASH` env var, SHA-256 of the secret, compared in constant time | Service-to-service: CLI, MCP, cron, agents |

CSRF token (`X-CSRF-Token` header or `csrf_token` body field) is required
for POST/PUT/DELETE when authenticated via `Remote-User`. API-key callers
are exempt — they already prove a secret. (Enforcement is on by default;
set `DOCI_CSRF_ENABLED=false` only for API-key-only deployments.)

## HTTP API — endpoint table

| Route | Method | Auth | Purpose |
|-------|--------|------|---------|
| `/api/documents.php` | GET/POST/PUT/DELETE | required | Document CRUD |
| `/api/list.php` | GET | required | List documents (tree, recent, folder-prefix, substring) |
| `/api/inbox.php` | GET/POST | required | Quick capture, list, promote |
| `/api/thread.php` | GET/POST | required | Create / list discussion threads |
| `/api/thread-reply.php` | POST | required | Append reply to existing thread |
| `/api/versions.php` | GET/POST | required | Snapshot / list versions |
| `/api/register.php` | POST | required | Index an existing on-disk file |
| `/api/ai-response.php` | POST | required | Trigger AI reply for a pending thread |
| `/api/key-document.php` | GET/POST/DELETE | required | Curated canonical-documents registry |
| `/api/validate-selection.php` | POST | required | Verify a text selection can become a thread anchor |
| `/api/health.php` | GET | none | Liveness probe (DB + files writability) |

Total **11 public routes**. UI routes (`/`, `/<guid>`, `/<path>`)
render HTML and are out of scope here — they are exercised by clicking,
not by integrating. `/api/save.php` is the browser-flow save endpoint
the editor uses; `/md.php` and `/video.php` are gone in v0.2.

## Documents

### `POST /api/documents.php`

Create a new document. Writes the file, registers metadata, commits to git.

**Request:**
```json
{
  "path": "docs/my-document",
  "content": "# My Document\n\nContent here...",
  "title": "My Document",
  "summary": "Optional summary",
  "tags": ["tag1", "tag2"]
}
```

`.md` is auto-appended if missing. `title`, `summary`, `tags` are optional.

**Response (201):**
```json
{
  "success": true,
  "guid": "a1b2c3d4-...",
  "path": "docs/my-document.md",
  "title": "My Document",
  "url": "/a1b2c3d4-..."
}
```

**Errors:** 400 (path traversal / missing required field), 409 (path
collision), 401 (auth missing).

```bash
curl -X POST http://localhost:8080/api/documents.php \
  -H "X-API-Key: $DOCI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"path":"docs/note","content":"# Note\n"}'
```

### `GET /api/documents.php`

Fetch metadata by GUID or path. Add `&content=1` to include the raw body.

```
GET /api/documents.php?guid=<guid>
GET /api/documents.php?path=<path>
GET /api/documents.php?guid=<guid>&content=1
```

### `PUT /api/documents.php`

Update fields. At least one of `content` / `title` / `summary` / `tags`
required. Content rewrite triggers a `Update <path>` git commit.

```json
{ "guid": "a1b2c3d4-...", "content": "new body", "title": "new title" }
```

### `DELETE /api/documents.php?guid=<g>`

Soft delete. Sets `deleted_at = NOW()`. The file on disk is preserved;
a `Delete <path>` commit is recorded. Restore via direct SQL: `UPDATE
documents SET deleted_at = NULL WHERE guid = '<g>'`.

## List

Read-only tree / listing endpoint. Returns every live document, ordered
by `path` unless `recent` is set (then ordered by `updated_at DESC`).

### `GET /api/list.php`

Query parameters (all optional, combine with AND):

- `q=<text>` — case-insensitive substring match on `path` or `title`.
  Clamped to 200 characters.
- `recent=<n>` — return only the `n` most recently updated documents,
  newest first. `n` is clamped to `[0, 500]`.
- `folder=<prefix>` — return every document whose path begins with
  `<prefix>/`. Recursive across sub-folders. The value is sanitised
  through `sanitize_path` (drops `..`, `.dotdirs` and anything outside
  `[A-Za-z0-9_.-]`); a value that sanitises to empty returns 400.

**Response (200):**
```json
{
  "success": true,
  "documents": [
    {
      "guid": "a1b2...",
      "path": "example-project/overview.md",
      "title": "Example project overview",
      "doc_type": "document",
      "summary": null,
      "created_by": "agent-b",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "count": 1
}
```

Every row carries at least `guid` and `path`; the remaining columns are
what a tree view needs in order to render without a second call per row.

## Inbox

Quick free-form capture, later promotable to a structured document.

### `POST /api/inbox.php?action=add`

Form-encoded:
```
content=Quick thought&tags=idea,api
```

Stores under `files/inbox/YYYYMMDD-HHMMSS.md` with `doc_type='inbox'`.
The title is the first non-empty line, truncated to 80 chars.

### `GET /api/inbox.php?action=list&limit=20`

List recent inbox items, newest first.

### `GET /api/inbox.php?action=get&guid=<g>`

Fetch one item's full body.

### `POST /api/inbox.php?action=promote`

```
guid=<g>&target=docs/promoted-note.md
```

Moves the file from `inbox/` to `<target>`, sets `doc_type='document'`,
preserves the GUID.

## Threads

A thread is itself a document (`doc_type='thread'`) whose `parent_guid`
points to the document under discussion and `quote` holds the exact
selected passage. **Starting a thread auto-snapshots the parent into a
version** — the conversation is anchored to a frozen state.

### `POST /api/thread.php`

```json
{
  "documentGuid": "<source-guid>",
  "comment": "Initial message",
  "quote": "selected text (optional)",
  "occurrenceIndex": 0,
  "requestAiResponse": false,
  "aiModel": "sonnet"
}
```

When `documentGuid` points at a `document` or `thread` row, a `version`
is auto-created first and the thread is anchored to **that** version
(this is what makes overlapping selections safe — see
[`03-architecture.md`](03-architecture.md)). When it points at a
`version` directly, the version is reused. `quote` is the plain
selection text (no markdown markers); `occurrenceIndex` (default `0`)
picks which match to wrap if the same text appears multiple times.
Title is derived from the first non-empty line of `comment`, truncated
to 100 chars.

`requestAiResponse=true` queues an asynchronous call to the AI gateway;
a placeholder `<!-- ai-pending:<model> -->` is appended to the thread
file and the actual reply lands via `/api/ai-response.php`. Models:
`sonnet`, `opus`, `haiku`.

**Response:**
```json
{
  "success": true,
  "thread":   { "guid": "...", "path": "...", "title": "...", "url": "/..." },
  "version":  { "guid": "...", "path": "...", "url": "/..." },
  "original": { "guid": "..." },
  "ai":       { "pending": true, "model": "sonnet" }
}
```

`ai` is present only when `requestAiResponse=true`.

### `GET /api/thread.php?document=<guid>`

List all threads attached to a document (and any of its versions).

### `POST /api/thread-reply.php`

```json
{ "threadGuid": "<thread-guid>", "message": "Reply text" }
```

Appends one reply to the thread file. No separate reply rows — the file
is the log.

## Versions

A version is a frozen snapshot of a document (`doc_type='version'`)
with `original_guid` pointing at the live document. Versions are
created automatically by thread creation; the explicit endpoint exists
for manual snapshots.

### `POST /api/versions.php`

```json
{ "document_guid": "<original-guid>" }
```

Writes a copy to `files/.data/versions/<orig-guid>/<ver-guid>/index.md`,
records a version row.

### `GET /api/versions.php?document_guid=<g>`

List versions in reverse-chronological order.

## Register

Index an existing on-disk file that was not created through the API
(e.g. an agent did `git mv` directly into `files/`).

### `POST /api/register.php`

```json
{ "path": "existing/file.md", "title": "Optional title override" }
```

Skips silently if a row with that path already exists.

## AI response

### `POST /api/ai-response.php`

```json
{ "threadGuid": "<thread-guid>" }
```

Reads pending AI context from `.ai-pending.json` (created at thread
creation time), calls the configured `AI_GATEWAY_URL` chat-completion
endpoint, appends the response to the thread file. Idempotent if the
pending file is gone.

## Canonical documents

Curated "first reach for these" registry mapped many-to-many onto
domains (a brand-voice guide may be key for both `marketing` and
`support`). See `migrations/002_key_documents.sql`.

| Route | Body / Params |
|-------|---------------|
| `GET /api/key-document.php` | All rows |
| `GET /api/key-document.php?domain=<d>` | Rows for one domain |
| `GET /api/key-document.php?guid=<g>` | Rows for one document |
| `POST /api/key-document.php` | `{ guid, domain, label?, description?, update_trigger? }` |
| `DELETE /api/key-document.php?id=<N>` | Remove one row |

## Validate selection

### `POST /api/validate-selection.php`

```json
{
  "documentGuid": "<guid>",
  "selectedText": "...",
  "contextBefore": "...",
  "contextAfter": "..."
}
```

Returns `{ valid, reason, markdownFragment }`. Called by the right-click
"Start Thread" UI before opening the modal — rejects selections that
span block boundaries, overlap existing thread anchors, or contain
characters that would break the quote anchor.

## Health

### `GET /api/health.php`

No auth required.

```json
{
  "status": "ok",
  "timestamp": "2026-05-23T12:00:00+00:00",
  "checks": { "database": "ok", "files": "ok" }
}
```

503 if either check fails. Used by Docker `HEALTHCHECK` and external
load balancers.

## CLI — `deep`

POSIX shell wrapper over `curl` + `jq`. Reads `DOCI_URL` and
`DOCI_API_KEY` from the environment. Authenticates via `X-API-Key`.

```bash
deep inbox "Quick thought"
deep inbox "With tags" -t idea,api

deep list                       # recent inbox items
deep show <guid>                # full body of one item
deep promote <guid> docs/target.md
```

The CLI covers inbox-heavy flows; for document CRUD use the HTTP API
directly or the MCP tools.

## MCP tools — `mcp_server.py`

FastMCP server. Configure once, every tool below appears as a callable
in Claude Desktop / Claude Code / any MCP client.

| Tool | Wraps | Purpose |
|------|-------|---------|
| `doci_create` | `POST /api/documents.php` | Create a document |
| `doci_get` | `GET /api/documents.php` | Fetch by guid or path; `content=true` for body |
| `doci_update` | `PUT /api/documents.php` | Update fields |
| `doci_delete` | `DELETE /api/documents.php` | Soft delete |
| `doci_search` | `GET /api/documents.php?path=…` | Path lookup (semantic search via mesh-memory if wired) |
| `doci_list_folder` | `GET /api/list.php?folder=…` | List every document under a folder prefix (recursive) |
| `doci_thread` | `POST /api/thread.php` | Start thread, optional AI reply |
| `doci_threads` | `GET /api/thread.php` | List threads for a document |
| `doci_reply` | `POST /api/thread-reply.php` | Append reply |
| `doci_inbox` | `POST /api/inbox.php?action=add` | Capture an inbox note |
| `doci_inbox_list` | `GET /api/inbox.php?action=list` | List inbox items |
| `doci_versions` | `GET /api/versions.php` | List versions |
| `doci_health` | `GET /api/health.php` | Health probe |

Total **13 MCP tools**.

**Configuration:**

```bash
export DOCI_API_URL="http://localhost:8080/api"
export DOCI_API_KEY="<secret>"        # plain secret, NOT the hash
python mcp_server.py                  # stdio
python mcp_server.py --transport sse  # SSE on port 8200
```

If `DOCI_API_URL` is unset, the server attempts `docker inspect doci`
to auto-detect the container IP — convenient for local dev, irrelevant
in production where the env var should always be set explicitly.

## Wire-format guarantees

- Every endpoint returns `application/json`.
- Mutating endpoints return `{ success: true, ... }` on success and
  `{ success: false, error: "<message>" }` on failure with an HTTP
  status code that matches.
- GUIDs are RFC-4122 v4 UUIDs.
- Timestamps are ISO 8601 in UTC.
- Tag arrays serialize as JSON arrays on the wire even though Postgres
  stores them as `TEXT[]`.

Breaking-change policy: any change to a request shape, a required
field, or a status code's meaning is a major version bump. Additive
changes (new optional fields, new endpoints) ship in minor versions.

---

> Next: [06-testing-strategy.md](06-testing-strategy.md) — how this contract is verified.
