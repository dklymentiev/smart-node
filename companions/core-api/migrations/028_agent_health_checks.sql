-- 028: sn_core.agent_health_checks -- per-agent, per-grant probe results.
--
-- Created to support the health-checker companion.
-- The probe runs every 15 minutes from /etc/cron.d/smart-node-health-probe and
-- upserts one row per (agent_slug, grant_name, check_type) per run.
--
-- Columns:
--   agent_slug      the agent the probe ran for
--   grant_name      tool name or channel slug
--   check_type      'tool' or 'channel'
--   status          'ok', 'fail', or 'untestable'
--   last_checked_at non-optional; a green without a timestamp is a stale green
--   error_detail    NULL when ok or untestable; the failure message otherwise
--
-- The panel reads this table. The panel never writes it: a hanging
-- probe must not be able to take the panel down (architect decision).
--
-- Safe on an existing node: CREATE TABLE IF NOT EXISTS, idempotent index.

CREATE TABLE IF NOT EXISTS sn_core.agent_health_checks (
    agent_slug       text        NOT NULL,
    grant_name       text        NOT NULL,
    check_type       text        NOT NULL CHECK (check_type IN ('tool', 'channel')),
    status           text        NOT NULL CHECK (status IN ('ok', 'fail', 'untestable')),
    last_checked_at  timestamptz NOT NULL DEFAULT now(),
    error_detail     text
);

-- Unique key: one row per probe target. Each run upserts rather than appending.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sn_core.agent_health_checks'::regclass
          AND conname   = 'agent_health_checks_pkey'
    ) THEN
        ALTER TABLE sn_core.agent_health_checks
            ADD CONSTRAINT agent_health_checks_pkey
            PRIMARY KEY (agent_slug, grant_name, check_type);
    END IF;
END $$;
