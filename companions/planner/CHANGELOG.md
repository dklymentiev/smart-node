# Changelog

## Unreleased

- tasks: recurring tasks (migration 0011). `recur_interval` (interval text
  such as `1 day`, `2 weeks`, `1 month`, or daily / weekly / monthly) and
  `next_run` on create, update and every read, in REST, MCP and the CLI;
  `off` ends a series. `Pipeline.recurrence_sweep` (CLI `planner maintenance
  recur`, `POST /api/v1/maintenance/recurrence-sweep`) reopens a cycle closed
  as completed to `in_progress` with `approved_at` kept, comments on a cycle
  that came due while the previous one is open, ends a series closed as
  cancelled or rejected, skips one waiting for approval, wakes the assignee
  and moves `next_run` forward by whole intervals. See
  docs/05-recurring-tasks.md.

  Why: repeating work (a daily check, a weekly report) had no place in the
  planner; a node kept it alive with a cron of its own waking an agent, and
  the task, if any, was a standing log nobody could close.

- objectives: the progress answer also counts the subtree in the three
  statuses: `tasks_by_state`, `queued_by_reason` (`none` for nobody on it yet)
  and `closed_by_outcome`. `tasks_by_status` keeps the stage names.
- The reviewer guard no longer tells the caller to "use done -> pending_review":
  it says to hand the task to review (status queued, wait_reason review).
- tasks: three statuses and two adjustable dictionaries (migration 0010).
  `tasks.status` is `queued | in_progress | closed`; `wait_reason` (a row of
  `wait_reasons`) says why a queued task is not being worked on; `close_outcome`
  (a row of `close_outcomes`) says how a closed task ended. The eleven old
  statuses are a projection, kept in `tasks.stage` by trigger through the
  dictionaries, and still spoken on the wire: a writer may send
  `status: done` or `status: closed, close_outcome: completed` and reads back
  both (`status`, `stage`, `state`, `wait_reason`, `close_outcome`).
  A request picks its vocabulary (`X-Status-Vocabulary: new|legacy` or
  `?vocabulary=`); `PLANNER_STATUS_COMPAT` (default `legacy`) is the node
  default for readers that do not ask, so a consumer moves when it is ready
  and nothing is flipped under the others. `assessing` and `incident` are
  accepted and mean `in_progress`.

  Why: whether a task moves, what it waits for and how it ended were one
  column, so every new need was a new status and a new status was a release of
  the planner, the office and every flow. A node in the field needed a
  "cancelled" that is not "rejected" and could not have it without a release;
  its flows bridged the refused edge with a hop through `escalated`. Reasons
  and outcomes are rows now: a node adds one with an INSERT, from the panel.

- tasks: the transition matrix is gone, in Python and in the database
  (`status_transitions` and its trigger are dropped). What remains are the
  guards that can say why they refuse: a close carries a report, only the
  reviewer closes a reviewed task, a decision answers only a task waiting for
  one, a claim answers only an open task, an incident is reported on delivered
  work. From `in_progress` a task may close with any outcome in one move.

- tasks: the comment rule follows the dictionaries. A close needs a comment
  unless the outcome's row says otherwise; a stop in the queue needs one when
  its reason's row asks (`effects.comment_required`). An outcome a person must
  stand behind (`not_possible`, `cancelled`, `rejected`) is refused to an actor
  of type agent (`effects.agent_may_set`); the agent stops the task instead
  (`queued`, `wait_reason: person`) and says why.

- api: `GET/POST/PATCH/DELETE /api/v1/dictionaries/{wait-reasons|close-outcomes}`.
  Reads for every caller, writes for the owner. A row in use cannot be deleted,
  only disabled. `GET /api/v1/tasks` takes `status_filter` in either
  vocabulary plus `wait_reason` and `close_outcome`.

- ui: the transition form carries a comment, so a close or a stop can be made
  from the detail page (it could not, since the done-needs-a-report rule).

- tests: the suite closes tasks with a report, as the pipeline has required
  since 0008 (nine tests had been failing on main for that reason).

## 0.4.0

- auth: an API key may now be marked `can_delegate`. In `apikey` mode a request
  bearing such a key may name the caller it is acting for through the proxy-user
  header (`PLANNER_PROXY_USER_HEADER`, default `Remote-User`), and the work is
  authored by that actor. For every other key the header is ignored entirely and
  the key's own actor authors everything. Migration 0005; the column defaults to
  false, so every key that existed before the upgrade is non-delegating.

  Why: a shared front door has to present two things at once, a credential
  proving it may come in and the name of the person or agent it is acting for.
  Neither existing mode could. `apikey` authors every call as the key's owner,
  so everyone behind one door collapses into one name. `proxy` keeps the real
  name but believes the header from anybody who can reach the socket. Splitting
  the questions -- the key answers "may this caller in", the header answers "on
  whose behalf", and the second is read only when the first says the holder is
  trusted to be asked -- gives both properties at once.

  A delegating key with no header authors as itself, which is right for the
  door's own housekeeping. A delegating key with an unusable header is refused
  rather than quietly recorded as the door itself: attribution that falls back
  to the credential holder is how an unusable name acquires a real one's
  authority.

- cli: `planner key create|list|revoke` replaces the `key hint` stub. These are
  operator commands and the one part of the CLI that does not go through the
  REST API -- a key is what that API authenticates with, so minting the first
  one over it is a bootstrap with no beginning. They read `DATABASE_URL` and
  `PLANNER_SCHEMA` exactly as the server does.

- tests: `tests/integration/test_key_delegation.py`, 14 cases. Verified to fail
  with the delegation branch removed (6 red, including the paired positive) so a
  branch that refuses everything cannot pass them.

## 0.3.1

- packaging: move `httpx` from the `dev` extra into the runtime dependencies.
  `planner/notify.py` imports it at module level for assignee notifications and
  `app.py` imports the routers that import it, so an image built the documented
  way (`pip install .`, which is what the Dockerfile does) crashed on startup
  with `ModuleNotFoundError: No module named 'httpx'` and the container never
  became healthy. Found when a Smart Node install failed at the tracking
  companion (2026-07-29): that image had been rebuilt from the v0.3.0 tag, while
  the image running on existing nodes was built from some other, unrecorded
  state -- so the tag could not produce a working container.
- version: `VERSION` said 0.2.0 while the tag was v0.3.0 and the shipped image
  was tagged 0.3.0. `VERSION`, `__version__` and the git tag now agree on 0.3.1.

## 0.3.0

- tasks: add nullable `workflow_id` (soft integer reference to a workflow/process id; no FK). Wired through the model, schemas (in/out/patch) and the tasks repo (select/create/update). Migration 0003.


All notable changes to Planner will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-27

### Added

- **Edge-trust auth (DD-008).** New `PLANNER_AUTH_MODE` selects how the
  request actor is resolved:
  - `dev` (default): every request is bound to the seeded `dev` actor;
    `/ui/login` redirects to the dashboard; root API key is no longer
    generated. Use only behind `localhost`.
  - `proxy`: read `Remote-User` from a trusted reverse proxy (override
    the header name via `PLANNER_PROXY_USER_HEADER`). Actor is
    materialised on first sight (`INSERT ... ON CONFLICT DO NOTHING`).
    The proxy must be the only ingress -- the header is trusted blindly.
  - `apikey`: previous v0.1.0 behaviour preserved -- sha256-hashed
    `X-API-Key` for service-to-service callers, root key bootstrapped on
    first start, cookie-session login form for browsers. This is how
    Flint / Rein / external agents continue to authenticate.
  - Audit log keeps writing `actor_id` on every transition regardless of
    mode (DD-005 unchanged).
  - Doci + Mesh + Planner behind one Traefik with forward-auth now share
    a single upstream identity.
  - Full RFC in `docs/03-architecture.md` section DD-008.
- **Projects top-nav + index.** New `/ui/projects` lists every project
  (active + archived) with progress bars. Top-nav now exposes Projects
  alongside Dashboard / Tasks / Objectives.
- **Project overview page.** `/ui/projects/{slug}` is the landing tab for
  a project (description, progress, recent activity); Board and
  Objectives are reachable as sibling tabs from the shared subnav.

### Changed

- `/ui/objectives` is now a flat tree of every objective grouped by
  project, instead of a project picker.
- Dashboard project rows link to the project overview
  (`/ui/projects/{slug}`) rather than directly to the kanban board.

### Tests

- New `tests/test_auth_modes.py` (30 cases) covering `get_auth_mode`
  env parsing and `_normalize_proxy_user` input cleaning.
- New `tests/integration/test_auth_modes.py` (16 cases) exercising
  `dev` / `proxy` / `apikey` end-to-end via `/whoami`, including actor
  materialisation, custom proxy header, and invalid-mode startup blow-up.
- Integration conftest pins `PLANNER_AUTH_MODE=apikey` as the default for
  legacy tests so the v0.1.0 baselines (login redirect, cookie session,
  `X-API-Key` actor) keep their semantics under the new dev default.

## [0.1.0] - 2026-04-28

First public release. Self-hostable task management with a six-stage pipeline,
hierarchical objectives, and four equally-capable interfaces (REST, MCP, CLI,
Web UI) sharing one repo layer over PostgreSQL 16.

### What works

- Full task pipeline from intake to delivery: open -> assessing ->
  awaiting_approval -> in_progress -> pending_review -> done. Plus paused
  states (blocked, escalated), terminal (rejected) and post-delivery
  (incident -> hotfix + post-mortem).
- Atomic state machine: every transition validates against a matrix,
  appends to an immutable audit log, and enqueues an outbound notification
  in one DB transaction.
- Block/unblock with cycle detection, cascading unblock when a blocker
  reaches a terminal status, automatic two-strike escalation on review
  bounce-backs, conflict detection over scope, and a periodic staleness
  sweep (approval aging + orphan recovery).
- Hierarchical objectives (strategy / roadmap / milestone / goal /
  initiative / campaign) with recursive progress aggregation.
- API key authentication with sha256-hashed storage and a root key
  bootstrapped on first start.
- Four interfaces, all parity-tested against the same repo:
  - **REST** (`/api/v1/...`) with OpenAPI docs at `/docs`.
  - **MCP** stdio server (`python -m planner.mcp_server`) with 24 tools.
  - **CLI** (`planner ...`) talking to the REST layer.
  - **Web UI** (`/ui/`) with dashboard (activity chart, objective
    rollups, queues), project list, kanban board, task detail (comments,
    log, tags, links, transitions, inline edit), global tasks grid,
    objective tree, docs reference, and API key management.
- Standalone (bundled Postgres via docker compose) and embedded
  (`PLANNER_SCHEMA` env over an existing DB) deployment paths.
- 215+ tests green: matrix unit tests, integration tests against real
  Postgres, parity tests across the four interfaces, and an opt-in live
  test harness that drives a uvicorn subprocess.

### What is in scope

- Task / project / objective CRUD.
- Pipeline state machine + audit log + notification queue.
- API + MCP + CLI + Web UI as equal first-class clients.
- Dashboard read-models exposed both as HTML (`/ui/`) and JSON
  (`/api/v1/dashboard/*`) so external consumers reuse the same
  aggregations.
- Demo seed script (`scripts/seed_demo.sql`) for local exploration.
- Migrations runner with per-migration transactions.
- Docker Compose deployment with bundled Postgres.

### What is not in scope for v0.1.0

- Real-time UI (board polls every 5s; no WebSocket / SSE yet).
- Drag-and-drop task moves (transition buttons only).
- API key management via REST/MCP (UI-only for now).
- HTTP transport for MCP (stdio works; HTTP is on the v0.2.x roadmap).
- Multi-tenant authorization (all keys see all projects).
- External issue tracker sync (GitHub/GitLab/Jira links are recorded but
  not pulled).

### Added

- Initial repository scaffolding (Software Product Template Level 0).
- Vision document at `docs/01-vision.md`.
- MIT license, Python 3.11+ project layout (FastAPI + MCP + PostgreSQL 16 stack).
- `Dockerfile` and `docker-compose.yml` skeletons (no working code yet).
- Initial schema `migrations/0001_initial.sql` -- 11 tables (`actors`, `projects`, `objectives`, `tasks`, `task_comments`, `task_relations`, `task_commits`, `task_log`, `task_tags`, `task_links`, `task_notifications`), with FK on `actors.id` from `tasks.assignee`, `tasks.requester`, `task_comments.author`, `task_log.actor`, and seeded `system` actor.
- Architecture document at `docs/03-architecture.md` -- design decisions DD-001..DD-007 and full data model.
- **R0 Foundation milestone closed.** First runnable code:
  - `planner.migrations` -- idempotent SQL migration runner with per-migration transactions and `PLANNER_SCHEMA` support (standalone vs embedded).
  - `planner.db` -- asyncpg pool, `lifespan` and `transaction` async context managers, schema-aware connection init.
  - `planner.install` -- `python -m planner.install` entrypoint applies pending migrations from `migrations/`; used by both deployment paths.
  - `planner.repos.*` -- actors, projects, objectives (with recursive CTE `tree(project_id)` query), tasks (CRUD only; pipeline state machine is R2), and seven ancillary repos (comments, relations, commits, log, tags, links, notifications).
  - 77 tests (66 unit + 11 integration against PostgreSQL 16). Integration tests gated on `PLANNER_TEST_DSN`.
- **R1 CRUD via four interfaces milestone closed.** Four working clients on the same repo layer:
  - FastAPI app (`planner.app:app`) with lifespan-managed pool, RFC 7807 error bodies, OpenAPI at `/docs`, `/health`, `/whoami`.
  - API key auth (`planner.auth`): migration `0002_api_keys.sql`, sha256-hashed keys, soft-revocation, root key auto-generated and printed once on first start, deactivated actors invalidate their keys.
  - REST endpoints (`/api/v1/...`): full CRUD for actors, projects, objectives (with `/tree` and `/{id}/progress`), tasks (CRUD only — pipeline transitions are R2), and ancillary (comments, relations, commits, log, tags, links). Comment author is server-derived from the API key actor.
  - MCP server (`python -m planner.mcp_server`, stdio transport): 24 tools mirroring REST CRUD; argument schemas exposed via `list_tools`.
  - CLI (`planner ...`, Click): 9 command groups; calls REST so behaviour stays identical.
  - Interface-parity acceptance: `tests/integration/test_parity.py` creates objects via each transport and reads them back via every other; all three views agree.
  - 104 tests green (66 unit + 38 integration).
- **R2 Pipeline state machine milestone closed.** `planner.pipeline.Pipeline` adds atomic state-machine semantics on top of the tasks repo:
  - `TRANSITIONS` matrix mirrored from `task.sh` (10 statuses, 32 allowed edges, no self-loops).
  - `Pipeline.status_change()` is fully atomic in one DB transaction: `SELECT ... FOR UPDATE`, validate, `UPDATE tasks` (status + whitelisted extra fields), append `task_log`, enqueue `task_notifications`, optional `task_comments`. `force=True` bypass for maintenance jobs; idempotent no-op when already in target status.
  - Stage actions (`claim`, `submit_assessment`, `approve`, `reject`, `review`, `verify`) wrap `status_change` with the right side effects (assignee, stage timestamps, complexity hint, scope, eta).
  - Bounce-back (`bounce_back`) increments `verification_attempts`; second bounce auto-escalates under the `system` actor.
  - Block / unblock (`block`, `unblock`) snapshot/restore `previous_status`, validate the reason taxonomy (`task_dependency` requires `by_task_id`; `external_approval` / `scheduled_time` require `external_detail`), and refuse cycles via a 1000-step `blocked_by` chain walk.
  - Cascading unblock: when a task enters `done` / `escalated` / `rejected`, every direct blockee is auto-unblocked in the same transaction.
  - Incident workflow (`report_incident`): `done -> incident` plus auto-created hotfix (same project, same assignee, high priority) + post-mortem (unassigned, medium) with bidirectional `task_relations` links.
  - Agent pickup (`pickup_next`): three-tier waterfall (resume own active work -> assigned-open -> unassigned-open) with priority desc, eta asc, age asc tiebreakers; optional project filter.
  - `check_conflicts` and `staleness_sweep` (approval reminder/escalate, orphaned-block recovery, verification-loop fallback).
  - 208 tests green (66 unit + 142 integration). New: `tests/test_pipeline_matrix.py` and `tests/integration/test_pipeline_*.py` (status_change, stages, bounce, block, cascade, incident, pickup, staleness, acceptance).
- **R3 Web UI milestone closed.** Server-rendered Jinja2 templates with HTMX
  + Tailwind (via CDN, no build step). Cookie-based session reusing the same
  plaintext API key as REST.
  - `/ui/login` form auth, `/ui/` dashboard with progress bars per project,
    `/ui/projects/{slug}/board` kanban (six columns, polls every 5s while
    the tab is visible), `/ui/projects/{slug}/objectives` indented tree
    with cumulative progress, `/ui/tasks/{id}` detail (comments / activity
    log / tags / links / relations / transition buttons gated by the
    matrix), `/ui/keys` API key management.
  - HTMX inline updates on the task detail (add comment, add tag, add
    link, transition) re-render the detail body in one round-trip and
    surface PipelineErrors as inline banners.
- **R4 Release gates milestone closed.** Live-server smoke test harness
  (opt-in via `PLANNER_LIVE=1`) that spawns uvicorn in a subprocess and
  drives REST + CLI + MCP clients end-to-end. Docker Compose deployment
  validated: `docker compose up -d` + first task in under five minutes.
- **R5 Dashboard, API-first read-models, demo seed, task UI.**
  - Dashboard at `/ui/` with adaptive 4-2-1 grid: activity line chart
    (opened vs closed, 30/90d toggle, Catmull-Rom smoothing), strategy
    and goal donut rollups, projects list with inline progress bars,
    "needs attention" / "assigned to me" / "needs review" /
    "agents working" cards, recent tasks. HTMX partials and inline SVG,
    no JS chart libraries.
  - `planner.repos.dashboard.DashboardRepo` centralises every aggregation
    query (flow series, project and objective rollups, task queues).
    Same read-models exposed as JSON at `/api/v1/dashboard/*` so
    external consumers (e.g. higher-level agents) don't re-implement
    aggregations.
  - `planner.ui.charts` — chart geometry helpers (Catmull-Rom smoothing,
    ring math).
  - `scripts/seed_demo.sql` — demo hierarchy
    (strategy -> roadmap -> milestone -> goal -> initiative -> campaign),
    4 projects, 25 objectives, 20 tasks with comments, relations, and
    log entries.
  - Global tasks grid at `/ui/tasks` (24-column table with filters),
    inline edit on the task detail view, `task_new` create form, docs
    reference at `/ui/docs`. Top-nav label changed from "Projects" to
    "Dashboard".
