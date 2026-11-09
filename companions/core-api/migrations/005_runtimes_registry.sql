-- Migration 005: sn_config.runtimes -- registry of agent runtimes (engines).
--
-- Parallel to sn_config.integrations, but a different concept:
--   integration = what an agent CALLS (memory, documents, browser, ...)
--   runtime     = the engine that EXECUTES an agent (Claude Code, Codex, ...)
--
-- An agent (persona/config) is bound to one runtime and may call many
-- integrations.
--

BEGIN;

CREATE TABLE sn_config.runtimes (
    id              BIGSERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    label           TEXT NOT NULL,
    purpose         TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'other'
                        CHECK (kind IN ('claude-code', 'codex', 'gpt', 'gemini', 'other')),
    status          TEXT NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available', 'not-installed', 'broken')),
    install_uri     TEXT NOT NULL DEFAULT '',
    install_path    TEXT NOT NULL DEFAULT '',
    companion_slug  TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    tenant_id       INT REFERENCES sn_config.tenants(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at     TIMESTAMPTZ
);

CREATE INDEX runtimes_status_idx         ON sn_config.runtimes(status);
CREATE INDEX runtimes_kind_idx           ON sn_config.runtimes(kind);
CREATE INDEX runtimes_companion_slug_idx ON sn_config.runtimes(companion_slug);

COMMENT ON TABLE sn_config.runtimes IS
    'Registry of installed agent runtimes (Claude Code, Codex, ...). One row per runtime engine on this node.';

COMMENT ON COLUMN sn_config.runtimes.purpose IS
    'Functional role (Reasoning, Coding, ...). Free-text like integrations.purpose.';
COMMENT ON COLUMN sn_config.runtimes.kind IS
    'Engine class: claude-code, codex, gpt, gemini, other.';
COMMENT ON COLUMN sn_config.runtimes.install_path IS
    'Local install location (binary path or install dir). Empty for SaaS-only runtimes.';

-- Backfill: Claude Code is already installed via the agent-runtimes companion.
INSERT INTO sn_config.runtimes
    (slug, label, purpose, description, kind, status, install_uri, install_path, companion_slug, metadata)
VALUES
    ('claude-code',
     'Claude Code',
     'Reasoning + tool use',
     'Anthropic Claude Code CLI agent runtime. Installed by the agent-runtimes companion. The CLI binary that hosts the LLM session and drives tools.',
     'claude-code',
     'available',
     'https://claude.com/claude-code',
     '/usr/local/bin/claude',
     'agent-runtimes',
     '{"installed_by": "agent-runtimes@0.2.0"}'::jsonb);

COMMIT;
