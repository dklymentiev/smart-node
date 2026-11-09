# Planner -- Functional Specification

**Status:** Draft, v0.1
**Document:** 02-spec (per Software Product Template v1.1)
**Date:** 2026-04-27

> Why this section matters: a functional spec turns the vision into concrete
> use cases (UC-xxx) and features (F-xxx). Every later document
> (architecture details, roadmap, testing, API reference) refers back to
> these IDs. If a feature is not listed here, it is out of scope until added.

---

## Sprint summary (2026-04-27)

This document is also the close of the design sprint that bootstrapped Planner OSS. Done in this sprint:

- Doci folder `planner/` created, vision (`01-vision.md`) drafted.
- Repository skeleton -- Software Product Template Level 0 baseline (README, LICENSE MIT, CHANGELOG, CONTRIBUTING, SECURITY, CLAUDE.md, Dockerfile, docker-compose.yml, pyproject.toml, .gitignore, src/planner/, tests/, docs/).
- Architecture (`03-architecture.md`) with design decisions DD-001..DD-007.
- Schema (`migrations/0001_initial.sql`): 11 tables, 85 columns, FK on `actors.id` everywhere an author appears.
- Data dictionary (`data-dictionary.md`): per-field Purpose for every column.
- This functional spec.

Not done yet (intentional, scoped to next sprints):
- 04-roadmap (phased delivery R0..RN with exit criteria + Phase 4.5 / 4.6 gates).
- 05-api-reference (REST + MCP + CLI contracts).
- 06-testing-strategy (test pyramid, gates, live benchmark scenarios).
- 07-operations (install, upgrade, backup, monitoring).
- Any code beyond `src/planner/__init__.py`.

This spec is **forward-derived from vision + schema**, not from an implementation. It will be cross-checked in Phase 4 against a `tech_audit` of the existing internal `/srv/tools/planner` codebase before tagging v0.1.

---

## Scope and boundaries

Planner is a self-hostable task manager. Its job is to maintain task state and audit, and to expose four interfaces (CLI / REST / MCP / UI) that read and write it. Anything beyond state and audit is out of scope -- handled by external consumers reading `task_notifications` or `task_log`.

**Inside the boundary:**
- All operations on the 11 tables defined in `data-dictionary.md`.
- Pipeline lifecycle enforcement (status transitions, verification loop limits, blocked-status preservation).
- Outbound event emission to `task_notifications` on state transitions.
- Audit log append on every state change.

**Outside the boundary (consumers, not Planner):**
- Email / Slack / webhook delivery.
- Workflow / agent orchestration.
- Document storage, semantic memory.
- LLM calls of any kind.
- Source control hosting.

---

## Use Cases

Numbered scenarios. Each one drives a set of features. Scenarios are written as the actor does them -- "creates a task" not "system exposes endpoint to create".

| ID | Actor | Scenario |
|---|---|---|
| UC-001 | Solo developer | Captures a quick task from CLI (`planner add "fix logger"`). System creates `tasks` row with status=`open`, default project, assignee=self. |
| UC-002 | Small team lead | Browses unclaimed tasks in a project, claims one (`open -> assessing`, sets self as assignee), records assessment, requests approval, executes, requests review, marks done. Full pipeline. |
| UC-003 | AI agent (via MCP) | Calls `planner.next` for unassigned high-priority tasks, claims one, runs, records progress as comments + status transitions. |
| UC-004 | Operator | Opens dashboard, sees pipeline view per project: how many `open`, `awaiting_approval`, `in_progress`, `pending_review`, plus blockers and overdue items. |
| UC-005 | External email service | Polls `task_notifications WHERE fired_at IS NULL`, picks events with `notify_target='email:requester'`, sends emails, sets `fired_at`. |
| UC-006 | Operator | Runs `docker compose up -d` on a fresh laptop. Within five minutes UI is reachable at `localhost:8080`, default project exists, first task can be created. |
| UC-007 | Embedded host | Configures `DATABASE_URL` to point at host Postgres (own schema). Disables bundled UI. Reads / writes through Planner REST + MCP from its own UI shell. |
| UC-008 | Auditor | Pulls full action history for one task (`task_log` filtered by `task_id`) plus all comments and commits, in chronological order. |
| UC-009 | Engineer | Marks done task as `incident`. System auto-creates two new tasks: a `hotfix` (assignee = same engineer, high priority) and a `postmortem` (unassigned). All linked via `task_relations`. |
| UC-010 | Operator | Registers a new agent in `actors`. Without registration, any task assigned to that agent ID would be rejected at FK -- this is the safety net. |
| UC-011 | Operator | Backs up the database (`pg_dump`), restores on a new host, runs `docker compose up`. Same install path as fresh, except migrations are skipped because already present. |
| UC-012 | Operator | Upgrades to a newer Planner version. Container starts, applies any new `000N_*.sql` migrations on top, version recorded in `_migrations` table. |
| UC-013 | Lead | Creates a hierarchical objective tree for a quarter (`strategy -> roadmap -> milestone -> tasks`), tracks recursive progress. |
| UC-014 | Engineer | Tags a task with `epic:checkout` and `sprint-12`. Filters dashboards by tag combinations. |
| UC-015 | Operator | Runs the staleness checker (cron every 30 min). Auto-escalates approval-aging tasks past 48h, auto-unblocks tasks whose blocker is done. |

---

## Feature inventory

Features are grouped by domain. Each F-xxx has a one-line description and a link to the table(s) it operates on. Every feature is exposed in **all four interfaces** (CLI / REST / MCP / UI) -- no UI-only or CLI-only features. Interface parity is the hard rule from `01-vision.md`.

### Actors (F-ACT)

| ID | Feature | Table |
|---|---|---|
| F-ACT-001 | Register a new actor | `actors` |
| F-ACT-002 | List actors (filter by type, by active flag) | `actors` |
| F-ACT-003 | Get one actor | `actors` |
| F-ACT-004 | Update actor (`display_name`, `type`) | `actors` |
| F-ACT-005 | Soft-deactivate / reactivate | `actors` |

### Projects (F-PRJ)

| ID | Feature | Table |
|---|---|---|
| F-PRJ-001 | Create project (`slug`, `title`, `description`, `sort_order`) | `projects` |
| F-PRJ-002 | List projects (filter by status) | `projects` |
| F-PRJ-003 | Get project by `slug` or `id` | `projects` |
| F-PRJ-004 | Update project | `projects` |
| F-PRJ-005 | Archive project (soft) | `projects` |

### Objectives (F-OBJ)

| ID | Feature | Table |
|---|---|---|
| F-OBJ-001 | Create objective (parent, project, type, title) | `objectives` |
| F-OBJ-002 | List objectives (by project, parent, type, status) | `objectives` |
| F-OBJ-003 | Get one objective | `objectives` |
| F-OBJ-004 | Update objective (title, status, type) | `objectives` |
| F-OBJ-005 | Re-parent objective (move under different parent or to root) | `objectives` |
| F-OBJ-006 | Tree view (recursive) for a project | `objectives` |
| F-OBJ-007 | Recursive progress (count tasks under this objective and descendants) | `objectives`, `tasks` |
| F-OBJ-008 | Link existing task to an objective | `tasks.objective_id` |
| F-OBJ-009 | Delete objective (only if no children and no tasks) | `objectives` |

### Tasks -- core (F-TSK)

| ID | Feature | Table |
|---|---|---|
| F-TSK-001 | Create task (title, description, project, objective, assignee, requester, priority, due, eta, complexity, scope) | `tasks` |
| F-TSK-002 | List tasks (filter by project, objective, assignee, requester, status, priority, due-before, tag) | `tasks` (+ `task_tags`) |
| F-TSK-003 | Get task (full detail: task + comments + commits + relations + tags + links + recent log) | many |
| F-TSK-004 | Update task -- any mutable field (assignee, priority, eta, scope, complexity, due) | `tasks` |
| F-TSK-005 | Search tasks (text in title / description) | `tasks` |
| F-TSK-006 | Soft-delete (mark as `rejected` rather than DROP) | `tasks` |

### Tasks -- pipeline (F-TSK-P)

| ID | Feature | Table |
|---|---|---|
| F-TSK-P-001 | Status transition (enforced by allowed-transitions matrix in Core) | `tasks`, `task_log`, `task_notifications` |
| F-TSK-P-002 | Claim task (`open -> assessing`, sets `assignee = caller`) | `tasks` |
| F-TSK-P-003 | Submit assessment (`assessing -> awaiting_approval`, sets `assessed_at`, requires `scope`/`eta`/`complexity_hint`) | `tasks` |
| F-TSK-P-004 | Approve (`awaiting_approval -> in_progress`, sets `approved_at` and `execution_started_at`) | `tasks` |
| F-TSK-P-005 | Reject (`* -> rejected`) | `tasks` |
| F-TSK-P-006 | Submit for review (`in_progress -> pending_review`) | `tasks` |
| F-TSK-P-007 | Verify (`pending_review -> done`, sets `completed_at`) | `tasks` |
| F-TSK-P-008 | Bounce back (`pending_review -> in_progress`, increments `verification_attempts`; auto-escalate at 2) | `tasks` |
| F-TSK-P-009 | Block (`* -> blocked`, snapshot `previous_status`, set `blocked_by`/`blocked_reason`/`external_detail`) | `tasks` |
| F-TSK-P-010 | Unblock (`blocked -> previous_status`, clear blocking fields) | `tasks` |
| F-TSK-P-011 | Cascading unblock when blocker reaches `done`/`rejected`/`escalated` | `tasks` |
| F-TSK-P-012 | Escalate (`* -> escalated`) | `tasks` |
| F-TSK-P-013 | Open incident (`done -> incident`; auto-creates hotfix and postmortem child tasks via `task_relations`) | `tasks`, `task_relations` |
| F-TSK-P-014 | Pickup next task for an agent (resume in-progress > assigned-open > unassigned-open, sorted by priority then ETA then age) | `tasks` |
| F-TSK-P-015 | Check conflicts (find active tasks with overlapping `scope`) | `tasks` |
| F-TSK-P-016 | Staleness sweep (cron): approval-aging reminders/escalations, orphaned-block auto-unblock, verification-loop escalation | `tasks`, `task_log` |

### Comments (F-CMT)

| ID | Feature | Table |
|---|---|---|
| F-CMT-001 | Add comment (author from caller, optional `comment_type`) | `task_comments` |
| F-CMT-002 | List comments for a task (filter by `comment_type`) | `task_comments` |
| F-CMT-003 | Edit own comment (audit recorded) | `task_comments` |
| F-CMT-004 | Delete own comment | `task_comments` |

### Relations (F-REL)

| ID | Feature | Table |
|---|---|---|
| F-REL-001 | Add relation (`task_id`, `related_id`, `relation`) | `task_relations` |
| F-REL-002 | List relations for a task (both directions) | `task_relations` |
| F-REL-003 | Remove relation | `task_relations` |

### Commits (F-CMM)

| ID | Feature | Table |
|---|---|---|
| F-CMM-001 | Link commit to task (`repo`, `commit_hash`, `message`, `author`, `content_body`) | `task_commits` |
| F-CMM-002 | List commits for a task | `task_commits` |
| F-CMM-003 | Remove commit reference | `task_commits` |

### Tags (F-TAG)

| ID | Feature | Table |
|---|---|---|
| F-TAG-001 | Add tag to task | `task_tags` |
| F-TAG-002 | Remove tag from task | `task_tags` |
| F-TAG-003 | List tags for a task | `task_tags` |
| F-TAG-004 | List all tags in use (with counts) | `task_tags` |
| F-TAG-005 | Filter tasks by tag (one or many, AND/OR) | `task_tags` |

### Links (F-LNK)

| ID | Feature | Table |
|---|---|---|
| F-LNK-001 | Add external link to task (`kind`, `url`, optional `label`) | `task_links` |
| F-LNK-002 | List links for a task | `task_links` |
| F-LNK-003 | Update link (label, url) | `task_links` |
| F-LNK-004 | Remove link | `task_links` |

### Notifications (F-NOT)

| ID | Feature | Table |
|---|---|---|
| F-NOT-001 | Emit notification (Core writes automatically on state transitions; not a public API) | `task_notifications` |
| F-NOT-002 | Poll pending events (filter by `notify_target` prefix or `event_type`) | `task_notifications` |
| F-NOT-003 | Mark event delivered (set `fired_at`) | `task_notifications` |
| F-NOT-004 | Replay event (clear `fired_at` for reprocessing) | `task_notifications` |
| F-NOT-005 | Stats: pending count by target | `task_notifications` |

### Audit log (F-LOG)

| ID | Feature | Table |
|---|---|---|
| F-LOG-001 | Append entry (Core writes automatically on every state-changing action) | `task_log` |
| F-LOG-002 | Read by task (full chronological history) | `task_log` |
| F-LOG-003 | Read by date range, by actor, by action | `task_log` |
| F-LOG-004 | Daily / weekly summary statistics | `task_log` |

### System (F-SYS)

| ID | Feature | Table / target |
|---|---|---|
| F-SYS-001 | Health check (DB reachable, migrations applied) | -- |
| F-SYS-002 | Version info (Planner version, schema version) | `_migrations` |
| F-SYS-003 | Migrations runner (apply pending `000N_*.sql` in order, idempotent) | `_migrations` (auto-created) |
| F-SYS-004 | Backup / restore guidance (`pg_dump` / `pg_restore`) | -- |

---

## Interface parity

Every F-xxx must be exposed in all four interfaces. There is no F that lives in only one. The only difference between interfaces is shape (CLI flags vs JSON body vs MCP arg vs UI form), not capability.

| Interface | Purpose | Shape |
|---|---|---|
| CLI | Operator scripting, quick local use | `planner <verb> <noun> [flags]` |
| REST | Machine integration, host UIs | `/api/v1/<resource>` JSON |
| MCP | Agent integration | MCP tool over stdio or HTTP |
| Web UI | Default interactive interface | Browser pages over REST |

Concrete check: when `02-spec.md` is finalised, every F-xxx must map to one row each in CLI command list, REST endpoint list, MCP tool list, UI screen list. These four lists become `05-api-reference.md`.

---

## Feature -> table coverage

Every table from `data-dictionary.md` must be reachable through at least one feature. Sanity check:

| Table | Features touching it |
|---|---|
| `actors` | F-ACT-001..005, F-LOG (FK constraint check) |
| `projects` | F-PRJ-001..005, F-OBJ (FK), F-TSK (FK) |
| `objectives` | F-OBJ-001..009, F-TSK-001 (FK) |
| `tasks` | F-TSK-*, F-TSK-P-* |
| `task_comments` | F-CMT-001..004 |
| `task_relations` | F-REL-001..003, F-TSK-P-013 |
| `task_commits` | F-CMM-001..003 |
| `task_log` | F-LOG-001..004, every state-changing F (Core writes) |
| `task_tags` | F-TAG-001..005 |
| `task_links` | F-LNK-001..004 |
| `task_notifications` | F-NOT-001..005, every transition F (Core writes) |

Every table has explicit features. No silent table.

---

## Out of scope (recap from vision Non-Goals)

These are not features and will not become features without revisiting `01-vision.md`:

- Document storage, version control of attachments.
- Semantic search, embeddings, full-text retrieval beyond simple LIKE on `title` / `description`.
- Workflow / agent orchestration. Tasks change state because something (human, CLI, agent, REST call) explicitly transitions them. Planner does not auto-execute next steps.
- LLM calls of any kind.
- Email / Slack / webhook delivery (consumers handle).
- Gantt charts, resource allocation, financial forecasting.
- Hosted SaaS in v1.

---

## Open questions (for next discussion)

These are deliberate gaps that need decisions before the next milestone. Listed in priority order.

1. **Auth model.** Standalone: API-key with operator-managed keys table? OAuth2? None and bind to localhost only? Embedded: deferred entirely to host?
2. **Migration runner.** Custom Python that scans `migrations/` and tracks `_migrations`, or pull in Alembic from day one?
3. **Schema namespace.** Standalone uses `public`. Embedded: must support `--schema=planner` so multiple OSS components can coexist in one database. Migrations file must be namespace-aware (parametrise schema name in DDL).
4. **Pagination + sort conventions.** Cursor-based or offset+limit? Default page size? Universal sort field handling?
5. **Bulk operations.** Bulk-update / bulk-tag / bulk-status? Or only single-row?
6. **Live updates.** WebSocket / SSE on UI for live board, or polling?
7. **Time zones.** UTC everywhere internally; UI converts for display? Or per-actor TZ field?
8. **Soft delete vs hard delete.** Tasks: only `rejected` (soft), never DELETE? Other tables (`task_links`, `task_tags`, `task_comments`): hard delete via cascade?
9. **Search depth.** v1 is `LIKE '%term%'`. When (if) do we need pg_trgm or `tsvector`?

These are the topics for the **next discussion** the user signalled at the end of this sprint. Each becomes a small DD-NNN entry in `03-architecture.md` once decided.

---

## Test matrix (placeholder)

Detailed test plan lives in `06-testing-strategy.md` (TBD). For each F-xxx the matrix will record:

- Unit test reference (function name).
- Integration test reference (Phase 4.5 live scenario).
- UX test reference (Phase 4.6 L1 / L2 / L3) -- only for features with human-facing surface (CLI prompts, UI screens, MCP tool descriptions).

Hard rule: a feature is not "complete" until all three test references exist (or UX is explicitly N/A for headless features like F-NOT-001 internal emit).

---

> Next: [03-architecture.md](03-architecture.md) -- components and design decisions.
