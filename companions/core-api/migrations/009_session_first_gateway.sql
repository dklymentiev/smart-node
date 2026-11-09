-- Migration 009: Session-first Gateway (session-first gateway schema)
-- 
-- Adds the postgres foundation for session-first AI gateway:
--   * sn_core.ai_sessions             -- single source of truth for all LLM sessions
--   * sn_core.ai_session_messages     -- append-only message log per session
--   * public.messages.ai_session_id   -- FK linking adapter messages to AI sessions
--   * sn_config.model_pricing         -- per-token / per-image / per-second pricing for cost calc
--   * sn_config.budgets               -- per-tenant/agent/scope budget buckets with hard/soft limits
--   * sn_config.cli_providers         -- registry for cli-style providers (claude-cli, codex-cli, ...)
--   * sn_config.session_retention     -- configurable cleanup rules per scope
--
-- Seed data:
--   * 6 model_pricing rows (claude-cli/sonnet, claude-cli/haiku, openrouter/anthropic/claude-3-haiku,
--     openrouter/google/gemini-2.0-flash, openai/gpt-5.4-mini, replicate/black-forest-labs/flux-pro)
--   * Default tenant budget: tenant_id=1, $100/mo soft (operator-configurable)
--   * Global retention default: forever (retention_days=NULL)
--   * cli_providers row: claude-cli binary
--
-- Reversible: see ROLLBACK section at bottom (commented out).

BEGIN;

-- ============================================================================
-- sn_core.ai_sessions
-- ============================================================================

CREATE TABLE sn_core.ai_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           integer NOT NULL REFERENCES sn_config.tenants(id),
    type                text NOT NULL,
    model               text NOT NULL,
    provider            text NOT NULL,
    is_opaque           boolean NOT NULL DEFAULT false,
    agent_id            integer REFERENCES sn_config.agents(id),
    status              text NOT NULL DEFAULT 'active',
    system_prompt       text,
    parent_session_id   uuid REFERENCES sn_core.ai_sessions(id),
    metadata            jsonb NOT NULL DEFAULT '{}',
    usage               jsonb NOT NULL DEFAULT '{}',
    budget_key          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_activity_at    timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    error               text,
    CHECK (type IN ('chat','image_gen','video_gen','tts','stt','embed')),
    CHECK (status IN ('active','streaming','idle_released','completed','failed','cancelled'))
);

COMMENT ON TABLE sn_core.ai_sessions IS
  'Single source of truth for AI gateway sessions. Provider-agnostic. Persisted forever (subject to session_retention rules).';
COMMENT ON COLUMN sn_core.ai_sessions.is_opaque IS
  'true when backend = CLI-style (claude-cli/codex-cli/...); documents audit gap to UI/reports.';
COMMENT ON COLUMN sn_core.ai_sessions.agent_id IS
  'NULL for ad-hoc Mode B sessions; set when created by agent reference (Mode A).';

CREATE INDEX idx_sessions_tenant_status ON sn_core.ai_sessions(tenant_id, status);
CREATE INDEX idx_sessions_parent ON sn_core.ai_sessions(parent_session_id) WHERE parent_session_id IS NOT NULL;
CREATE INDEX idx_sessions_activity ON sn_core.ai_sessions(last_activity_at) WHERE status IN ('active', 'streaming');
CREATE INDEX idx_sessions_budget ON sn_core.ai_sessions(budget_key, created_at) WHERE budget_key IS NOT NULL;
CREATE INDEX idx_sessions_agent ON sn_core.ai_sessions(agent_id) WHERE agent_id IS NOT NULL;
CREATE INDEX idx_sessions_provider ON sn_core.ai_sessions(provider, status);


-- ============================================================================
-- sn_core.ai_session_messages
-- ============================================================================

CREATE TABLE sn_core.ai_session_messages (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id          uuid NOT NULL REFERENCES sn_core.ai_sessions(id) ON DELETE CASCADE,
    seq                 integer NOT NULL,
    role                text NOT NULL,
    content_type        text NOT NULL,
    content             text,
    artifact_uri        text,
    tool_calls          jsonb,
    tool_call_id        text,
    is_partial          boolean NOT NULL DEFAULT false,
    is_injected         boolean NOT NULL DEFAULT false,
    usage               jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    UNIQUE (session_id, seq),
    CHECK (role IN ('user','assistant','system','operator','tool'))
);

COMMENT ON TABLE sn_core.ai_session_messages IS
  'Append-only message log per AI session. Includes both upstream-bound and operator-injected entries.';
COMMENT ON COLUMN sn_core.ai_session_messages.is_partial IS
  'true during streaming; flipped false on done event.';
COMMENT ON COLUMN sn_core.ai_session_messages.is_injected IS
  'true when message came from /inject endpoint (operator override, bypasses pre-send hooks).';

CREATE INDEX idx_msgs_session_seq ON sn_core.ai_session_messages(session_id, seq);
CREATE INDEX idx_msgs_role ON sn_core.ai_session_messages(session_id, role);


-- ============================================================================
-- public.messages.ai_session_id FK
-- ============================================================================

ALTER TABLE public.messages
    ADD COLUMN ai_session_id uuid REFERENCES sn_core.ai_sessions(id);

CREATE INDEX idx_messages_ai_session ON public.messages(ai_session_id) WHERE ai_session_id IS NOT NULL;

COMMENT ON COLUMN public.messages.ai_session_id IS
  'Links an adapter-ingested message to the AI session that processed it.';


-- ============================================================================
-- sn_config.model_pricing
-- ============================================================================

CREATE TABLE sn_config.model_pricing (
    id              serial PRIMARY KEY,
    provider        text NOT NULL,
    model           text NOT NULL,
    kind            text NOT NULL,
    unit_price_usd  numeric NOT NULL,
    effective_from  date NOT NULL DEFAULT CURRENT_DATE,
    notes           text,
    UNIQUE (provider, model, kind, effective_from),
    CHECK (kind IN ('input_tokens','output_tokens','image','video_second','audio_second','embedding'))
);

COMMENT ON TABLE sn_config.model_pricing IS
  'Per-provider/model pricing for cost calc. Price per 1k tokens, or per image, or per second.';

-- Seed: rough pricing snapshot 2026-05. Operator updates via panel as upstream changes prices.
INSERT INTO sn_config.model_pricing (provider, model, kind, unit_price_usd, notes) VALUES
    ('claude-cli',  'sonnet',                           'input_tokens',  0.003,  'Anthropic claude-3.5-sonnet input via CLI'),
    ('claude-cli',  'sonnet',                           'output_tokens', 0.015,  'Anthropic claude-3.5-sonnet output via CLI'),
    ('claude-cli',  'haiku',                            'input_tokens',  0.0008, 'Anthropic claude-3.5-haiku input via CLI'),
    ('claude-cli',  'haiku',                            'output_tokens', 0.004,  'Anthropic claude-3.5-haiku output via CLI'),
    ('openrouter',  'anthropic/claude-3-haiku',         'input_tokens',  0.00025,'via OpenRouter'),
    ('openrouter',  'anthropic/claude-3-haiku',         'output_tokens', 0.00125,'via OpenRouter'),
    ('openrouter',  'google/gemini-2.0-flash',          'input_tokens',  0.0001, 'via OpenRouter'),
    ('openrouter',  'google/gemini-2.0-flash',          'output_tokens', 0.0004, 'via OpenRouter'),
    ('openai',      'gpt-5.4-mini',                     'input_tokens',  0.0003, 'OpenAI direct (placeholder for gpt-5 family pricing)'),
    ('openai',      'gpt-5.4-mini',                     'output_tokens', 0.0012, 'OpenAI direct (placeholder)'),
    ('replicate',   'black-forest-labs/flux-pro',       'image',         0.055,  'flux-pro per image'),
    ('replicate',   'black-forest-labs/flux-schnell',   'image',         0.003,  'flux-schnell per image (cheap+fast)');


-- ============================================================================
-- sn_config.budgets
-- ============================================================================

CREATE TABLE sn_config.budgets (
    id                 serial PRIMARY KEY,
    scope_type         text NOT NULL,
    scope_id           text NOT NULL,
    period             text NOT NULL,
    limit_usd          numeric NOT NULL,
    current_spend_usd  numeric NOT NULL DEFAULT 0,
    period_reset_at    timestamptz NOT NULL,
    hard_limit         boolean NOT NULL DEFAULT false,
    enabled            boolean NOT NULL DEFAULT true,
    UNIQUE (scope_type, scope_id, period),
    CHECK (scope_type IN ('tenant','agent','session_metadata_key')),
    CHECK (period IN ('daily','weekly','monthly'))
);

COMMENT ON TABLE sn_config.budgets IS
  'Configurable cost limits per scope. hard_limit=true rejects requests (429) when exceeded; false logs warning only.';

-- Seed: default tenant_id=1 budget. $100/mo soft (operator-configurable from panel).
INSERT INTO sn_config.budgets (scope_type, scope_id, period, limit_usd, period_reset_at, hard_limit, enabled)
VALUES ('tenant', '1', 'monthly', 100.00, date_trunc('month', now()) + interval '1 month', false, true);


-- ============================================================================
-- sn_config.cli_providers
-- ============================================================================

CREATE TABLE sn_config.cli_providers (
    id                  serial PRIMARY KEY,
    slug                text NOT NULL UNIQUE,
    binary_path         text NOT NULL,
    invocation_args     jsonb NOT NULL DEFAULT '[]',
    supported_models    text[] NOT NULL,
    env_vars            jsonb NOT NULL DEFAULT '{}',
    enabled             boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE sn_config.cli_providers IS
  'Registry of CLI-style LLM providers (subprocess wrappers). Adding a new one = INSERT row + register backend class in gateway.';
COMMENT ON COLUMN sn_config.cli_providers.invocation_args IS
  'JSON array of CLI args with {{template}} placeholders. e.g. ["--session-id","{{session_id}}","--print"].';
COMMENT ON COLUMN sn_config.cli_providers.env_vars IS
  'JSON object: env var name -> secret reference. Resolved at subprocess spawn from secrets store.';

-- Seed: claude-cli row. binary_path / invocation_args operator-tunable.
INSERT INTO sn_config.cli_providers (slug, binary_path, invocation_args, supported_models, env_vars, enabled) VALUES
    ('claude-cli',
     '/usr/local/bin/claude',
     '["--session-id", "{{session_id}}", "--print"]'::jsonb,
     ARRAY['sonnet','haiku','opus'],
     -- No provider env by default. A node's LLM credential is an OAuth token
     -- (CLAUDE_CODE_OAUTH_TOKEN), never an ANTHROPIC_API_KEY, and it reaches the CLI
     -- through the gateway's process env (EnvironmentFile=/etc/smart-node/agent-runtime.env).
     -- Seeding a `secret:` ref that nothing ever writes made the gateway raise on every
     -- chat once it started resolving secrets -- see migration 021.
     '{}'::jsonb,
     true);


-- ============================================================================
-- sn_config.session_retention
-- ============================================================================

CREATE TABLE sn_config.session_retention (
    id                  serial PRIMARY KEY,
    scope_type          text NOT NULL,
    scope_id            text,
    retention_days      integer,
    archive_to          text,
    enabled             boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_type, scope_id),
    CHECK (scope_type IN ('global','tenant','provider','session_type')),
    CHECK (retention_days IS NULL OR retention_days > 0)
);

COMMENT ON TABLE sn_config.session_retention IS
  'Configurable cleanup rules per scope. retention_days NULL = forever. archive_to NULL = hard delete on expiry.';

-- Seed: global default = forever (no cleanup until operator sets a rule).
INSERT INTO sn_config.session_retention (scope_type, scope_id, retention_days, archive_to, enabled)
VALUES ('global', NULL, NULL, NULL, true);


COMMIT;

-- ============================================================================
-- Verification queries (run after COMMIT):
-- ============================================================================
-- \dt sn_core.*
-- \dt sn_config.model_pricing
-- \dt sn_config.budgets
-- \dt sn_config.cli_providers
-- \dt sn_config.session_retention
-- \d+ public.messages
-- SELECT count(*) FROM sn_config.model_pricing;   -- expect >= 12
-- SELECT * FROM sn_config.budgets;                 -- expect 1 row (tenant_id=1)
-- SELECT * FROM sn_config.cli_providers;           -- expect 1 row (claude-cli)
-- SELECT * FROM sn_config.session_retention;       -- expect 1 row (global, NULL retention)
--
-- Smoke insert (manual test):
--   INSERT INTO sn_core.ai_sessions (tenant_id, type, model, provider)
--     VALUES (1, 'chat', 'openrouter/anthropic/claude-3-haiku', 'openrouter')
--     RETURNING id;
--   INSERT INTO sn_core.ai_session_messages (session_id, seq, role, content_type, content)
--     VALUES ('<id>', 0, 'user', 'text', 'hello');
--
-- ============================================================================
-- ROLLBACK (manual, NOT auto):
-- ============================================================================
--   BEGIN;
--   ALTER TABLE public.messages DROP COLUMN ai_session_id;
--   DROP TABLE sn_core.ai_session_messages;
--   DROP TABLE sn_core.ai_sessions;
--   DROP TABLE sn_config.session_retention;
--   DROP TABLE sn_config.cli_providers;
--   DROP TABLE sn_config.budgets;
--   DROP TABLE sn_config.model_pricing;
--   COMMIT;
