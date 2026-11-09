# Planner -- Data Dictionary

**Status:** Draft, v0.1
**Date:** 2026-04-27
**Authoritative DDL:** [`migrations/0001_initial.sql`](../migrations/0001_initial.sql)

---

## Purpose of this document

This is the canonical reference for Planner's data model. It answers, for every column of every table, the single question:

> **Why does this field exist?**

Not what it stores -- the name and type usually tell you that. **Why** -- which scenario it closes, which past bug it prevents, which business question it lets us answer. If the answer is "I do not know", the field is a candidate for removal.

Everything else in Planner -- API, CLI, MCP tools, UI screens -- is a projection of this model. If a feature cannot be expressed against the schema below, either the schema is missing something or the feature is out of scope.

---

## Map of tables

```
                   +---------+
                   | actors  |   registry of who can act
                   +----+----+
                        |
     +----+----+---+----+----+----+
     |         |        |         |
  assignee  requester author     actor
     v         v        v         v
+---------+   +-----+   +-------------+   +---------+
| projects|<--|tasks|-->|task_comments|   |task_log |
+---------+   +-+-+-+   +-------------+   +---------+
     ^          | |
     |          | +-->task_relations    +-----------+
+----+-----+    |     (self-ref)        |task_tags  |
|objective | <--+-->task_commits        |task_links |
+----+-----+    |                       +-----------+
     ^          v
     |     +--------------------+
parent_id  | task_notifications |  outbound event queue
(self)     +--------------------+
```

| Table | One-line role |
|---|---|
| `actors` | Registry of who can act (humans, agents, system). FK target everywhere an "author" exists. |
| `projects` | Top-level grouping. Tasks and objectives live inside one. |
| `objectives` | Hierarchical tree of goals above the task level (strategy / roadmap / milestone / ...). |
| `tasks` | Central entity. Ten-status lifecycle plus blocking, ETA, verification. |
| `task_comments` | Plain-text discussion attached to a task. |
| `task_relations` | Typed graph edges between tasks (parent / child / hotfix / postmortem). |
| `task_commits` | Source-control commit references kept locally for review. |
| `task_log` | Append-only audit trail. |
| `task_tags` | Free-form labels per task. |
| `task_links` | Generic external-resource references (URL + kind + label). |
| `task_notifications` | Outbound event queue for downstream consumers. |

---

## 1. `actors`

**Purpose:** registry of every entity that can take action on a task. Without it, audit columns become free text and the system loses the ability to answer "who actually did this".

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | TEXT PK | no | -- | -- | Stable handle used in all FK references (`alex`, `pm`, `secretary`, `system`). Short and human-readable so log entries are scannable. |
| `display_name` | TEXT | no | -- | -- | Human-friendly label for UI. Decoupled from `id` so renaming a person does not break references. |
| `type` | TEXT | no | -- | -- | One of `agent`, `human`, `system`. Lets dashboards and policies treat agent activity differently from human activity (e.g. "show only human-initiated changes"). |
| `active` | BOOLEAN | no | true | -- | Soft flag for retiring an actor without breaking historical FK links. False means "do not show in pickers", but old log rows still resolve. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | When the actor was registered. Used for "new agent appeared today" diagnostics. |

**Incoming FK:** `tasks.assignee`, `tasks.requester`, `task_comments.author`, `task_log.actor`.
**Outgoing FK:** none.
**Invariants:**
- `id = 'system'` is seeded by the migration and must always exist.
- Inserting unknown actor fails downstream INSERTs by FK -- this is intentional, it forces actor registration before first use.

---

## 2. `projects`

**Purpose:** top-level container. Lets one Planner instance serve multiple distinct workstreams without cross-contamination of filters and views. A `slug` (URL-safe short name) gives every project a stable handle for CLI / API / link sharing.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal numeric ID for FK references. |
| `slug` | TEXT UNIQUE | no | -- | -- | Human/CLI handle (`infrastructure`, `website`, ...). Unique so it can be used in URLs and CLI flags without ambiguity. |
| `title` | TEXT | no | -- | -- | Display name, may include spaces and ordering prefixes. |
| `description` | TEXT | yes | -- | -- | Optional short description for the project picker. |
| `status` | TEXT | no | 'active' | -- | `active` / `archived`. Archived projects are hidden from default lists but retain history. |
| `sort_order` | INT | no | 0 | -- | Manual sort weight for the UI list -- without it, alphabetical order is the only option. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | Audit timestamp. |
| `updated_at` | TIMESTAMPTZ | no | now() | -- | Audit timestamp; set by application on UPDATE. |

**Incoming FK:** `objectives.project_id`, `tasks.project_id`.
**Outgoing FK:** none.
**Invariants:**
- `slug` is unique.
- A project with `status = archived` should not accept new tasks; enforced in Core, not DDL.

---

## 3. `objectives`

**Purpose:** flexible tree of goals above the task level. Lets a single task answer "what bigger thing is this serving?" all the way up to a strategy, without forcing a fixed hierarchy depth.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal numeric ID. |
| `parent_id` | INT | yes | -- | objectives.id | Self-referential edge. NULL = root node. Tree shape is defined by this field only -- types do not constrain nesting. |
| `project_id` | INT | no | -- | projects.id | Every objective belongs to exactly one project. |
| `title` | TEXT | no | -- | -- | Display label. |
| `type` | TEXT | no | -- | -- | One of `strategy`, `roadmap`, `milestone`, `goal`, `initiative`, `campaign`. Vocabulary, not a constraint -- any type may parent any other. |
| `status` | TEXT | no | 'active' | -- | `active` / `completed` / `paused` / `cancelled` / `draft`. Controls whether contained tasks are considered live. |
| `sort_order` | INT | no | 0 | -- | Manual sort within siblings (UI only). |
| `description` | TEXT | yes | -- | -- | Optional short description. The full plan usually lives in an external document linked via `task_links` or referenced from the objective's `description`. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | Audit. |
| `updated_at` | TIMESTAMPTZ | no | now() | -- | Audit. |

**Incoming FK:** `objectives.parent_id` (self), `tasks.objective_id`.
**Outgoing FK:** `parent_id -> objectives.id`, `project_id -> projects.id`.
**Invariants:**
- A node cannot be its own ancestor (cycle detection lives in Core, not DDL).
- Deleting a parent sets children's `parent_id` to NULL (`ON DELETE SET NULL`) -- they become roots, never silently disappear.

---

## 4. `tasks`

**Purpose:** the central entity. Every Planner action is on a task. The schema records both the work to do and the full history of how it moved through the lifecycle, so any moment in a task's life can be reconstructed without external context.

### Identity and ownership

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal numeric ID, used in CLI (`task.sh get 42`) and URLs. |
| `title` | TEXT | no | -- | -- | One-line summary. Required because anonymous tasks are useless. |
| `description` | TEXT | yes | -- | -- | Long-form context. Optional to keep quick capture cheap (just title + project). |
| `project_id` | INT | yes | -- | projects.id | Which project the task belongs to. Optional so quick personal tasks can exist without project assignment. `ON DELETE SET NULL` so deleting a project orphans tasks rather than deleting them. |
| `objective_id` | INT | yes | -- | objectives.id | Optional link upward to the goal this task serves. |
| `assignee` | TEXT | yes | -- | actors.id | Who is doing the task right now. Optional because new tasks may not yet have an owner. |
| `requester` | TEXT | yes | -- | actors.id | Who asked for the task. Lets downstream consumers (e.g. an email service) know who to notify on completion. In practice, `assignee` alone does not answer "who do I send the result to". |

### Priority and lifecycle

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `priority` | TEXT | no | 'medium' | -- | `low` / `medium` / `high` / `critical`. Drives sort order in agent task pickup and dashboards. |
| `status` | TEXT | no | 'open' | -- | One of ten lifecycle values (see Status reference below). The single most important column in the system. |

### Pipeline timestamps

Each timestamp is set when the task crosses a specific transition. They let us answer "how long was this stuck in approval?" without reading the full audit log.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `created_at` | TIMESTAMPTZ | no | now() | -- | When intake happened. |
| `updated_at` | TIMESTAMPTZ | no | now() | -- | Last-touched timestamp; set by application on UPDATE. Used for staleness detection. |
| `assessed_at` | TIMESTAMPTZ | yes | -- | -- | Set when the task moves to `awaiting_approval`. Lets us detect "approval pending more than 24h" by simple `WHERE`. |
| `approved_at` | TIMESTAMPTZ | yes | -- | -- | Set when `awaiting_approval -> in_progress`. Records explicit approval moment for audit. |
| `execution_started_at` | TIMESTAMPTZ | yes | -- | -- | Set when the task first moves to `in_progress`. May be earlier than `approved_at` if approval was skipped (simple flow). |
| `completed_at` | TIMESTAMPTZ | yes | -- | -- | Set when status -> `done`. Lets us compute throughput and lead time without scanning the audit log. |
| `due_date` | DATE | yes | -- | -- | Deadline the task is targeted for. Used for sort and for "overdue" alerts. Date, not timestamp -- task management does not need hour-of-day precision for due dates. |

### Assessment metadata (filled during the assessment stage)

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `complexity_hint` | TEXT | yes | -- | -- | `simple` / `moderate` / `complex` / `unknown`. Self-reported complexity used for batching and pickup heuristics. Hint, not contract. |
| `scope` | TEXT | yes | -- | -- | Free text listing files, areas, or systems the task touches. Lets `check-conflicts` find tasks that step on the same files before assigning agents in parallel. |
| `eta` | TEXT | yes | -- | -- | Self-reported estimate ("30m", "2h", "1d"). Drives the "shortest first" sort in agent pickup. Free text on purpose -- no parser opinion. |

### Blocking and dependencies

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `blocked_by` | INT | yes | -- | tasks.id | Self-referential link to the blocker task. Enables cascading unblock when the blocker completes. |
| `blocked_reason` | TEXT | yes | -- | -- | One of `task_dependency` / `external_approval` / `scheduled_time`. Lets the dashboard explain why a task is paused without parsing free text. |
| `external_detail` | TEXT | yes | -- | -- | Free text describing the external blocker (e.g. "waiting on legal sign-off from Sonat"). Used when `blocked_reason = external_approval`. |
| `previous_status` | TEXT | yes | -- | -- | Snapshot of `status` taken when the task entered `blocked` or `escalated`, restored on unblock. Without this, "unblock" cannot tell if the task should resume `in_progress` or `awaiting_approval`. |

### Recurrence (migration 0011)

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `recur_interval` | INTERVAL | yes | -- | -- | How often the task comes back (`1 day`, `2 weeks`, `1 month`). NULL for a one-off task. CHECK > 0. |
| `next_run` | TIMESTAMPTZ | yes | -- | -- | When the next cycle opens. Set and cleared together with `recur_interval` (CHECK). Indexed where `recur_interval` is set. |

### Verification

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `verification_attempts` | INT | no | 0 | -- | Count of `pending_review -> in_progress` round-trips. Auto-escalation triggers when this reaches 2 -- prevents infinite "almost done" loops without human attention. |

**Incoming FK:** `task_comments.task_id`, `task_relations.{task_id,related_id}`, `task_commits.task_id`, `task_log.task_id`, `task_tags.task_id`, `task_links.task_id`, `task_notifications.task_id`, `tasks.blocked_by` (self).
**Outgoing FK:** `project_id`, `objective_id`, `assignee`, `requester`, `blocked_by` (self).
**Invariants:**
- Status transitions enforced by Core (not DDL CHECK), because allowed-set depends on current row.
- `previous_status` set on entry to `blocked`/`escalated`, cleared on exit.
- `verification_attempts` increments on each `pending_review -> in_progress`; resets to 0 only on `done`.
- `blocked_by` must not form a cycle (Core check, not DDL).

---

## 5. `task_comments`

**Purpose:** plain-text discussion thread per task. Comments are typed (`comment_type`) so dashboards can filter "show only assessment notes" without parsing prose.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal ID. |
| `task_id` | INT | no | -- | tasks.id | Owner task. `ON DELETE CASCADE` -- comments do not survive their task. |
| `author` | TEXT | no | -- | actors.id | Who wrote it. FK enforces known actor. |
| `content` | TEXT | no | -- | -- | The comment body. Plain text by convention; Markdown is rendered by the UI but not enforced. |
| `comment_type` | TEXT | yes | -- | -- | Optional category: `assessment`, `review`, `incident_followup`, `escalation`, or NULL for general. Added because in practice ~30% of comments needed filtering by purpose, and grepping prose was unreliable. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | Audit. |

**Incoming FK:** none.
**Outgoing FK:** `task_id`, `author`.
**Invariants:** none beyond DDL.

---

## 6. `task_relations`

**Purpose:** typed graph edges between tasks for relationships richer than "blocked_by". Lets a `hotfix` know which `incident` it belongs to, a `child` know its `parent`, etc.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal ID. |
| `task_id` | INT | no | -- | tasks.id | The "from" side of the edge. |
| `related_id` | INT | no | -- | tasks.id | The "to" side of the edge. |
| `relation` | TEXT | no | -- | -- | One of `parent`, `child`, `hotfix`, `postmortem`. Closed enum because new relation types affect every consumer. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | Audit. |

**Invariants:**
- `task_id <> related_id` (no self-loops; CHECK).
- `(task_id, related_id, relation)` is unique.
- Edges are directional. To express both directions, insert two rows.

---

## 7. `task_commits`

**Purpose:** source-control commit references attached to a task, with the commit body kept locally so reviewers and AI agents can read commits without round-tripping to the source-control host.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal ID. |
| `task_id` | INT | no | -- | tasks.id | Owner task. |
| `repo` | TEXT | yes | -- | -- | Repository identifier (`bird-cms`, `mesh-memory`). Free text -- no host opinion. |
| `commit_hash` | TEXT | yes | -- | -- | Full or short hash. |
| `message` | TEXT | yes | -- | -- | First line / subject of the commit message. |
| `author` | TEXT | yes | -- | -- | Commit author. Free text -- not FK, because the commit author may be someone outside the actors registry (e.g. an external contributor). |
| `content_body` | TEXT | yes | -- | -- | Full commit message body. Lets reviewers read the full diff context without leaving Planner. |
| `committed_at` | TIMESTAMPTZ | no | now() | -- | When the commit was authored. Used for chronological sort. |

**Invariants:** none beyond DDL.

---

## 8. `task_log`

**Purpose:** append-only audit trail. Every action that changes task state writes one row. Never updated, never deleted. Answers "what changed and who did it" with a single SELECT.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal ID, also gives strict total order within a task. |
| `task_id` | INT | no | -- | tasks.id | Owner task. |
| `action` | TEXT | no | -- | -- | The kind of action (`created`, `status_changed`, `assigned`, `priority_changed`, `commented`, `linked_commit`, `blocked`, `unblocked`, ...). Free text -- new action types should not require migrations. |
| `old_value` | TEXT | yes | -- | -- | What the field was before. Optional because some actions have no "before" (e.g. `created`). |
| `new_value` | TEXT | yes | -- | -- | What it is now. |
| `actor` | TEXT | no | 'system' | actors.id | Who did it. Default `system` covers automatic actions (cron, cascading unblock, auto-escalation). FK enforces known actor -- the historical bug "every log row attributed to claude" is impossible by construction. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | When the action happened. Index lets dashboards query "what changed today" cheaply. |

**Invariants:**
- Append-only by convention (no UPDATE / DELETE statements anywhere in Core).
- `actor` must resolve in `actors` (FK).

---

## 9. `task_tags`

**Purpose:** free-form labels on tasks. Statuses fix the lifecycle; tags express everything else (sprint, epic, blocker subtype, "needs-review", "icebox", ...). Production experience showed this is the single most-used auxiliary table after `tasks` itself.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `task_id` | INT | no | -- | tasks.id | Owner task. Composite PK with `tag` ensures uniqueness per task. |
| `tag` | TEXT | no | -- | -- | The label. Free text -- no taxonomy enforcement. Convention is kebab-case with optional `prefix:value` for grouping (`epic:checkout`, `blocked:approval`). |

**Invariants:**
- Composite PK `(task_id, tag)` -- one row per (task, tag) pair.
- `ON DELETE CASCADE` -- tags do not survive their task.

---

## 10. `task_links`

**Purpose:** generic external-resource references. Replaces the per-system GUID columns (`plan_guid`, `worklog_guid`, `deepdoc_guid`, `session_id`) found in earlier internal versions. Planner is agnostic to what the URL points to -- the consumer interprets `kind`.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal ID. |
| `task_id` | INT | no | -- | tasks.id | Owner task. |
| `kind` | TEXT | no | -- | -- | Free-text type (`plan`, `worklog`, `document`, `pr`, `deploy`, `session`, `external`). Not constrained -- adding a new external system must not require a Planner migration. |
| `url` | TEXT | no | -- | -- | The link itself. Planner treats it as opaque. |
| `label` | TEXT | yes | -- | -- | Optional human label for the UI. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | Audit. |

**Invariants:**
- `(task_id, kind)` is NOT unique -- a task may have multiple links of the same kind (two plans, several PRs).

---

## 11. `task_notifications`

**Purpose:** outbound event queue. Planner writes events on state transitions; downstream consumers (email, slack, webhook, agents) poll `WHERE fired_at IS NULL`, deliver, then set `fired_at`. This is the single boundary between Planner and any external delivery channel -- Planner never knows who is listening.

| Column | Type | Null | Default | FK | Purpose |
|---|---|---|---|---|---|
| `id` | SERIAL PK | no | -- | -- | Internal ID; gives total order. |
| `task_id` | INT | no | -- | tasks.id | The task the event is about. `ON DELETE CASCADE` -- events for deleted tasks are gone. |
| `event_type` | TEXT | no | -- | -- | What happened (`review_ready`, `draft_ready`, `approval_needed`, `escalated`, ...). Free text so new event types do not require migrations. |
| `notify_target` | TEXT | no | -- | -- | Free-text consumer hint (`email:requester`, `webhook:slack`, `agent:secretary`). Planner does not parse it -- consumers select rows whose target they understand. |
| `payload` | JSONB | yes | -- | -- | Optional event-specific data (URL of just-rendered document, summary of approval ask, etc.). JSONB so structure can vary per `event_type` without migrations. |
| `created_at` | TIMESTAMPTZ | no | now() | -- | When the event was emitted. Indexed (partial WHERE fired_at IS NULL) so polling is cheap. |
| `fired_at` | TIMESTAMPTZ | yes | -- | -- | NULL = pending. Set by the consumer to mark "delivered". A second consumer cares only about `fired_at IS NULL`, so multiple consumers reading the same queue is safe -- but each must implement its own dedup, since Planner does not know who is who. |

**Invariants:**
- `fired_at` is set once and never updated.
- Partial index `WHERE fired_at IS NULL` keeps the polling query O(pending) regardless of total event count.

---

## Status reference

Ten lifecycle values for `tasks.status`. Each gets a one-line meaning so anyone reading the schema understands the pipeline.

| Status | Stage | Meaning |
|---|---|---|
| `open` | INTAKE | New task. Not yet picked up. |
| `assessing` | ASSESSMENT | Owner is investigating: defining scope, complexity, ETA. |
| `awaiting_approval` | APPROVAL | Assessment done, plan submitted, waiting on a human/lead sign-off. |
| `in_progress` | EXECUTION | Approved (or skipped approval) -- work is happening. |
| `pending_review` | VERIFICATION | Owner says "done" -- waiting on review/QA. |
| `done` | DELIVERY | Verified and accepted. Terminal happy path. |
| `blocked` | (paused) | Waiting on a dependency or external condition. `previous_status` recorded for restoration. |
| `escalated` | (paused) | Cannot proceed without higher-level intervention (loop limits hit, plan rejected too many times). |
| `rejected` | (terminal) | Decided not to do. Terminal sad path. |
| `incident` | (post-delivery) | A delivered task broke something in production. Triggers hotfix + post-mortem subtasks. |

### Allowed transitions

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

---

## Other enums

| Field | Allowed values |
|---|---|
| `tasks.priority` | `low`, `medium`, `high`, `critical` |
| `tasks.complexity_hint` | `simple`, `moderate`, `complex`, `unknown` |
| `tasks.blocked_reason` | `task_dependency`, `external_approval`, `scheduled_time` |
| `objectives.type` | `strategy`, `roadmap`, `milestone`, `goal`, `initiative`, `campaign` |
| `objectives.status` | `active`, `completed`, `paused`, `cancelled`, `draft` |
| `task_relations.relation` | `parent`, `child`, `hotfix`, `postmortem` |
| `actors.type` | `agent`, `human`, `system` |
| `projects.status` | `active`, `archived` |

---

## FK summary

```
actors  <--  tasks.assignee
        <--  tasks.requester
        <--  task_comments.author
        <--  task_log.actor

projects <-- objectives.project_id
         <-- tasks.project_id

objectives <-- objectives.parent_id  (self)
           <-- tasks.objective_id

tasks <-- task_comments.task_id
      <-- task_relations.task_id
      <-- task_relations.related_id
      <-- task_commits.task_id
      <-- task_log.task_id
      <-- task_tags.task_id
      <-- task_links.task_id
      <-- task_notifications.task_id
      <-- tasks.blocked_by  (self)
```

`ON DELETE` policy:
- Cascade on `task_comments`, `task_relations`, `task_commits`, `task_log`, `task_tags`, `task_links`, `task_notifications` -- they have no meaning without the parent task.
- SET NULL on `tasks.project_id`, `tasks.objective_id`, `tasks.assignee`, `tasks.requester`, `tasks.blocked_by`, `objectives.parent_id` -- the task survives, the link breaks softly.
- RESTRICT on `objectives.project_id`, `task_comments.author`, `task_log.actor` -- you may not delete a project that still has objectives, or an actor still attached to comments/log.

---

## How to use this document

1. Before adding a column: write its **Purpose** entry first. If you cannot, do not add the column.
2. Before removing a column: re-read its **Purpose** entry. If the scenario it closes is still real, do not remove.
3. When reviewing PRs that touch the schema: this document and `migrations/0001_initial.sql` (plus future `000N_*.sql` files) must move together. A schema change without a `data-dictionary.md` update is incomplete.
