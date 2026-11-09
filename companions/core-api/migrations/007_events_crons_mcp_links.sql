-- Migration 007: events + crons + cron_runs + mcp_servers extras + tools->mcp link
--
-- Wraps the initial events, crons and MCP links schema.
--
-- sn_core.events: generic event log used by all node services.
--   event_type vocabulary: cron.started/completed/failed, controller.heartbeat,
--   service.health_ok/fail, tool.registered, integration.registered, secret.created, agent.created.
-- sn_core.crons: cron registry (single-tenant at node level, so no project_id).
-- sn_core.cron_runs: execution log per cron entry.
-- sn_config.mcp_servers: add last_discovered_at, auth_secret_ref, status (endpoint stays in config_jsonb).
-- sn_core.tools: add mcp_server_id FK so a kind=mcp tool knows its origin.

BEGIN;

-- ============================================================
-- sn_core.events  -- generic event log
-- ============================================================
CREATE TABLE sn_core.events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    entity_type     TEXT NULL,
    entity_id       TEXT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    parent_event_id BIGINT NULL REFERENCES sn_core.events(id)
);

CREATE INDEX idx_sn_events_ts ON sn_core.events (ts DESC);
CREATE INDEX idx_sn_events_type ON sn_core.events (event_type);
CREATE INDEX idx_sn_events_entity ON sn_core.events (entity_type, entity_id);

COMMENT ON TABLE sn_core.events IS
    'Generic event log for this Smart Node.';
COMMENT ON COLUMN sn_core.events.actor IS
    'Who/what caused the event: system:cron, user:<name>, agent:<id>, component:<name>.';
COMMENT ON COLUMN sn_core.events.event_type IS
    'Semantic verb-noun: controller.heartbeat, cron.started, tool.registered, etc.';
COMMENT ON COLUMN sn_core.events.entity_type IS
    'Optional reference: tool, integration, cron, mcp_server, etc.';
COMMENT ON COLUMN sn_core.events.entity_id IS
    'Optional reference: id or slug of the entity_type row.';
COMMENT ON COLUMN sn_core.events.parent_event_id IS
    'Optional chain to a triggering event (e.g. cron.completed -> cron.started parent).';

-- ============================================================
-- sn_core.crons  -- cron registry
-- ============================================================
CREATE TABLE sn_core.crons (
    id              SERIAL PRIMARY KEY,
    schedule        VARCHAR(100) NOT NULL,
    command         TEXT NOT NULL,
    script_path     VARCHAR(500),
    description     VARCHAR(500) DEFAULT '',
    "group"         VARCHAR(50) DEFAULT 'other',
    log_file        VARCHAR(500) DEFAULT '',
    crontab_user    VARCHAR(50) DEFAULT 'root',
    enabled         BOOLEAN DEFAULT true,
    disabled_flag   VARCHAR(500),
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP DEFAULT now(),
    CONSTRAINT crons_schedule_command_crontab_user_key UNIQUE (schedule, command, crontab_user)
);

CREATE INDEX idx_sn_crons_enabled ON sn_core.crons (enabled);
CREATE INDEX idx_sn_crons_group ON sn_core.crons ("group");

COMMENT ON TABLE sn_core.crons IS
    'Registry of cron jobs on this node (single-tenant at node level, so no project_id).';

-- ============================================================
-- sn_core.cron_runs  -- execution log per cron entry
-- ============================================================
CREATE TABLE sn_core.cron_runs (
    id             SERIAL PRIMARY KEY,
    cron_id        INTEGER NOT NULL REFERENCES sn_core.crons(id) ON DELETE CASCADE,
    started_at     TIMESTAMP NOT NULL,
    finished_at    TIMESTAMP,
    exit_code      INTEGER,
    duration_ms    INTEGER,
    output_snippet TEXT DEFAULT '',
    created_at     TIMESTAMP DEFAULT now()
);

CREATE INDEX idx_sn_cron_runs_cron_started ON sn_core.cron_runs (cron_id, started_at DESC);

COMMENT ON TABLE sn_core.cron_runs IS
    'Per-execution log for sn_core.crons entries.';

-- ============================================================
-- sn_config.mcp_servers  -- discovery + auth ref columns
-- ============================================================
ALTER TABLE sn_config.mcp_servers
    ADD COLUMN last_discovered_at TIMESTAMPTZ NULL,
    ADD COLUMN auth_secret_ref    TEXT NULL,
    ADD COLUMN status             TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE sn_config.mcp_servers
    ADD CONSTRAINT mcp_servers_status_check
    CHECK (status IN ('unknown','available','broken'));

COMMENT ON COLUMN sn_config.mcp_servers.last_discovered_at IS
    'Last successful MCP tools/list call against this server. NULL = never reached.';
COMMENT ON COLUMN sn_config.mcp_servers.auth_secret_ref IS
    'Path inside /etc/smart-node/integrations.sops.yaml. Resolved via core-api /api/secrets/resolve at dispatch-time.';
COMMENT ON COLUMN sn_config.mcp_servers.status IS
    'unknown=not probed; available=discovery succeeded; broken=discovery failed.';

-- ============================================================
-- sn_core.tools  -- link to mcp_server origin
-- ============================================================
ALTER TABLE sn_core.tools
    ADD COLUMN mcp_server_id INTEGER NULL
        REFERENCES sn_config.mcp_servers(id) ON DELETE SET NULL;

CREATE INDEX idx_sn_tools_mcp_server_id ON sn_core.tools (mcp_server_id) WHERE mcp_server_id IS NOT NULL;

COMMENT ON COLUMN sn_core.tools.mcp_server_id IS
    'For kind=mcp tools: link to the sn_config.mcp_servers row this tool was discovered from. NULL for kind=api/cli/builtin.';

-- ============================================================
-- Ownership: new tables to smartnode (matching schema owner).
-- ============================================================
ALTER TABLE sn_core.events     OWNER TO smartnode;
ALTER TABLE sn_core.crons      OWNER TO smartnode;
ALTER TABLE sn_core.cron_runs  OWNER TO smartnode;

COMMIT;
