# DOCI — Roadmap

Phases are sized in whole sessions, not dates. A phase is done when its exit criteria pass, not when a time runs out.

## Phase 1 — Internal prototype  [DONE pre-2026-05-22]

**Goal:** working document workspace behind a private VPN, used daily
by the author and a small set of agents.

**Deliverables**
- [x] PHP + Postgres + git working tree
- [x] Document CRUD via REST (`api/documents.php`)
- [x] Inbox quick-capture
- [x] Threads pinned to quoted passages
- [x] Auto-snapshot to version on thread creation
- [x] AI thread replies via external gateway (`ai.php`)
- [x] MCP server (`mcp_server.py`) wrapping the REST API
- [x] Deep CLI (`./deep`) for inbox-heavy workflows

**Exit gate:** in daily personal use for 6+ months, supports both UI and
agent traffic without manual reconciliation between Postgres and `files/`.

## Phase 2 — OSS extraction + v0.1.0  [DONE 2026-05-22]

**Goal:** first public release. Internal code stripped of host-specific
defaults; one-command Docker setup; tour content that explains every
feature; canonical Level-0 doc set.

**Deliverables**
- [x] License (MIT), CONTRIBUTING, SECURITY, CODE_OF_CONDUCT
- [x] CHANGELOG with v0.1.0 entry
- [x] CLAUDE.md (agent instructions)
- [x] `docker-compose.dev.yml` — fully self-contained, embedded Postgres
- [x] `docker-compose.yml` — production with external Postgres + Traefik
- [x] Migrations applied automatically on first start
- [x] `DOCI_ENV=development` + `DOCI_DEV_AUTO_AUTH=true` auto-auth as `dev` for local trial
- [x] `docs/01-vision.md`, `docs/02-spec.md`, `docs/03-architecture.md`,
  `docs/data-dictionary.md` (Level 1 partial)
- [x] Tour content in `files/tour/` (10 stops)
- [x] Demo farm content in `files/demo-farm/` (15 files across 7 folders)
- [x] PHPUnit tests + `tests/api-test.sh`
- [x] Tagged `v0.1.0`

**Exit gate:** `git clone` + `docker compose up` produces a working UI at
http://localhost:8080 with seeded demo content, no manual steps.

## Phase 3 — Documentation canon completion  [DONE 2026-05-24]

**Goal:** close Level 1 + Level 2 gaps against the Software Product
Template v1.1. Match the canonical numbering of the template.

**Deliverables**
- [x] `docs/04-roadmap.md` (this file)
- [x] `docs/05-api-reference.md` (extracted from README, expanded)
- [x] `docs/06-testing-strategy.md` (with mandatory live benchmark gate)
- [x] `docs/07-operations.md` (deploy, monitor, backup, incident response)
- [x] `docs/09-doc-pipeline.md` (how docs are produced and kept current)
- [x] `RELEASING.md` at repo root
- [x] `Makefile` wrapping `composer test`, `phpstan`, `docker compose`

**Exit gate:** doc-pipeline lint passes (no canonical doc missing or
out of date). Project reaches Level 1 = 100% per template README.

## Phase 4 — Onboarding landing  [IN FLIGHT 2026-05-24]

**Goal:** first-time visitor lands on the root document and grasps every
feature (threads, canonical documents, versions, mixed markdown / HTML,
edit + git history) within one or two scroll screens. Replaces the
v0.1.0 30-file `tour/` + `demo-farm/` pile.

**Deliverables**
- [x] Old `tour/` and `demo-farm/` seed content removed.
- [x] Curated editorial landing at `files/README.md` (problem-first
  hero → three feature sections → Get started → For agents → Read
  next). Router in `index.php` falls back to README when `index.md`
  is absent so the file doubles as `/` and `/README.html`.
- [x] Landing exercises every feature surface: an inline thread chip
  (`51a4a00a-...`) on a quoted passage, the auto-snapshotted parent
  version it anchors to, a markdown table in the "For agents" section,
  and a CTA out to `/farm` for the live HTML-and-markdown mix.
- [x] Sidebar dedup: `index.md` / `index.html` hidden from the tree at
  every level; folder-cards grid no longer appended to root.
- [ ] Farm demo refreshed (`farm/` still has the dev-iteration
  pricing/charts that were not yet committed before Phase 4 began).
- [ ] Hallway test — five new users open `/`, scroll once, and can
  name every feature without reading the spec.

**Exit gate:** hallway test passes. Once a user has scrolled the
README and clicked through one thread, one canonical document, and
the farm demo, every claim in `01-vision.md` is observably true.

## Phase 5 — UI completion  [PLANNED]

**Goal:** close known UI debt. The version bar and thread context menu
work, but the CHANGELOG claims they're "planned" — fix the claim, fix
the gaps.

**Deliverables**
- [x] Make `DOCI_CSRF_ENABLED=true` the default in `config.php::require_csrf_token`
- [ ] Threads + versions tabs explicitly listed in CHANGELOG under v0.1
- [ ] Mobile layout pass on document, thread, and version pages
- [ ] Empty-state copy on root when `files/` is empty (post-wipe)

**Exit gate:** lighthouse a11y ≥ 90 on three primary pages; CSRF gate
verified by `tests/api-test.sh` covering the rejection path.

## Phase 6 — Public GitHub release  [IN FLIGHT]

**Goal:** mirror to GitHub, stop pointing the README at a placeholder
URL, accept external contributors.

**Deliverables**
- [ ] GitHub mirror at `github.com/dklymentiev/doci`
- [ ] Issue templates (bug / feature / docs)
- [ ] PR template referencing `CONTRIBUTING.md`
- [x] CI workflow: PHPUnit + PHPStan on push and PR (`.github/workflows/test.yml`, PHP 8.2/8.3 matrix + lint job)
- [ ] Docker image published to GHCR
- [ ] README quickstart verified by a stranger on a clean Ubuntu 24.04 box

**Exit gate:** `git clone` to first running UI in under five minutes,
on a machine that has only Docker installed.

## Phase 7 — Per-document ACL  [DEFERRED]

The `documents.access` column has been reserved since the initial schema
but is not wired. Multi-tenant or per-doc permissions would also require
a `users`/`groups` model, which DOCI today delegates to the reverse proxy.

Reopens only if a concrete request appears — until then, single-tenant
per instance.

## Gates that must pass for every phase touching HTTP, subprocess, or external APIs

Per the Software Product Template v1.1 rule (added post-Flint R0-R10):

**Live Benchmark Gate** — real PHP-FPM / real Postgres / real git in
the container / real reverse-proxy header / real MCP client over HTTP.
Unit tests catch module-level bugs; only live runs catch integration
bugs such as commit attribution under `www-data`, Authum header
canonicalization, or container-IP drift in `mcp_server.py::_detect_doci_url`.
See `06-testing-strategy.md`.

Applied in Phase 2 to the docker-compose dev path. Required for every
future phase that ships new endpoints, new container behavior, or new
MCP tools.

---

> Next: [05-api-reference.md](05-api-reference.md) — the contract.
