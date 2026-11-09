-- Migration 010: Policies registry (real backend for /policies/ panel page)
--
-- Adds sn_config.policies. Per-agent overrides already live in
-- sn_config.agent_policy_opts (Migration 008b) -- this table is the base
-- registry the override layer references.
--
-- Out of scope: runtime resolver that bakes policies into agent system
-- prompts. That's downstream of Stage 7 panel work.

BEGIN;

CREATE TABLE sn_config.policies (
    id          serial PRIMARY KEY,
    tenant_id   integer REFERENCES sn_config.tenants(id),
    slug        text NOT NULL,
    category    text NOT NULL,
    title       text NOT NULL,
    body_md     text NOT NULL,
    severity    text NOT NULL DEFAULT 'warn',
    enabled     boolean NOT NULL DEFAULT true,
    version     text NOT NULL DEFAULT '0.1.0',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text,
    updated_by  text,
    archived_at timestamptz,
    CHECK (slug ~ '^[a-z][a-z0-9-]{1,60}$'),
    CHECK (category IN ('safety','workflow','communication','data-handling')),
    CHECK (severity IN ('info','warn','block'))
);

CREATE UNIQUE INDEX policies_slug_active
  ON sn_config.policies(tenant_id, slug)
  WHERE archived_at IS NULL;

CREATE INDEX policies_category_enabled
  ON sn_config.policies(category, enabled)
  WHERE archived_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON sn_config.policies TO smartnode;
GRANT USAGE, SELECT ON SEQUENCE sn_config.policies_id_seq TO smartnode;

-- Seed: port the three policies that lived in /srv/smart-node-hq/data/policies.json
-- (mock seed used before this migration shipped).
INSERT INTO sn_config.policies (tenant_id, slug, category, title, body_md, severity, enabled, created_by, updated_by) VALUES
(1, 'no-read-png', 'safety',
 'Never read PNG via vision',
 'Convert PNG to JPEG before passing to a vision model. PNGs in real-world agent workflows are typically large screenshots — they explode the context window without proportional information gain. Use a server-side `magick` or `cwebp` step first.',
 'warn', true, 'migration:010', 'migration:010'),
(1, 'worklog-before-done', 'workflow',
 'Worklog must exist before status=done',
 'An agent may not transition a task to `done` until a Mesh worklog is linked via `task.sh update <id> --worklog <guid>`. Worklog is the only persistent record of *what was actually done* — task description records intent, worklog records reality.',
 'block', true, 'migration:010', 'migration:010'),
(1, 'agent-never-closes-own-task', 'workflow',
 'Agent does not close its own task',
 'After completing work, the agent sets status to `pending_review` and leaves the close to the human reviewer. This preserves the audit boundary between *agent claims it is done* and *human accepts it*.',
 'block', true, 'migration:010', 'migration:010');

COMMIT;

-- Verification:
-- \d sn_config.policies
-- SELECT slug, category, severity, enabled FROM sn_config.policies;
--
-- ROLLBACK (manual):
--   BEGIN; DROP TABLE sn_config.policies; COMMIT;
