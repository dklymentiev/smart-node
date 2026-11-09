# Planner -- Architecture

**Status:** Draft, v0.1
**Document:** 03-architecture (per Software Product Template v1.1)
**Date:** 2026-04-27

> Why this section matters: architecture docs prevent tribal knowledge. New
> contributors need to understand the system shape before touching code.
> Design decisions with rationale prevent re-litigating settled debates.

---

## System Overview

Planner is a PostgreSQL-backed task manager with three input/output surfaces (REST, MCP, CLI), a thin web UI, and a generic outbound event queue (`task_notifications`) consumed by external services.

```
                        +-------------+
                        |   Web UI    |
                        +------+------+
                               |
                          HTTPS / REST
                               |
            +----------+-------+--------+----------+
            |          |                |          |
            v          v                v          v
        +-------+  +-------+        +-------+  +--------+
        |  CLI  |  | REST  |        |  MCP  |  | Notify |
        +---+---+  +---+---+        +---+---+  | reader |
            |          |                |       +---+----+
            +----------+----------------+           |
                              |                     |  reads
                              v                     |  task_notifications
                        +-----+------+              |
                        |   Core     |  -- writes ->|
                        |   logic    |
                        +-----+------+
                              |
                        +-----+------+
                        | PostgreSQL |
                        +------------+
```

External services (email gateway, slack notifier, flow engine, ...) are NOT part of Planner. They consume `task_notifications` and react. Planner has zero awareness of who reads its events.

## Components

| Component | Responsibility | Interface | Dependencies |
|---|---|---|---|
| Core | Pipeline logic, transitions, validation | Internal Python module | PostgreSQL |
| REST API | HTTP endpoints, auth | FastAPI (HTTP/JSON) | Core |
| MCP server | Agent-facing tool surface | MCP protocol | Core |
| CLI | Operator interface | `planner ...` shell command | REST API |
| Web UI | Default standalone UI | Browser (HTML/JS) | REST API |
| Migrations | Schema versioning | Raw SQL files in `migrations/` | psql |

(Sizing budgets and code structure: TBD when implementation begins.)

## Layer Map

```
Layer        | May call           | Must NOT call
-------------+--------------------+-------------------------
Interfaces   | Core               | Database driver directly
Core         | Repositories       | Interfaces, external HTTP
Repositories | Database driver    | Core, external services
```

---

## Design Decisions

### DD-001: PostgreSQL 16 as the only data store

| Field | Value |
|---|---|
| Date | 2026-04-27 |
| Status | Accepted |
| Context | The task lifecycle requires transactional consistency: status change + audit-log entry + notification-queue insert must be atomic. Reporting needs rich query capability. |
| Decision | PostgreSQL 16 is the only persistent store. No Redis, no SQLite, no message broker. |
| Alternatives | SQLite (single-writer, embedded -- too limiting for multi-user). MongoDB (no relational integrity, no transactional joins). Postgres + Redis (extra moving part for v1). |
| Consequences | One process to operate. ACID transactions. JSONB available for flexible metadata. Slight overkill for single-user setup, but the install path scales without change. |

### DD-002: Pipeline statuses as CHECK constraints, not lookup tables

| Field | Value |
|---|---|
| Date | 2026-04-27 |
| Status | Accepted |
| Context | Ten task statuses, six objective types, four relation types. Could be lookup tables (allows runtime addition) or embedded as CHECK constraints. |
| Decision | Embed as CHECK constraints in DDL. |
| Alternatives | Lookup tables -- adds JOINs to every query, no documentation in DDL. |
| Consequences | Statuses change rarely; embedded enum is documentation in DDL. Adding/removing a status requires a migration, which is correct -- it is a schema change. |

### DD-003: Single pipeline mode (no `simple` / `gated` toggle)

| Field | Value |
|---|---|
| Date | 2026-04-27 |
| Status | Accepted |
| Context | Some users may want only `open / in_progress / done`. Should we offer two modes via a per-project setting? |
| Decision | One mode, ten statuses. CHECK does not force traversal of all of them. Users who want simple use `open -> in_progress -> done` and ignore the rest. |
| Alternatives | `pipeline_mode = simple|gated` per project -- doubles code paths and tests. |
| Consequences | Single code path. Full lifecycle is visible to anyone reading the schema. Lightweight users pay no cost they did not opt into. |

### DD-004: External resources as `task_links` table, not columns

| Field | Value |
|---|---|
| Date | 2026-04-27 |
| Status | Accepted |
| Context | Tasks reference plans, worklogs, PRs, deploy logs, sessions, etc. Could be N typed columns (`plan_url`, `worklog_url`, ...) or one generic links table. |
| Decision | Single table `task_links (task_id, kind, url, label)`. Planner is agnostic to what `kind` means or what is at the URL. |
| Alternatives | Per-system columns -- couples Planner to a fixed list of external systems. |
| Consequences | Fully agnostic to external systems. N links of any kind per task. Loses single-column query convenience (compensated by simple JOIN). |

### DD-005: `actors` registry with FK enforcement everywhere

| Field | Value |
|---|---|
| Date | 2026-04-27 |
| Status | Accepted |
| Context | Free-text actor strings led to "claude" being recorded for every action regardless of which agent ran it (production incident, prior internal deployment). Audit trail became unreliable. |
| Decision | Table `actors (id, display_name, type, active)`. FK from `tasks.assignee`, `tasks.requester`, `task_comments.author`, `task_log.actor`. |
| Alternatives | Free-text columns (no enforcement). Lookup table without FK (no enforcement). |
| Consequences | DB rejects unknown actors -- audit trail is reliable. Operator must register an actor before first use; install seeds `system`, others added by `INSERT ... ON CONFLICT DO NOTHING`. |

### DD-006: `task_notifications` as generic outbound event queue, consumers external

| Field | Value |
|---|---|
| Date | 2026-04-27 |
| Status | Accepted |
| Context | Polling task statuses for state changes is unreliable -- in one production incident a downstream service emailed the requester before the document the email referenced was assembled. The system needs an explicit event surface. |
| Decision | Planner writes events to `task_notifications` on state transitions. Downstream services (email, slack, webhook, agents, flow engines) poll `WHERE fired_at IS NULL`, deliver, then set `fired_at`. Planner is unaware of consumers. |
| Alternatives | Bake email / slack / webhook delivery into Planner -- couples Planner to delivery channels. Postgres LISTEN/NOTIFY -- does not survive consumer restart, no replay. |
| Consequences | Planner has one boundary: state + audit. Anything else is a consumer. Multiple consumers can read the same event independently. Trade-off: each consumer must implement its own dedup / retry. |

### DD-007: Raw SQL migrations in v1 (no migration framework yet)

| Field | Value |
|---|---|
| Date | 2026-04-27 |
| Status | Accepted |
| Context | Schema versioning frameworks (Alembic, Flyway) add a dependency and a layer of indirection. v1 has a single migration. |
| Decision | Migrations are raw SQL files in `migrations/` (`0001_initial.sql`, `0002_*.sql`, ...). The installer runs them in order on first start; subsequent starts skip applied files. |
| Alternatives | Alembic from day one -- heavier setup, autogeneration unnecessary for one file. |
| Consequences | Anyone can read the schema by opening one SQL file. Adding Alembic later is straightforward when the schema starts evolving. |

### DD-008: Auth at the edge -- Planner owns no user store of its own

| Field | Value |
|---|---|
| Date | 2026-05-25 |
| Status | Accepted (planned for v0.2.0; v0.1.0 ships the standalone API-key login as an interim) |
| Context | v0.1.0 implemented self-contained auth (`actors` + `api_keys`, sha256-hashed, `/ui/login` form takes a pasted key). For the typical single-machine operator this is friction: the very first screen asks for `planner_...` instead of showing the dashboard. Sibling tools in the same system handle auth differently and none have a built-in login UI: **Doci** auto-authenticates as a seeded `dev` user in dev mode and reads `Remote-User` from a trusted reverse proxy (Traefik / Authum forward-auth) in prod, with `X-API-Key` available for service-to-service callers. **Mesh** has no UI login -- it exposes scoped `X-API-Key` for service-to-service callers only. **Rein** is a pure CLI tool with no UI and no own auth at all (its `ANTHROPIC_API_KEY` is the LLM-provider credential, not Rein's). Planner is the only one with its own browser login. The real consistency win is operational: when Doci + Mesh + Planner sit behind the same Traefik with forward-auth, one upstream identity (Authum) covers all three; today the planner login breaks that property. The audit log still needs `actor_id` on every transition (DD-005), so identity cannot disappear -- only its source can change. |
| Decision | Adopt the Doci auth model. Identity is resolved by middleware in one of three modes selected at startup via `PLANNER_AUTH_MODE`: `dev` (default for local) auto-binds every request to a seeded `dev` actor; `proxy` reads `Remote-User` header from a trusted reverse proxy and `INSERT ... ON CONFLICT DO NOTHING` materialises the actor on first sight; `apikey` keeps the v0.1.0 behaviour for service-to-service callers (Flint, Rein, external agents) where no proxy exists. `/ui/login` is shown only in `apikey` mode -- in `dev` and `proxy` modes the first hit lands on `/ui/`. Audit log keeps writing `actor_id` regardless of mode. |
| Alternatives | Keep v0.1.0 self-contained auth (inconsistent with siblings; bad first-run UX). Real username/password store (architectural overkill; duplicates what edge auth solves; password reset / lockout / 2FA all become Planner's problem). OAuth/OIDC built-in (locks Planner to a specific IdP; better delegated to the proxy). |
| Consequences | Planner becomes a peer of Doci in auth posture: one user store at the edge serves the whole system. Local dev open `localhost:8080/ui/` and see the dashboard immediately. The `api_keys` table stays -- it is the credential for `apikey` mode and how agents authenticate. A small risk: `proxy` mode trusts upstream blindly, so deployments must make sure the reverse proxy is the only ingress (same caveat Doci documents). The `dev` actor is shared across all browsers on a single-machine install -- audit log says "dev" for everything in local mode, which is the correct trade-off because a single-machine operator does not have a meaningful identity to record. |

### DD-009: A trusted key may name the caller it acts for

| Field | Value |
|---|---|
| Date | 2026-08-13 |
| Status | Accepted (shipped in 0.4.0) |
| Context | DD-008 gives three modes and a shared front door fits none of them. Such a door -- a controller that has already authenticated its own callers and forwards their work here -- must present two facts in one request: a credential proving it may come in, and the name of the person or agent it is acting for. `apikey` proves the first and destroys the second: every call through the door is authored by the key's owner, so a hundred callers collapse into one name and the audit trail DD-005 promises says nothing useful. `proxy` preserves the second and abandons the first: `Remote-User` is believed from anyone able to reach the socket, and the callers reaching an embedded deployment are exactly the population that can set a header. This surfaces the moment Planner is embedded rather than run standalone. The Smart Node deployment that prompted it had the planner answering unauthenticated on a docker bridge address in `dev` mode, authoring every entry as `dev`. |
| Decision | Add `api_keys.can_delegate` (migration 0005, default false). In `apikey` mode a request bearing a delegating key may name its actor through the proxy-user header, and that actor authors the work; the key's own actor authors it when no header is sent. For a key without the flag the header is ignored entirely -- not weighed, not preferred against, ignored. A delegating key sending an unusable header is refused rather than falling back to its own actor. `planner key create --delegate` is the only way to set it. |
| Alternatives | Trust `Remote-User` in `apikey` mode for every key: re-creates the `proxy` weakness behind a credential, which is worse than either mode alone because the forged name now arrives with a credential attesting it. A second header naming the delegator: more surface, same trust question. Per-caller keys issued to every agent: the credential then lives wherever the agent lives, and the number of secrets grows with the number of callers. |
| Consequences | The door and its callers become separable in the record: entries carry the real actor while `request.state.api_key_id` still names the key that carried them. Delegation is a deliberate grant, so an operator can hold it to one component. The risk is stated rather than mitigated: whatever holds a delegating key can author work under any name, so it is only as trustworthy as its own authentication of callers, and granting it broadly is equivalent to running in `proxy` mode. |

---

## Data Model

11 tables. Authoritative DDL: [`migrations/0001_initial.sql`](../migrations/0001_initial.sql).

### Diagram (logical)

```
                   +---------+
                   | actors  |
                   +----+----+
                        |
     +----+----+----+---+---+----+----+
     |         |        |        |    |
  assignee  requester  author  actor (audit)
     |         |        |        |
     v         v        v        v
+---------+   +-----+   +-------------+   +---------+
| projects|<--|tasks|-->|task_comments|   |task_log |
+----+----+   +--+--+   +-------------+   +---------+
     |           |
     |     +-----+-----+--------------+----------+
     |     |           |              |          |
     |     v           v              v          v
     | task_relations  task_commits   task_tags  task_links
     |     (self-ref)
     |
     v
+----+-----+
|objective |<-- parent_id (self-referential)
+----------+

                                                 +-------------------+
                                                 |task_notifications |  (event queue)
                                                 +-------------------+
```

### Tables

#### `actors`

Registry of who can act on tasks. `id` is a stable handle (e.g. `alex`, `pm`, `secretary`, `system`). FK source for `tasks.assignee`, `tasks.requester`, `task_comments.author`, `task_log.actor`.

| Column | Type | Notes |
|---|---|---|
| id | TEXT PK | stable handle |
| display_name | TEXT | human label |
| type | TEXT | `agent` / `human` / `system` |
| active | BOOLEAN | soft flag |
| created_at | TIMESTAMPTZ | |

Seed: `system` is inserted by the migration.

#### `projects`

Top-level grouping. Tasks and objectives belong to a project.

| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | |
| slug | TEXT UNIQUE | URL / CLI handle |
| title, description | TEXT | |
| status | TEXT | `active` / `archived` |
| sort_order | INT | UI sort |
| created_at, updated_at | TIMESTAMPTZ | |

#### `objectives`

Hierarchical tree of goals above the task level. Self-referential via `parent_id`. Six type values from highest abstraction (`strategy`) to most granular (`campaign`). Any type can nest under any other -- the tree shape is defined by `parent_id`, not by type.

| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | |
| parent_id | INT FK self | tree edge |
| project_id | INT FK projects | required |
| title, description | TEXT | |
| type | TEXT | strategy / roadmap / milestone / goal / initiative / campaign |
| status | TEXT | active / completed / paused / cancelled / draft |
| sort_order | INT | sibling order |
| created_at, updated_at | TIMESTAMPTZ | |

#### `tasks`

Central entity. Six-stage pipeline (intake -> assessment -> approval -> execution -> verification -> delivery) plus paused (`blocked`, `escalated`), terminal (`rejected`), and post-delivery (`incident`) states -- ten in total.

| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | |
| title, description | TEXT | |
| project_id | INT FK projects | optional |
| objective_id | INT FK objectives | optional |
| assignee | TEXT FK actors | who is doing it |
| requester | TEXT FK actors | who asked |
| priority | TEXT | low / medium / high / critical |
| status | TEXT | one of ten -- see below |
| created_at, updated_at | TIMESTAMPTZ | |
| assessed_at | TIMESTAMPTZ | set when -> awaiting_approval |
| approved_at | TIMESTAMPTZ | set when awaiting_approval -> in_progress |
| execution_started_at | TIMESTAMPTZ | set when -> in_progress |
| completed_at | TIMESTAMPTZ | set when -> done |
| due_date | DATE | optional |
| complexity_hint | TEXT | simple / moderate / complex / unknown |
| scope | TEXT | free text: paths / areas |
| eta | TEXT | "30m", "2h", "1d" |
| blocked_by | INT FK self | blocker task |
| blocked_reason | TEXT | task_dependency / external_approval / scheduled_time |
| external_detail | TEXT | for `external_approval` reason |
| previous_status | TEXT | snapshot taken on `blocked` / `escalated`, restored on unblock |
| verification_attempts | INT | auto-escalate after 2 failed reviews |

**Status transitions:**

| From | Allowed To |
|---|---|
| open | assessing, in_progress, rejected, blocked |
| assessing | awaiting_approval, in_progress, blocked, escalated, rejected |
| awaiting_approval | in_progress, rejected, blocked, escalated |
| in_progress | pending_review, blocked, escalated, done |
| pending_review | done, in_progress, escalated |
| blocked | (restores `previous_status`) |
| escalated | open, assessing, in_progress, rejected |
| done | incident |
| incident | in_progress, done |

Transition enforcement lives in Core (not the DDL CHECK), because the allowed set depends on the current row, not just the new value.

#### `task_comments`

Plain-text discussion attached to a task. `comment_type` enables filtered views (e.g. show only assessment comments) without parsing text. Author is FK to `actors.id`.

#### `task_relations`

Typed graph edges between tasks: `parent`, `child`, `hotfix`, `postmortem`. Self-loop forbidden. Uniqueness on `(task_id, related_id, relation)`.

#### `task_commits`

Source-control commit references with full content body kept locally so reviews and AI agents can read commits without round-tripping to the source-control host.

#### `task_log`

Append-only audit trail. Every action that changes task state writes one row. Never updated, never deleted. `actor` is FK to `actors.id` -- unknown actors are rejected at the DB layer.

#### `task_tags`

Free-form labels (no taxonomy enforcement). Composite PK `(task_id, tag)` -- each tag unique per task. Examples observed in production: `needs-review`, `blocked:approval`, `icebox`, `sprint-1`, `epic:checkout`.

#### `task_links`

External resource references. `kind` is free text -- Planner is agnostic to what is on the other side. Examples: `plan`, `worklog`, `document`, `pr`, `deploy`, `session`, `external`. Replaces the per-system columns (`plan_guid`, `worklog_guid`, `deepdoc_guid`, `session_id`) found in earlier internal versions.

#### `task_notifications`

Outbound event queue. Planner writes events on state transitions; downstream consumers (email, slack, webhook, agents) poll `WHERE fired_at IS NULL`, deliver, then set `fired_at`. `notify_target` is a free-text identifier (`email:requester`, `webhook:slack`, `agent:secretary`) parsed by the consumer.

| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | |
| task_id | INT FK tasks | |
| event_type | TEXT | free text (`review_ready`, `draft_ready`, `approval_needed`, `escalated`) |
| notify_target | TEXT | free text consumer hint |
| payload | JSONB | optional event-specific data |
| created_at | TIMESTAMPTZ | |
| fired_at | TIMESTAMPTZ | NULL = pending |

Partial index `WHERE fired_at IS NULL` keeps the polling query fast even with millions of historical events.

---

## Architectural Boundary

Planner = state + audit. Everything else = consumer.

- **Notifications, email, slack:** consumers of `task_notifications`. Not in scope.
- **Flow / workflow engines:** consumers of `tasks` and `task_log`. They orchestrate transitions; they do not live inside Planner.
- **Memory / semantic search / document storage:** out of scope. Linked from `task_links` only.
- **Auth / SSO:** boundary undecided for v1 -- minimal API-key auth in standalone, deferred to host system in embedded mode.

Boundary tested by a single rule: if a feature requires Planner to know about another system's protocol or data shape, it does not belong in Planner.

---

## Integration Points

(TBD -- depends on REST / MCP API design in 02-spec and 05-api-reference.)

## Security Architecture

**Auth model: edge-trust (see [DD-008](#dd-008-auth-at-the-edge----planner-owns-no-user-store-of-its-own)).** Planner does not own a user store. Identity arrives at the application boundary through one of three configured paths:

1. `dev` mode (default for local): middleware binds the request to a seeded `dev` actor. No `/ui/login`. Use only behind `localhost` -- there is no credential check.
2. `proxy` mode (production behind Traefik / Authum / any forward-auth): the reverse proxy authenticates the user and sets `Remote-User`. Middleware materialises the actor (`INSERT ... ON CONFLICT DO NOTHING`) on first sight and proceeds. The proxy is the only ingress; expose Planner on a private interface or bind it behind a network policy.
3. `apikey` mode (service-to-service from outside any proxy -- Flint, Rein, agents): the caller sends `X-API-Key`; the key is sha256-hashed at rest, looked up, and resolves to its owning actor. `/ui/login` and cookie sessions exist only in this mode for browser callers without a proxy.

**Delegation within `apikey` mode (see [DD-009](#dd-009-a-trusted-key-may-name-the-caller-it-acts-for)).** A key marked `can_delegate` may name the caller it is acting for through the proxy-user header, so a front door that authenticates its own callers can present a credential and a real author at once. For every other key that header does not exist. Grant the flag to one component and treat it accordingly: whatever holds a delegating key can author work under any name, and granting it widely is `proxy` mode with extra steps.

**Audit trail.** Every state-changing transition writes a `task_log` row with `actor_id` regardless of which auth mode produced the identity (DD-005). The audit guarantee does not depend on the auth source.

**Threat model in scope:** unauthorized API access; impersonation in audit log; CSRF on cookie-session callers in `apikey` mode (mitigated by SameSite cookie + form-token on write paths).

**Out of scope:** account recovery, password rotation, 2FA, MFA, role-based authorization -- all delegated to the upstream IdP / proxy. Multi-tenant authorization (key-scoped projects) is a v0.3+ topic.

---

> Next: [04-status-model.md](04-status-model.md) -- how a task moves between statuses.
