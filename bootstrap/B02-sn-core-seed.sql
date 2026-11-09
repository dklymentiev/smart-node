-- B02-sn-core-seed.sql -- minimal seed for a fresh Smart Node to ANSWER.
--
-- Applied by B02-persistence.sh AFTER B02-sn-core-schema.sql. Mirrors the
-- minimal data inserted by core-api migration 009 (session-first gateway)
-- plus the migration-009 public-side ALTER.
--
-- WHAT A FRESH NODE NEEDS TO ANSWER
-- ---------------------------------
-- The session-first AI gateway, to create a session and return a response,
-- needs only:
--   * sn_config.tenants            -- the default tenant (id=1); every
--                                     ai_session.tenant_id FKs to it.
--   * sn_config.model_pricing      -- per-provider/model price rows so cost
--                                     accounting does not divide-by-missing.
--   * sn_config.cli_providers      -- the claude-cli subprocess provider.
--   * sn_config.budgets            -- a default tenant budget (soft limit)
--                                     so budget checks have a row to read.
--   * sn_config.session_retention  -- a global "keep forever" rule.
--
-- NO SPECIALIST / AGENT ROW IS SEEDED -- and this is deliberate.
-- ------------------------------------------------------------------
-- Investigation: the dispatcher (brin) resolves a
-- specialist from .md files on disk via its loader, NOT from the DB. When
-- no specialist matches, core/router.py falls back to the literal id
-- "assistant" and core/executor.py run_direct() sends an empty/static
-- system prompt; the gateway still creates a session and answers. This
-- was confirmed by
-- the gateway smoke returning specialist "assistant" on a node whose only
-- seeded specialists were an unrelated node's sysadmin ones. A
-- DB-resolved agent (Mode A) is an OPT-IN via the BRIN_DEFAULT_AGENT env
-- var; without it, basic answering needs no agents/specialists rows.
-- Therefore seeding a generic specialist would be dead weight and is
-- skipped. Agents/specialists are provisioned later (panel / companions).
--
-- IDEMPOTENT: every INSERT uses ON CONFLICT DO NOTHING against the unique
-- keys defined in B02-sn-core-schema.sql. The public.messages ALTER uses
-- ADD COLUMN IF NOT EXISTS and a guarded constraint add.
--

\set ON_ERROR_STOP on

SET ROLE smartnode;

-- ----------------------------------------------------------------------
-- Default tenant (id=1). The gateway hard-references tenant_id=1, so the
-- id is pinned explicitly (OVERRIDING SYSTEM VALUE for the IDENTITY col).
-- ----------------------------------------------------------------------
INSERT INTO sn_config.tenants (id, slug, name, settings_jsonb)
OVERRIDING SYSTEM VALUE
VALUES (1, 'default', 'Default Tenant', '{}'::jsonb)
ON CONFLICT (slug) DO NOTHING;

-- Keep the IDENTITY sequence ahead of the pinned id=1 so the next
-- auto-generated tenant does not collide. No-op when already advanced.
SELECT setval(
    pg_get_serial_sequence('sn_config.tenants', 'id'),
    GREATEST((SELECT COALESCE(MAX(id), 1) FROM sn_config.tenants), 1)
);

-- ----------------------------------------------------------------------
-- First operator (owner). A super-admin user in the default tenant whose
-- email matches the identity Authum forwards as Remote-User for the first
-- operator ('admin'). Generic, no PII -- the owner's real name / contact are
-- filled later from the panel. get_principal() (core-api auth.py) rejects any
-- identity with no active users row, so EVERY user-scoped API (dashboard
-- activity/inbox, events, messages) is dead until this exists. The /setup
-- wizard is still a placeholder, so the base install seeds it here.
-- Unique on (tenant_id, email).
-- ----------------------------------------------------------------------
INSERT INTO sn_config.users (tenant_id, email, role, active)
VALUES (1, 'admin', 'super-admin', true)
ON CONFLICT (tenant_id, email) DO NOTHING;

-- The initial owner is a human actor. On an updated node, retain an existing
-- actor_id and role; on a fresh node, the legacy bootstrap user gains both.
INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation)
SELECT 'user:' || u.email, COALESCE(u.name, u.email), 'user', 'human',
       'human', 'internal'
  FROM sn_config.users u
 WHERE u.role = 'super-admin' AND u.actor_id IS NULL
ON CONFLICT (id) DO NOTHING;

UPDATE sn_config.users u
   SET actor_id = a.id
  FROM sn_core.actors a
 WHERE u.role = 'super-admin' AND u.actor_id IS NULL
   AND a.id = 'user:' || u.email;

UPDATE sn_core.actors a
   SET kind = COALESCE(a.kind, 'human'),
       relation = COALESCE(a.relation, 'internal')
  FROM sn_config.users u
 WHERE u.role = 'super-admin' AND u.actor_id = a.id
   AND (a.kind IS NULL OR a.relation IS NULL);

INSERT INTO sn_core.actor_roles (actor_id, role, source)
SELECT u.actor_id, 'super-admin', 'seed'
  FROM sn_config.users u
 WHERE u.role = 'super-admin' AND u.actor_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sn_core.actor_roles WHERE role = 'super-admin')
 ORDER BY u.id LIMIT 1
ON CONFLICT (actor_id) DO NOTHING;

-- Remote operators are node configuration. The owner registers one and issues
-- its bearer credential through /api/operators; product seed creates neither
-- an operator actor nor an active admin users row.

-- ----------------------------------------------------------------------
-- Channel catalogue. Generic capability definitions (NOT per-node config):
-- which comms channels the node knows about. An agent becomes reachable on a
-- channel via sn_config.agent_channels (bound from the panel). The voice
-- dialer reads this to list voice-enabled agents. `status` is capability
-- maturity; `enabled` is conservative on a fresh node -- only web-chat works
-- with zero external config, so the rest ship disabled and the owner turns
-- them on from the panel after supplying the token/mailbox/key (voice needs a
-- Gemini key; telegram a bot token; email a mailbox; ...). Unique on slug.
-- ----------------------------------------------------------------------
INSERT INTO sn_config.channels (slug, label, transport, mode, adapter_slug, status, enabled, description) VALUES
  ('web-chat', 'Web Chat', 'web',      'realtime',  'web-chat', 'available', true,  'Browser chat widget (brin webchat session).'),
  ('voice',    'Voice',    'voice',    'realtime',  'voice',    'available', false, 'Phone / voice call, speech in and out.'),
  ('telegram', 'Telegram', 'telegram', 'realtime',  'telegram', 'available', false, 'Telegram bot.'),
  ('email',    'Email',    'email',    'scheduled', 'email',    'available', false, 'Email inbox, polled by the secretary.'),
  ('whatsapp', 'WhatsApp', 'whatsapp', 'realtime',  'whatsapp', 'planned',   false, 'WhatsApp Business messaging.'),
  ('sms',      'SMS',      'sms',      'scheduled', 'sms',      'planned',   false, 'SMS text messaging.'),
  ('slack',    'Slack',    'slack',    'realtime',  'slack',    'available', false, 'Slack workspace bot.'),
  ('discord',  'Discord',  'discord',  'realtime',  'discord',  'planned',   false, 'Discord bot.')
ON CONFLICT (slug) DO NOTHING;

-- ----------------------------------------------------------------------
-- Model pricing. Per 1k tokens (or per image). Unique on
-- (provider, model, kind, effective_from). Generic catalogue, no
-- node-specific data. effective_from pinned so re-runs match the conflict
-- key rather than inserting a new CURRENT_DATE row each apply.
-- ----------------------------------------------------------------------
INSERT INTO sn_config.model_pricing (provider, model, kind, unit_price_usd, effective_from, notes) VALUES
  ('claude-cli', 'sonnet',                         'input_tokens',  0.003,   '2026-05-23', 'Anthropic claude sonnet input via CLI'),
  ('claude-cli', 'sonnet',                         'output_tokens', 0.015,   '2026-05-23', 'Anthropic claude sonnet output via CLI'),
  ('claude-cli', 'haiku',                          'input_tokens',  0.0008,  '2026-05-23', 'Anthropic claude haiku input via CLI'),
  ('claude-cli', 'haiku',                          'output_tokens', 0.004,   '2026-05-23', 'Anthropic claude haiku output via CLI'),
  ('openrouter', 'anthropic/claude-3-haiku',       'input_tokens',  0.00025, '2026-05-23', 'via OpenRouter'),
  ('openrouter', 'anthropic/claude-3-haiku',       'output_tokens', 0.00125, '2026-05-23', 'via OpenRouter'),
  ('openrouter', 'google/gemini-2.0-flash',        'input_tokens',  0.0001,  '2026-05-23', 'via OpenRouter'),
  ('openrouter', 'google/gemini-2.0-flash',        'output_tokens', 0.0004,  '2026-05-23', 'via OpenRouter'),
  -- Direct Google Gemini (Stage 2c, http_google backend). Track 1 of the
  -- panel needs a pricing row to render a "gemini" provider tile, and the budget
  -- pipeline needs a per-1k number to bill each session. Prices below match
  -- Google's public "Gemini 2.5 Flash" list as of 2026-09; refresh when Google
  -- moves the number. Track 2 folds this into a Gateway-owned catalogue.
  ('gemini',     'gemini-flash-latest',            'input_tokens',  0.000075,'2026-09-14', 'Direct Google Gemini API (gemini-flash-latest alias)'),
  ('gemini',     'gemini-flash-latest',            'output_tokens', 0.0003,  '2026-09-14', 'Direct Google Gemini API (gemini-flash-latest alias)'),
  ('openai',     'gpt-5.4-mini',                   'input_tokens',  0.0003,  '2026-05-23', 'OpenAI direct (placeholder for gpt-5 family pricing)'),
  ('openai',     'gpt-5.4-mini',                   'output_tokens', 0.0012,  '2026-05-23', 'OpenAI direct (placeholder)'),
  ('replicate',  'black-forest-labs/flux-pro',     'image',         0.055,   '2026-05-23', 'flux-pro per image'),
  ('replicate',  'black-forest-labs/flux-schnell', 'image',         0.003,   '2026-05-23', 'flux-schnell per image (cheap+fast)')
ON CONFLICT (provider, model, kind, effective_from) DO NOTHING;

-- ----------------------------------------------------------------------
-- CLI providers. The claude-cli subprocess wrapper. env_vars points at a
-- secret reference resolved at spawn time (B04 secrets slice owns the
-- store). Unique on slug.
-- ----------------------------------------------------------------------
INSERT INTO sn_config.cli_providers (slug, binary_path, invocation_args, supported_models, env_vars, enabled) VALUES
  ('claude-cli',
   '/usr/local/bin/claude',
   '["--session-id", "{{session_id}}", "--print"]'::jsonb,
   ARRAY['sonnet','haiku','opus'],
   -- A node's LLM credential is an OAuth token (CLAUDE_CODE_OAUTH_TOKEN; the CLI rejects
   -- it as ANTHROPIC_API_KEY) and it reaches the CLI through the gateway's process env
   -- (EnvironmentFile=/etc/smart-node/agent-runtime.env). We declare the `secret:` ref
   -- here: it is WRITTEN by the browser Connect flow (cli login -> gateway_secrets,
   -- pgcrypto) and READ back by the core, which PROJECTS it into agent-runtime.env and
   -- reloads the gateway. The DB stays the source of truth; the env file is a rendered
   -- projection, exactly like /etc/cron.d is rendered from sn_core.crons.
   '{"CLAUDE_CODE_OAUTH_TOKEN": "secret:gateway/claude-cli-token", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}'::jsonb,
   true)
ON CONFLICT (slug) DO NOTHING;

-- Existing nodes already have this row, so the INSERT above is intentionally a
-- no-op there.  Add the safety window when absent without replacing the
-- credential ref or any operator-owned provider environment.  The product handoff threshold is
-- 120k; CLI compaction near 167k is the last-resort ceiling, not the normal path.
UPDATE sn_config.cli_providers
   SET env_vars = env_vars || '{"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}'::jsonb
 WHERE slug = 'claude-cli'
   AND NOT (env_vars ? 'CLAUDE_CODE_AUTO_COMPACT_WINDOW');

-- Flint runs a provider model through its own stream-json process. Its key
-- stays in gateway_secrets and is sent to the agent-owned wrapper over a
-- private stdin frame at spawn; it is never projected to a shared env file.
INSERT INTO sn_config.cli_providers
    (slug, binary_path, invocation_args, supported_models, env_vars, enabled)
VALUES
  ('flint-cli', '/usr/local/bin/flint',
   '["--stdio", "--session-id", "{{session_id}}"]'::jsonb,
   ARRAY[]::text[],
   '{"FLINT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "secret:gateway/flint-key"}'::jsonb,
   true)
ON CONFLICT (slug) DO NOTHING;

-- Flint reads its agent's identity from FLINT.md (from 1.16.0, the release the
-- gateway requires); CLAUDE.md stays Claude Code's.
UPDATE sn_config.cli_providers SET instructions_file = 'FLINT.md'
 WHERE slug = 'flint-cli' AND instructions_file = 'CLAUDE.md';

-- ----------------------------------------------------------------------
-- Provider display names. The slug stays the routing key everywhere; the
-- panel's AI Gateway card shows the display name on top with the slug as a
-- subtitle. A provider slug without a row here keeps falling back to the slug,
-- so a newly plugged backend is visible on day one even before this seed is
-- updated.
-- ----------------------------------------------------------------------
INSERT INTO sn_config.providers (slug, display_name) VALUES
  ('claude-cli', 'Anthropic'),
  ('flint-cli',  'Flint'),
  ('gemini',     'Google'),
  ('openai',     'OpenAI'),
  ('openrouter', 'OpenRouter'),
  ('replicate',  'Replicate')
ON CONFLICT (slug) DO NOTHING;

-- ----------------------------------------------------------------------
-- Chat models offered in the panel. This table was NEVER seeded, so a fresh
-- node showed an empty model list and the panel fell back to a hard-coded
-- openrouter model -- which needs a cloud key a fresh node does not have. The
-- chat page then 502'd and sat in "reconnecting" forever. The seed below gives a fresh node a working default.
--
-- The DEFAULT is claude-cli, i.e. the node's own agent: it has the agent's
-- persona and tools, needs no cloud key, and costs nothing extra. Cloud models
-- (openrouter/...) are a deliberate add-on once a key is configured -- not the
-- default a node is born with.
--
-- provider is the runtime, so an agent on the first row is 'claude-cli/sonnet';
-- vendor is who makes the model.
-- ----------------------------------------------------------------------
INSERT INTO sn_config.models (provider, model, label, modality, enabled, is_default, sort_order, vendor) VALUES
  ('claude-cli', 'sonnet', 'Claude Sonnet (agent)', 'chat', true, true,  10, 'anthropic'),
  ('claude-cli', 'haiku',  'Claude Haiku (agent)',  'chat', true, false, 20, 'anthropic'),
  ('claude-cli', 'opus',   'Claude Opus (agent)',   'chat', true, false, 30, 'anthropic'),
  -- Direct Google Gemini, runtime gemini: an agent on it is
  -- 'gemini/gemini-flash-latest'. Needs gateway_secrets
  -- ref 'gateway/gemini-key' (AI Gateway settings) before a call can complete.
  ('gemini',     'gemini-flash-latest', 'Gemini Flash (direct)', 'chat', true, false, 40, 'google')
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------------
-- Default tenant budget. Soft limit (hard_limit=false) so a fresh node
-- never rejects requests on budget grounds; gives budget checks a row to
-- read. period_reset_at seeded to the first of next month (UTC). Unique on
-- (scope_type, scope_id, period).
-- ----------------------------------------------------------------------
INSERT INTO sn_config.budgets
    (scope_type, scope_id, period, limit_usd, current_spend_usd, period_reset_at, hard_limit, enabled)
VALUES
    ('tenant', '1', 'monthly', 100.00, 0,
     date_trunc('month', now()) + interval '1 month', false, true)
ON CONFLICT (scope_type, scope_id, period) DO NOTHING;

-- Registry entries are stable codes. Re-running B02 adds missing definitions
-- without changing owner-edited profiles on an existing node.
INSERT INTO sn_config.limit_defs (code, label, unit, control_point, description) VALUES
    ('spend_usd', 'Agent spending', 'USD', 'gateway', 'Spending by one agent'),
    ('node_spend_usd', 'Node spending', 'USD', 'gateway', 'Spending across the node'),
    ('wakes_unattended', 'Unattended wakes', 'wakes', 'runner', 'Automatic starts without a person'),
    ('turn_seconds', 'Turn length', 'seconds', 'gateway', 'Maximum wall-clock length of one turn')
ON CONFLICT (code) DO NOTHING;

-- Copy every existing money setting before the gateway stops reading budget
-- limits. An existing agent row becomes its override; the tenant row becomes
-- the node limit. The old meter stays intact, including current spend.
INSERT INTO sn_config.limit_profiles
    (code, scope_type, scope_id, period, value, action, warn_at_pct, notify)
SELECT CASE WHEN b.scope_type = 'tenant' THEN 'node_spend_usd' ELSE 'spend_usd' END,
       CASE b.scope_type WHEN 'tenant' THEN 'node'
                         WHEN 'agent' THEN 'agent' ELSE 'metadata' END,
       CASE b.scope_type WHEN 'tenant' THEN '*'
                         WHEN 'agent' THEN a.slug ELSE b.scope_id END,
       CASE b.period WHEN 'daily' THEN 'day' WHEN 'weekly' THEN 'week' ELSE 'month' END,
       b.limit_usd, CASE WHEN b.hard_limit THEN 'pause' ELSE 'warn' END,
       b.warn_at_pct, true
  FROM sn_config.budgets b
  LEFT JOIN sn_config.agents a ON a.id::text = b.scope_id
 WHERE b.enabled AND b.scope_type IN ('tenant', 'agent', 'session_metadata_key') AND b.limit_usd > 0
   AND (b.scope_type IN ('tenant', 'session_metadata_key') OR a.slug IS NOT NULL)
ON CONFLICT (code, scope_type, scope_id, period) DO NOTHING;

INSERT INTO sn_config.limit_profiles
    (code, scope_type, scope_id, period, value, action, warn_at_pct, notify) VALUES
    ('wakes_unattended', 'default', '*', 'hour',
        COALESCE(NULLIF(current_setting('smart_node.legacy_wake_hour', true), '')::numeric, 30),
        'pause', 80, true),
    ('wakes_unattended', 'default', '*', 'day',
        COALESCE(NULLIF(current_setting('smart_node.legacy_wake_day', true), '')::numeric, 150),
        'pause', 80, true),
    ('turn_seconds', 'default', '*', 'turn',
        COALESCE(NULLIF(current_setting('smart_node.legacy_turn_seconds', true), '')::numeric, 600),
        'pause', 80, true)
ON CONFLICT (code, scope_type, scope_id, period) DO NOTHING;
INSERT INTO sn_config.node_control (id) VALUES (true) ON CONFLICT (id) DO NOTHING;

-- ----------------------------------------------------------------------
-- Session retention. One global "keep forever" rule (retention_days NULL,
-- archive_to NULL). Unique on (scope_type, scope_id); scope_id is NULL for
-- the global rule, so the unique index treats NULLs as distinct -- guard
-- the insert with NOT EXISTS to stay idempotent.
-- ----------------------------------------------------------------------
INSERT INTO sn_config.session_retention (scope_type, scope_id, retention_days, archive_to, enabled)
SELECT 'global', NULL, NULL, NULL, true
WHERE NOT EXISTS (
    SELECT 1 FROM sn_config.session_retention
    WHERE scope_type = 'global' AND scope_id IS NULL
);

-- Context handoff.  120k leaves room for the handoff-generating request before
-- Claude CLI's 200k safety window compacts at roughly 167k.  Agent-specific
-- rows override this node default and are created through core-api/the panel.
INSERT INTO sn_config.session_context_limits
    (scope_type, scope_id, threshold_tokens, enabled)
VALUES ('global', NULL, 120000, true)
ON CONFLICT (scope_type, scope_id) DO NOTHING;

RESET ROLE;

-- ======================================================================
-- public.messages.ai_session_id  (migration 009 public-side change).
-- Runs as the cluster superuser (postgres) -- public.messages is owned by
-- the kernel schema (B02-schema.sql, also owned by smartnode, but the FK
-- target sn_core.ai_sessions exists now so the constraint can be added).
-- MUST run AFTER B02-sn-core-schema.sql created sn_core.ai_sessions.
--
-- This is the ONLY change this seed makes to the public/kernel schema and
-- it is purely additive: it does NOT recreate public.messages, only adds a
-- nullable column + FK. Idempotent: ADD COLUMN IF NOT EXISTS + a guarded
-- constraint add.
-- ======================================================================
ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS ai_session_id uuid;

DO $msgfk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'messages_ai_session_id_fkey'
    ) THEN
        ALTER TABLE public.messages
            ADD CONSTRAINT messages_ai_session_id_fkey
            FOREIGN KEY (ai_session_id) REFERENCES sn_core.ai_sessions(id);
    END IF;
END
$msgfk$;

CREATE INDEX IF NOT EXISTS idx_messages_ai_session
    ON public.messages (ai_session_id) WHERE ai_session_id IS NOT NULL;

-- ======================================================================
-- One event journal: sn_core.events.
--
-- This node had two. bootstrap/lib.sh emitted `INSERT INTO events`
-- unqualified, which search_path ("$user", public) resolves to
-- public.events, while core-api's events_emit.py writes sn_core.events and
-- sn_core.v_actor_events reads it. So every install-time event --
-- stage.done, companion.installed, node.validated -- landed in a table no
-- reader on the node ever opens, and the identity model covered half the
-- node's history.
--
-- lib.sh now writes sn_core.events. This block carries whatever the old
-- journal already holds across, ensures the actors those rows need, and
-- then DROPS the empty shell. No row is lost -- every one of them is in
-- sn_core.events first, and the drop is the last statement in the block, so
-- a failure anywhere above it takes the whole transaction with it and the
-- table is still there to try again.
--
-- The owner asked for the drop rather than a rename aside (2026-09-09): one
-- journal on the node and no second name for anyone to find later and
-- wonder about. Nothing depends on the table -- checked on this node, no
-- view and no incoming foreign key -- so the DROP is plain, not CASCADE:
-- if something ever does depend on it, the install should stop and say so.
--
-- Idempotent: to_regclass returns NULL once the table is gone and the block
-- returns, so a second run is a no-op. The carry-across is NOT EXISTS-guarded
-- on (actor, event_type, ts) for the case where it ran and the drop did not.
-- ======================================================================
-- The installer's own actor. It used to be seeded into public.actors by
-- B02-schema.sql, which runs before sn_core exists; now that bootstrap writes
-- sn_core.events, the FK target has to be here. ON CONFLICT: an updated node
-- already carries it.
INSERT INTO sn_core.actors (id, display_name, type, layer)
VALUES ('system:installer', 'Smart Node Installer', 'installer', 'system')
ON CONFLICT (id) DO NOTHING;

DO $events_consolidate$
DECLARE
    moved integer := 0;
BEGIN
    IF to_regclass('public.events') IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO sn_core.actors (id, display_name, type, layer)
    SELECT DISTINCT e.actor, e.actor, split_part(e.actor, ':', 1),
           CASE split_part(e.actor, ':', 1)
               WHEN 'user'         THEN 'human'
               WHEN 'human'        THEN 'human'
               WHEN 'agent'        THEN 'specialist'
               WHEN 'specialist'   THEN 'specialist'
               WHEN 'orchestrator' THEN 'orchestrator'
               WHEN 'guard'        THEN 'guard'
               ELSE 'system'
           END
      FROM public.events e
     WHERE NOT EXISTS (SELECT 1 FROM sn_core.actors a WHERE a.id = e.actor)
    ON CONFLICT (id) DO NOTHING;

    INSERT INTO sn_core.events (actor, event_type, entity_type, entity_id, payload, ts)
    SELECT e.actor, e.event_type, e.entity_type, e.entity_id, e.payload, e.ts
      FROM public.events e
     WHERE NOT EXISTS (
           SELECT 1 FROM sn_core.events c
            WHERE c.actor = e.actor AND c.event_type = e.event_type AND c.ts = e.ts);
    GET DIAGNOSTICS moved = ROW_COUNT;
    IF moved > 0 THEN
        RAISE NOTICE 'events: carried % row(s) from public.events into sn_core.events', moved;
    END IF;

    DROP TABLE public.events;
    RAISE NOTICE 'events: public.events dropped -- sn_core.events is the journal';
END
$events_consolidate$;

-- ======================================================================
-- public.messages tenant column. Kernel-schema change core-api expects
-- that B02-schema.sql does not create; messages are unaffected by the
-- events consolidation above.
-- ======================================================================
ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS tenant_id integer NOT NULL DEFAULT 1;

DO $msgtenantfk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname  = 'messages_tenant_id_fkey'
           AND conrelid = 'public.messages'::regclass
    ) THEN
        ALTER TABLE public.messages ADD CONSTRAINT messages_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id);
    END IF;
END
$msgtenantfk$;

CREATE INDEX IF NOT EXISTS idx_messages_tenant   ON public.messages (tenant_id);

-- ======================================================================
-- sn_config.agent_channels.history_before -- the binding watermark.
--
-- The schema file carries this column inside CREATE TABLE, which reaches a
-- FRESH node only: `CREATE TABLE IF NOT EXISTS` is a no-op on a node that
-- already has the table, so without the ALTER below an updated node would keep
-- the old shape and the poller's watermark lookup would fail on every pass.
-- That is the whole reason this additive section exists.
--
-- Backfill is `created_at`, not now(): for a binding that already exists the
-- honest watermark is the moment it was bound. Stamping now() would silently
-- declare everything currently unanswered to be history.
-- ======================================================================
ALTER TABLE sn_config.agent_channels
    ADD COLUMN IF NOT EXISTS history_before timestamp with time zone;

UPDATE sn_config.agent_channels
   SET history_before = created_at
 WHERE history_before IS NULL;

ALTER TABLE sn_config.agent_channels
    ALTER COLUMN history_before SET DEFAULT now();
ALTER TABLE sn_config.agent_channels
    ALTER COLUMN history_before SET NOT NULL;

-- ======================================================================
-- sn_config.projects / project_members -- project membership registry,
--
-- CREATE TABLE IF NOT EXISTS in the schema file reaches fresh nodes; these
-- reach a node that already has the schema. Whole tables rather than columns,
-- so a plain CREATE ... IF NOT EXISTS is enough and the guarded PK/FK block in
-- the schema file adds the constraints on the same run.
--
-- Not seeded with any project. A node names its own; an example row here would
-- become a directory and a unix group on every node in the fleet.
-- ======================================================================
CREATE TABLE IF NOT EXISTS sn_config.projects (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    unix_group text NOT NULL,
    fs_path text NOT NULL,
    workspace_id integer,
    planner_project text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    tenant_id integer DEFAULT 1 NOT NULL,
    CONSTRAINT projects_slug_chk CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$')
);

CREATE TABLE IF NOT EXISTS sn_config.project_members (
    project_id integer NOT NULL,
    member_kind text NOT NULL,
    member_id text NOT NULL,
    role text DEFAULT 'member' NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_members_kind_chk CHECK (member_kind IN ('user', 'agent')),
    CONSTRAINT project_members_role_chk CHECK (role IN ('lead', 'member', 'viewer'))
);

CREATE INDEX IF NOT EXISTS project_members_member_idx
    ON sn_config.project_members (member_kind, member_id);

COMMENT ON TABLE sn_config.projects IS
    'a project is a registry row -- slug is the one key across memory, documents, planner and /srv/projects/<slug>.';
-- Guarded: `teams` comes from the schema file, but this seed also runs against
-- nodes older than it, and an unguarded COMMENT on a missing table aborts the
-- whole file -- taking the projects tables above with it.
DO $teams$
BEGIN
    IF to_regclass('sn_config.teams') IS NOT NULL THEN
        COMMENT ON TABLE sn_config.teams IS
            'SUPERSEDED by sn_config.projects. Never implemented, 0 rows on every node. Do not extend.';
    END IF;
END
$teams$;

-- ======================================================================
-- message_state.status -- re-assert the allowed set.
--
-- The set is declared inline in the two CREATE TABLE statements, so it too
-- reaches fresh nodes only, and it has already drifted once in the field:
-- sn_core gained 'blocked' when the contact gate landed, public did not, and
-- any node installed before that change still carries the four-value CHECK
-- while dispatch.py writes 'blocked' to it. That write fails with a constraint
-- violation on exactly the nodes nobody rebuilt.
--
-- Rather than adding a second special case for 'history', this drops and
-- re-adds the constraint to the CURRENT definition on every B02 run. It is the
-- one place the set is enforced, it repairs the existing 'blocked' drift as a
-- side effect, and a future value needs editing here and in the schema file
-- and nowhere else.
--
-- Guarded on the table existing (public.message_state comes from the kernel
-- schema, sn_core.message_state from the sn_core one) and applied by regclass
-- so it is a no-op rather than an error on a node that has only one of them.
-- ======================================================================
DO $msgchk$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['public.message_state', 'sn_core.message_state'] LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format(
                'ALTER TABLE %s DROP CONSTRAINT IF EXISTS message_state_status_chk', t);
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT message_state_status_chk '
                'CHECK (status = ANY (ARRAY[''new'',''routed'',''processed'','
                '''failed'',''blocked'',''history'',''dry_run'','
                '''awaiting_reply'']))', t);
        END IF;
    END LOOP;
END
$msgchk$;
