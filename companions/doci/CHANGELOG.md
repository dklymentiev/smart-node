# Changelog

All notable changes to DOCI are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `Dockerfile.mcp`, the image definition for the MCP sidecar. `mcp_server.py`
  and `requirements.txt` have always lived here; the Dockerfile that turns them
  into an image did not, so the one thing needed to build the sidecar was kept
  somewhere else. Brought in from the Smart Node vendored copy.

### Fixed
- API: an HTTP status set by a handler survives the outer exception catch.
  Every endpoint wrapped its body in `catch (Exception)` and answered a flat
  400, so a handler that had already called `http_response_code(404)` and then
  thrown reported the wrong status to the caller. The catch now keeps a status
  the handler set when it is itself an error code, and still answers 400 for a
  genuinely unexpected exception. Affects ai-response, documents, register,
  thread, thread-reply and validate-selection.

## [0.3.2] - 2026-08-30

### Added
- `GET /api/list.php` -- list live documents, for a caller building a tree.
  Supports `?q=` (case-insensitive match on path or title) and `?recent=N`
  (the N most recently updated, newest first). Office has called this
  endpoint since its Documents section shipped and DOCI never carried the
  file, so that page rendered "No documents yet" against a full store on
  every node from the day it was added. The failure read as emptiness
  rather than as an error, which is why it survived unnoticed.

## [0.3.1] - 2026-07-18

### Fixed
- Auth: accept email-format principals in the ForwardAuth `Remote-User`
  header. The validation regex was `^[a-zA-Z0-9_-]+$`, which rejected any
  username containing `@` or `.` -- i.e. every email identity from a
  ForwardAuth proxy. Trusted-proxy callers were silently dropped to
  `unknown` and returned `api.auth_failed`, so read endpoints (e.g.
  `list.php`) returned empty for those users. Widened to
  `^[a-zA-Z0-9_@.+-]+$` at both check sites (`src/config.php`: raw header
  and revalidation). Still bounded (no spaces/slashes/shell metacharacters)
  and honoured only from allowlisted proxy IPs.

## [0.2.0-rc] - 2026-05-24

This release closes the eight critical blockers raised by the rein
pre-release audit (see the archived release evidence)
and bundles the canonical-documents feature, full doc canon, and CI
work that had accumulated on `main` since v0.1.0.

### Added
- **Canonical documents** — curated registry, many-to-many onto domain
  strings (`marketing`, `support`, `incidents`, …). New endpoint
  `GET/POST/DELETE /api/key-document.php`, a **Canonical** switch in
  the page header (right of the GUID), and a **Canonical only** toggle
  at the top of the sidebar that collapses the tree to just those
  documents. Canonical files render bold + underlined; folders that
  contain a canonical descendant render bold.
- `migrations/002_key_documents.sql` — `key_documents` table + indexes.
- `migrations/003_hierarchy_full_chain.sql` — rewrite
  `get_document_hierarchy()` to walk both thread→version and
  version→original links so an AI prompt sees the full ancestry.
- Unit tests: `tests/Unit/MarkdownTest.php` (extract_title, HTML
  detection, thread-quote matching, footnote rewriting),
  `tests/Unit/MeshTest.php` (GUID derivation, skip-folder config),
  `tests/Unit/PathSafetyTest.php` (validate_path_within against a real
  temp directory).
- `docs/04-roadmap.md` — phased delivery with exit gates per phase.
- `docs/05-api-reference.md` — full HTTP API + CLI + MCP tool contract.
- `docs/06-testing-strategy.md` — test pyramid + mandatory live benchmark gate
  per Software Product Template v1.1.
- `docs/07-operations.md` — deploy, monitor, backup, incident runbook.
- `docs/08-changelog.md` — stub pointing at this file.
- `docs/09-doc-pipeline.md` — three documentation layers and how they stay current.
- `docs/quickstart.md` and `docs/monitoring.md`.
- `examples/` — runnable curl + Python REST clients, Claude Desktop MCP config.
- `schemas/` — JSON Schemas for `document`, `inbox`, `thread`, `version`.
- `RELEASING.md` — release checklist, semver policy, rollback plan.
- `Makefile` — operator shortcuts (`make help`, `lint`, `test`, `api-test`,
  `up`, `down`, `deploy`, `psql`, etc.).
- `.github/workflows/test.yml` — CI matrix (PHP 8.2/8.3) running
  PHPUnit + PHPStan + `php -l` lint, now with a `postgres:16` service
  container and migrations applied before `composer test` so
  DB-backed tests actually exercise the database.
- `requirements.txt` — declares the two real MCP server runtime deps
  (`mcp>=1.0.0`, `httpx>=0.27.0`). Previously listed `asyncpg` and
  `click`, neither of which is imported anywhere; `mcp_server.py`
  was uninstallable from a clean `pip install -r requirements.txt`.
- **Configuration env vars** introduced by the security hardening
  pass (all documented in `docs/07-operations.md` and `.env.example`):
  `DOCI_ENV`, `DOCI_DEV_AUTO_AUTH`, `DOCI_TRUSTED_PROXIES`,
  `DOCI_LOG_LEVEL`, `DOCI_MCP_HOST`.

### Changed
- **Root document is `files/README.md`, not `files/index.md`.** Router
  in `index.php` falls back to `README.md` when `index.md` is absent
  (search "Root-document fallback"). `$requestPath` stays as `'index'`
  internally, so nav / breadcrumbs / version-bar logic continues to
  treat it as the root. The editorial landing (TOC, three feature
  sections, Get started with MCP config, farm CTA, Read next) lives in
  README.md; the old `files/index.md` was deleted.
- **Sidebar tree hides `index.md` / `index.html`** at every level
  (`render_file_tree` in `lib/render.php`). They are the implicit
  landing of their level — listing them as a separate child duplicated
  the parent-click target.
- **Landing drops the appended folder-cards grid.** The root URL `/`
  now renders the curated landing only; sub-directory index pages
  still get folder/file cards (unchanged).
- **AI gateway** — `ai.php` now talks to any OpenAI-compatible
  `/chat/completions` endpoint via `AI_GATEWAY_URL`, replacing the
  previous internal gateway protocol.
- **Internal links** — saves rewrite `[text](relative/path)` references
  to their `documents.guid` form on write, not only at render time, so
  the markdown source stays stable when files are renamed.
- **Container start** — the entrypoint indexes anything in `files/`
  that has no DB row yet, so seeded markdown gets GUIDs on first boot
  without a manual `scripts/index-documents.php` call.
- `files/` wiped for landing rebuild (Phase 4 of the roadmap). Old demo content
  preserved in git history at commit `9cfa34d`.

### Removed
- `doci.local` as a hardcoded fallback for `DOCI_DOMAIN`. The variable
  is now required at boot; the container fails fast if it is missing.
- Hardcoded LAN bind in `mcp_server.py` (a specific private LAN address).
  SSE / streamable-http transports now require `DOCI_MCP_HOST`
  (use `127.0.0.1` for local-only, `0.0.0.0` for all interfaces); the
  process exits with code 2 if it is missing. Stdio transport is
  unaffected.

### Security
- **Dev-mode auto-auth split** (was: `DOCI_DEBUG=true`). The single
  flag granted full app access as user `dev` to any unauthenticated
  request -- a leaked env var in production was a complete bypass.
  Replaced with three independent vars: `DOCI_ENV`
  (development|production), `DOCI_DEV_AUTO_AUTH` (false by default),
  and `DOCI_DEBUG_LOG` (now `DOCI_LOG_LEVEL`, see below). Auto-auth
  requires BOTH `DOCI_DEV_AUTO_AUTH=true` AND `DOCI_ENV=development`;
  any other combination fails with HTTP 500 before any handler runs.
- **Remote-User trust boundary.** The `Remote-User` header was
  trusted from any source -- anyone able to reach the container
  directly (e.g. on `traefik-net`) could forge an admin identity.
  Now honoured only when `REMOTE_ADDR` matches an entry in the
  comma-separated `DOCI_TRUSTED_PROXIES` IPv4 / CIDR list. Default
  empty = strip every Remote-User header (API-key auth still works).
- **CSRF enforcement is now ON by default.** Earlier in the v0.2
  cycle the hidden `return;` at the top of `require_csrf_token()`
  was lifted into an explicit `DOCI_CSRF_ENABLED` env flag; the
  pre-release audit flagged opt-in as a misconfiguration trap and
  the default flipped to enforce. API-key callers remain exempt
  automatically; set `DOCI_CSRF_ENABLED=false` only for
  API-key-only deployments that never expose the browser flow.
- **Structured production logging with always-on security events.**
  `doci_log()` was binary-gated on the old `DOCI_DEBUG_LOG=true`;
  production therefore logged nothing application-level. Replaced
  with a real log threshold (`DOCI_LOG_LEVEL` = `ERROR` | `WARN` |
  `INFO` | `DEBUG`, default `WARN`) plus an always-on override for
  security actions (`auth.*`, `csrf.*`, `api.auth_*`, `*.delete.*`,
  `*.api_key_*`). The audit trail survives even at the lowest log
  level.
- **API attribution server-side only.** `handleCreate()` and
  `handleUpdate()` previously accepted `created_by` /
  `updated_by` from the request body; an API-key holder could
  attribute work to any user. Both fields are now derived strictly
  from `get_current_username()` and the body value is ignored.
- **Tag validation on update path.** `handleUpdate()` skipped
  `validate_tags()` and imploded raw values into the PostgreSQL
  array literal. Both create and update now call `validate_tags()`
  explicitly.
- **HTTP-level deny for `.git` and `files/.data/`.** `.htaccess`
  had no rules guarding either prefix -- commit packs, FETCH_HEAD,
  and pending AI context blobs were reachable over HTTP. Two
  `RedirectMatch 404` lines at the top of the rewrite block close
  both prefixes before the routing rule sees them.

## [0.1.0] - 2026-05-22

First open-source release. Code extracted from internal use.

### Features

- **Documents.** Hierarchical markdown documents with REST CRUD, GUID-stable
  identifiers, human-readable paths, soft delete, tags, parent/child links.
- **Inbox.** Quick capture of free-form notes; later promote to documents
  at a chosen path.
- **Threads.** Open discussions on a document or a selected quote inside
  it; threads are themselves first-class documents (`doc_type = 'thread'`).
- **Versions.** Snapshot a document into a version document
  (`doc_type = 'version'`) before edits; full chain queryable.
- **Git-backed storage.** Every create/update/delete is committed in the
  `files/` working tree; commit log doubles as audit trail.
- **AI thread replies.** Optional `ai.php` integration generates draft
  replies against an external AI gateway.
- **HTML documents.** Documents whose content is a full HTML page
  (starting with `<!DOCTYPE html>` or `<html>`) render in a sandboxed
  `<iframe>` (sandbox omits `allow-same-origin`).
- **Threads + versions UI.** Version bar at the top of every document
  (chips `Original | DV-1 | …`); right-click on a selection opens
  "Start Thread" which atomically creates a version + a thread
  anchored to the quoted passage; thread pages render the reply log
  with an inline reply textarea and optional AI reply checkbox.
- **MCP server.** `mcp_server.py` (FastMCP) exposes 12 tools for AI
  agents: `doci_create`, `doci_get`, `doci_update`, `doci_delete`,
  `doci_search`, `doci_inbox`, `doci_inbox_list`, `doci_thread`,
  `doci_threads`, `doci_reply`, `doci_versions`, `doci_health`.
- **Auth.** API key (`X-API-Key`, SHA-256 hashed at rest) and/or external
  auth via reverse proxy passing `Remote-User`. CSRF token for mutating
  HTTP methods.
- **Optional Mesh integration.** When `MESH_API_URL` is reachable,
  documents are indexed for semantic search via the companion project
  `mesh-memory` (https://github.com/dklymentiev/mesh-memory).

### Known limitations

- No multi-tenant isolation. Single org / single git repo per instance.
- No built-in user management. Auth is delegated to a reverse proxy or
  to API key holders.
- CSRF enforcement is opt-in (`DOCI_CSRF_ENABLED=true`); default is
  disabled while the gate is being hardened in Phase 5 of the roadmap.
- No built-in search endpoint; the architecture doc previously listed a
  `search.php` route that was never shipped. Path/title/tag lookup works
  through `GET /api/documents.php?path=…`; semantic search requires the
  optional Mesh integration.

[Unreleased]: https://github.com/dklymentiev/doci/compare/v0.2.0-rc...HEAD
[0.2.0-rc]: https://github.com/dklymentiev/doci/compare/v0.1.0...v0.2.0-rc
[0.1.0]: https://github.com/dklymentiev/doci/releases/tag/v0.1.0
