# DOCI — Functional Spec

## Document model

A **document** is a row in the `documents` table plus (for content-bearing
types) a file on disk under `files/`.

| Field | Type | Purpose |
|---|---|---|
| `guid` | UUID | Stable identifier; survives renames |
| `path` | text | Filesystem path relative to `files/`, ending in `.md` |
| `parent_guid` | UUID | Parent for threads (links a thread to its document) |
| `original_guid` | UUID | Original document for versions (snapshot ancestry) |
| `doc_type` | text | `document` \| `inbox` \| `thread` \| `version` |
| `title` | text | Display title |
| `summary` | text | Optional one-line summary |
| `tags` | text[] | Postgres array; GIN-indexed |
| `quote` | text | (Threads only) selected passage on the parent |
| `created_by`, `updated_by` | text | Identity strings (e.g. `Remote-User`) |
| `created_at`, `updated_at` | timestamptz | |
| `access` | text | Reserved for per-doc ACL |
| `deleted_at` | timestamptz | Soft-delete tombstone |

## Operations

### Create document

`POST /api/documents.php` with `{path, content, title?, summary?, tags?}`.
Behavior:

1. Path is normalized: must end `.md` (auto-appended).
2. Content is written to `files/<path>`.
3. A row is inserted with a fresh GUID.
4. The file is committed: `Create <path>`.
5. Response: `{guid, path, title, url}`.

Failure modes: path collision (HTTP 409), path traversal (HTTP 400),
auth missing (HTTP 401).

### Read document

`GET /api/documents.php?guid=<g>` or `?path=<p>`. Add `&content=1` to
include the raw markdown body. Returns full metadata.

### Update document

`PUT /api/documents.php` with `{guid, content?, title?, summary?, tags?}`.
At least one mutating field required. Content rewrite triggers a git
commit `Update <path>`.

### Delete (soft)

`DELETE /api/documents.php?guid=<g>`. Sets `deleted_at = NOW()`. The
file on disk is left in place; a `Delete <path>` commit is recorded.
Restoring is a metadata operation (no API in v0.1 — direct SQL).

### Inbox

`POST /api/inbox.php?action=add` with `content=<text>&tags=<csv>`. Stores
under `files/inbox/YYYYMMDD-HHMMSS.md` with `doc_type='inbox'`. The
title is the first non-empty line, truncated to 80 chars.

`POST /api/inbox.php?action=promote` with `guid=<g>&target=<path>`. Moves
the file from `inbox/` to `<target>`, sets `doc_type='document'`,
preserves GUID.

### Threads

`POST /api/thread.php` with `{documentGuid, comment, quote?, occurrenceIndex?, requestAiResponse?, aiModel?}`.
The thread is itself a document with `doc_type='thread'`. **Starting a
thread on a `document` or `thread` row auto-snapshots the source into a
`version`** — the thread's `parent_guid` then points at that version,
not the live document. (Starting a thread on an existing `version` reuses
it.) `quote` is the plain selected text from the rendered surface;
`occurrenceIndex` disambiguates which match to wrap when the same string
appears multiple times. Title is derived from the first line of
`comment` (truncated to 100 chars).

`POST /api/thread-reply.php` with `{threadGuid, message}` appends a reply
to an existing thread's file. (No separate reply rows; the file is the
log.) Always queues a sonnet AI reply asynchronously.

### Versions

`POST /api/versions.php` with `{document_guid}`. Snapshots the current
state of the document into a sibling `doc_type='version'` row whose file
lives under `files/.data/versions/<original-guid>/<version-guid>/index.md`.

`GET /api/versions.php?document_guid=<g>` lists versions in reverse
chronological order.

### Register (existing files)

`POST /api/register.php` with `{path, title?}`. Useful when a file was
created out-of-band (e.g. by an agent doing `git mv` directly). Inserts
a metadata row for an existing file. Skips if a row with that path
already exists.

## Interfaces

- **Web UI** (`index.php`) — read/edit, threads, inbox, search.
- **REST** (`api/*.php`) — canonical contract; everything else calls it
  or duplicates it.
- **CLI** (`deep`) — thin shell wrapper for inbox-heavy use:
  `deep inbox`, `deep list`, `deep show`, `deep promote`.
- **MCP** (`mcp_server.py`) — FastMCP server exposing `doci_create`,
  `doci_get`, `doci_update`, `doci_delete`, `doci_inbox`,
  `doci_inbox_list`, `doci_thread`, `doci_threads`, `doci_reply`,
  `doci_versions`, `doci_search`. Talks to REST over HTTP.

All four cover the same surface; an operation missing from any of them
is a defect to file.

## Auth

Two modes, used together or alone:

1. **Reverse-proxy header.** A reverse proxy authenticates the request
   and sets `Remote-User: <id>`. Mutations are attributed to that user.
2. **API key.** Caller sends `X-API-Key: <secret>`; the server compares
   `sha256(secret)` against `DOCI_API_KEY_HASH` (constant-time).

CSRF token (header `X-CSRF-Token` or body `csrf_token`) required for
POST/PUT/DELETE when authenticated by `Remote-User`. API key callers
are exempt (they already prove a secret).

## Search

- **Title / path / tag.** Built-in. Backed by Postgres GIN on `tags`
  and trigram-like prefix on `title`/`path`.
- **Semantic.** Only when [mesh-memory](https://github.com/dklymentiev/mesh-memory)
  is reachable at `MESH_API_URL`. DOCI upserts each save into Mesh
  tagged `source:doci`, then `doci_search` proxies a query to Mesh's
  `/search`.

## What is intentionally not in the spec

- User management (delegated to the proxy).
- Permissions per document (`access` is reserved; not wired).
- Workflows / approvals / assignees (see sibling
  planner).
- Real-time presence / multiplayer cursors.
