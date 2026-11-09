-- Planner: initial schema
-- Migration 0001
--
-- Creates the 11 core tables on a fresh PostgreSQL 16 database:
--   actors, projects, objectives, tasks,
--   task_comments, task_relations, task_commits, task_log,
--   task_tags, task_links, task_notifications.
--
-- Seeds the 'system' actor.
--
-- To upgrade later, add 0002_*.sql files and run them in order.

BEGIN;

-- =========================================================================
-- 1. actors -- registry of who can act on tasks (humans, agents, system)
-- =========================================================================
CREATE TABLE actors (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    type          TEXT NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT actors_type_check CHECK (type IN ('agent', 'human', 'system'))
);

COMMENT ON TABLE actors IS
    'Registry of actors who can act on tasks. Foreign-keyed from tasks.assignee, tasks.requester, task_comments.author, task_log.actor. Unknown actors are rejected at the DB layer.';

INSERT INTO actors (id, display_name, type) VALUES
    ('system', 'System', 'system')
ON CONFLICT (id) DO NOTHING;

-- =========================================================================
-- 2. projects -- top-level container
-- =========================================================================
CREATE TABLE projects (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    sort_order  INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT projects_status_check
        CHECK (status IN ('active', 'archived'))
);

COMMENT ON TABLE projects IS
    'Top-level grouping. Tasks and objectives belong to a project. slug is the human/CLI handle.';

-- =========================================================================
-- 3. objectives -- hierarchical tree of goals above the task level
-- =========================================================================
CREATE TABLE objectives (
    id           SERIAL PRIMARY KEY,
    parent_id    INT REFERENCES objectives(id) ON DELETE SET NULL,
    project_id   INT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    title        TEXT NOT NULL,
    type         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    sort_order   INT NOT NULL DEFAULT 0,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT objectives_type_check
        CHECK (type IN ('strategy', 'roadmap', 'milestone', 'goal', 'initiative', 'campaign')),
    CONSTRAINT objectives_status_check
        CHECK (status IN ('active', 'completed', 'paused', 'cancelled', 'draft'))
);

CREATE INDEX idx_objectives_parent  ON objectives(parent_id);
CREATE INDEX idx_objectives_project ON objectives(project_id);
CREATE INDEX idx_objectives_type    ON objectives(type);
CREATE INDEX idx_objectives_status  ON objectives(status);

COMMENT ON TABLE objectives IS
    'Hierarchical goals above the task level. Self-referential tree via parent_id; tree shape is defined by parent_id, not by type. Tasks attach via tasks.objective_id.';

-- =========================================================================
-- 4. tasks -- the central entity
-- =========================================================================
CREATE TABLE tasks (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT,
    project_id    INT REFERENCES projects(id) ON DELETE SET NULL,
    objective_id  INT REFERENCES objectives(id) ON DELETE SET NULL,

    -- Actors (FK to actors.id -- DB rejects unknown actors)
    assignee      TEXT REFERENCES actors(id) ON DELETE SET NULL,
    requester     TEXT REFERENCES actors(id) ON DELETE SET NULL,

    -- Priority and lifecycle
    priority      TEXT NOT NULL DEFAULT 'medium',
    status        TEXT NOT NULL DEFAULT 'open',

    -- Pipeline timestamps
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    assessed_at             TIMESTAMPTZ,
    approved_at             TIMESTAMPTZ,
    execution_started_at    TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    due_date                DATE,

    -- Assessment metadata
    complexity_hint  TEXT,
    scope            TEXT,
    eta              TEXT,

    -- Blocking and dependencies
    blocked_by       INT REFERENCES tasks(id) ON DELETE SET NULL,
    blocked_reason   TEXT,
    external_detail  TEXT,
    previous_status  TEXT,

    -- Verification
    verification_attempts INT NOT NULL DEFAULT 0,

    CONSTRAINT tasks_priority_check
        CHECK (priority IN ('low', 'medium', 'high', 'critical')),

    CONSTRAINT tasks_status_check
        CHECK (status IN (
            'open',                -- INTAKE: new task
            'assessing',           -- ASSESSMENT: investigating
            'awaiting_approval',   -- APPROVAL: needs sign-off
            'in_progress',         -- EXECUTION: work underway
            'pending_review',      -- VERIFICATION: needs review
            'done',                -- DELIVERY: completed
            'blocked',             -- Paused: dependency
            'escalated',           -- Paused: needs higher-level attention
            'rejected',            -- Terminal: not proceeding
            'incident'             -- Post-delivery: something broke
        )),

    CONSTRAINT tasks_complexity_check
        CHECK (complexity_hint IS NULL OR complexity_hint IN (
            'simple', 'moderate', 'complex', 'unknown'
        )),

    CONSTRAINT tasks_blocked_reason_check
        CHECK (blocked_reason IS NULL OR blocked_reason IN (
            'task_dependency', 'external_approval', 'scheduled_time'
        ))
);

CREATE INDEX idx_tasks_project    ON tasks(project_id);
CREATE INDEX idx_tasks_objective  ON tasks(objective_id);
CREATE INDEX idx_tasks_assignee   ON tasks(assignee);
CREATE INDEX idx_tasks_requester  ON tasks(requester);
CREATE INDEX idx_tasks_status     ON tasks(status);
CREATE INDEX idx_tasks_priority   ON tasks(priority);
CREATE INDEX idx_tasks_due_date   ON tasks(due_date);

COMMENT ON TABLE tasks IS
    'Central entity. Six-stage pipeline (intake -> assessment -> approval -> execution -> verification -> delivery) plus paused (blocked, escalated), terminal (rejected), and post-delivery (incident) states -- ten in total.';
COMMENT ON COLUMN tasks.assignee IS
    'Who is doing the task. FK to actors.id.';
COMMENT ON COLUMN tasks.requester IS
    'Who asked for the task. FK to actors.id.';
COMMENT ON COLUMN tasks.previous_status IS
    'Snapshot taken when status moves to blocked or escalated; restored on unblock.';

-- =========================================================================
-- 5. task_comments -- plain-text discussion attached to tasks
-- =========================================================================
CREATE TABLE task_comments (
    id            SERIAL PRIMARY KEY,
    task_id       INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author        TEXT NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    content       TEXT NOT NULL,
    comment_type  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_comments_task ON task_comments(task_id);
CREATE INDEX idx_task_comments_type ON task_comments(comment_type);

COMMENT ON COLUMN task_comments.comment_type IS
    'Optional category: assessment, review, incident_followup, escalation, or NULL for general.';

-- =========================================================================
-- 6. task_relations -- typed graph edges between tasks
-- =========================================================================
CREATE TABLE task_relations (
    id          SERIAL PRIMARY KEY,
    task_id     INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    related_id  INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT task_relations_check
        CHECK (relation IN ('parent', 'child', 'hotfix', 'postmortem')),
    CONSTRAINT task_relations_no_self
        CHECK (task_id <> related_id),
    CONSTRAINT task_relations_unique
        UNIQUE (task_id, related_id, relation)
);

CREATE INDEX idx_task_relations_task    ON task_relations(task_id);
CREATE INDEX idx_task_relations_related ON task_relations(related_id);

-- =========================================================================
-- 7. task_commits -- references to source-control commits
-- =========================================================================
CREATE TABLE task_commits (
    id            SERIAL PRIMARY KEY,
    task_id       INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    repo          TEXT,
    commit_hash   TEXT,
    message       TEXT,
    author        TEXT,
    content_body  TEXT,
    committed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_commits_task ON task_commits(task_id);

COMMENT ON COLUMN task_commits.content_body IS
    'Full commit body kept locally so reviews and AI agents can read it without round-tripping to the source-control host.';

-- =========================================================================
-- 8. task_log -- immutable audit trail of every state change
-- =========================================================================
CREATE TABLE task_log (
    id          SERIAL PRIMARY KEY,
    task_id     INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action      TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    actor       TEXT NOT NULL DEFAULT 'system' REFERENCES actors(id) ON DELETE RESTRICT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_log_task    ON task_log(task_id);
CREATE INDEX idx_task_log_action  ON task_log(action);
CREATE INDEX idx_task_log_actor   ON task_log(actor);
CREATE INDEX idx_task_log_created ON task_log(created_at);

COMMENT ON TABLE task_log IS
    'Append-only audit trail. Every action that changes task state writes one row. Never updated, never deleted. actor is FK to actors.id so unknown actors are rejected.';

-- =========================================================================
-- 9. task_tags -- free-form labels on tasks
-- =========================================================================
CREATE TABLE task_tags (
    task_id  INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (task_id, tag)
);

CREATE INDEX idx_task_tags_tag ON task_tags(tag);

COMMENT ON TABLE task_tags IS
    'Free-form labels (no taxonomy enforcement). Composite PK ensures uniqueness per task. Examples: needs-review, blocked:approval, sprint-1, epic:checkout.';

-- =========================================================================
-- 10. task_links -- N:1 external-resource references
-- =========================================================================
CREATE TABLE task_links (
    id          SERIAL PRIMARY KEY,
    task_id     INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    url         TEXT NOT NULL,
    label       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_links_task ON task_links(task_id);
CREATE INDEX idx_task_links_kind ON task_links(kind);

COMMENT ON TABLE task_links IS
    'External resource references. kind is free-form (plan, worklog, document, pr, deploy, session, external). Planner is agnostic to what is on the other side of the URL.';

-- =========================================================================
-- 11. task_notifications -- generic outbound event queue
-- =========================================================================
CREATE TABLE task_notifications (
    id             SERIAL PRIMARY KEY,
    task_id        INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type     TEXT NOT NULL,
    notify_target  TEXT NOT NULL,
    payload        JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    fired_at       TIMESTAMPTZ
);

CREATE INDEX idx_task_notifications_pending
    ON task_notifications(created_at) WHERE fired_at IS NULL;
CREATE INDEX idx_task_notifications_task
    ON task_notifications(task_id);

COMMENT ON TABLE task_notifications IS
    'Outbound event queue. Planner writes events on state transitions. Downstream services (email, webhook, slack, agents) poll WHERE fired_at IS NULL, deliver, then set fired_at. Planner is unaware of consumers.';
COMMENT ON COLUMN task_notifications.notify_target IS
    'Free-form target identifier interpreted by the consumer, e.g. email:requester, webhook:slack, agent:secretary.';

COMMIT;
