-- B02-sn-core-schema.sql -- Smart Node sn_core + sn_config schema layer.
--
-- Applied by B02-persistence.sh into the `smartnode` core database, AFTER
-- B02-schema.sql (which owns the public/kernel schema: actors, events,
-- messages, message_state and the trigger functions events_immutable(),
-- messages_immutable(), messages_seed_state(), message_state_guard()).
--
-- WHY THIS FILE EXISTS
-- --------------------
-- A fresh golden-image node ships only the public kernel schema. The
-- session-first AI gateway and the config/registry plane need
-- sn_core.* (ai_sessions, ai_session_messages, crons, cron_runs, tools,
-- workflows, desks, agent_runtime, node_services, node_service_sweeps, and
-- the sn_core mirror of actors/events/messages/message_state) plus the whole
-- sn_config registry (tenants, agents, specialists, tools, models,
-- tenant_profile, projection_ledger, merge_ownership, ...). This file is the
-- BAKED END STATE of core-api migrations 001..N -- a single idempotent schema
-- layer applied at the persistence stage. There is NO migration runner for
-- fresh installs.
--
-- Source of truth: `pg_dump --schema-only -n sn_core -n sn_config` from the
-- live 0.16.0 node (reference-node, smartnode database), curated:
--   * pg_dump cruft stripped (no \restrict, no SET statements, no banner,
--     no OWNER TO / GRANT / documentation-only COMMENTs).
--   * ownership unified to the `smartnode` role via SET ROLE (same convention
--     as B02-schema.sql), so no per-object OWNER/GRANT is needed.
--
-- sn_core actors/events/messages/message_state
-- --------------------------------------------
-- Unlike the pre-0.16 baked schema (which deliberately had NO sn_core.events,
-- per the 007/008a lesson), the 0.16.0 core-api reads/writes the *sn_core*
-- copies of these tables directly: events_emit.py -> sn_core.actors/events;
-- routes/events.py, routes/system.py, lib/projector.py -> sn_core.events;
-- routes/messages.py -> sn_core.messages / sn_core.message_state. They are
-- first-class and included here. The four immutability/seed triggers on them
-- reuse the public trigger functions created by B02-schema.sql.
--
-- IDEMPOTENT: safe to re-apply. Schemas/tables/sequences/indexes use
-- IF NOT EXISTS; identity + CHECK + DEFAULT are inline in CREATE TABLE (so a
-- re-apply skips them); PK/UNIQUE/FK are gated on pg_constraint in DO blocks
-- (keyed on table+conname to survive cross-schema name collisions such as
-- sn_config.tools_pkey vs sn_core.tools_pkey); triggers use CREATE OR REPLACE.
--
-- Rebaked from the core-api 0.16.0 schema.

\set ON_ERROR_STOP on

-- Objects are owned by the core application role, not the postgres superuser
-- that runs the apply (same convention as B02-schema.sql).
SET ROLE smartnode;

-- ======================================================================
-- Schemas
-- ======================================================================
CREATE SCHEMA IF NOT EXISTS sn_config;
CREATE SCHEMA IF NOT EXISTS sn_core;


-- ======================================================================
-- Tables (identity / CHECK / DEFAULT inline; PK/UNIQUE/FK added below)
-- ======================================================================

-- history_before -- the binding watermark. Everything that arrived at
-- this channel instance AT OR BEFORE this moment is HISTORY: ingested as
-- evidence, never routed to an agent for a reply. Only what arrives after it
-- is an inbox. Connecting a real business mailbox with months of accumulated
-- mail once replayed the whole backlog as live inbound, and the agent answered
-- it -- one reply carrying the node's internal status reached an outside party.
CREATE TABLE IF NOT EXISTS sn_config.agent_channels (
    agent_id integer NOT NULL,
    channel_id integer NOT NULL,
    instance_ref text,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    history_before timestamp with time zone DEFAULT now() NOT NULL
);

-- projects / project_members -- project membership registry,
--
-- A project is a REGISTRY ROW, not a directory convention: the slug is the one
-- key across memory (project-<slug>), documents (<slug>/), the node planner's
-- project of the same name and the filesystem (/srv/projects/<slug>), and a
-- convention holds none of that mapping. It is also what the projector and the
-- drift detector compare the disk against -- without a row, a directory with no
-- owner is indistinguishable from a correct one.
--
-- `workspace_id` is deliberately present and deliberately NULL: workspaces are
-- a later grouping, and the flat layout was chosen because adding them costs
-- one column while un-nesting would cost moving every file.
--
-- `planner_project` references the node planner's project rather than
-- duplicating it. Duplicating it breaks "one door per class" on day one.
--
-- This SUPERSEDES sn_config.teams + team_members (0 rows on every node, never
-- implemented). sn_config.departments stays -- agents.department_id references
-- it and it is already in the agent payload.
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
    -- a project marked `restricted` drops the grp-role-developer entry
    -- from its default POSIX ACL and keeps only grp-prj-<slug>. The installer
    -- consults this column when applying ACLs on /work/projects/<slug>.
    restricted boolean DEFAULT false NOT NULL,
    CONSTRAINT projects_slug_chk CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$')
);
-- Existing nodes: additive column, same pattern as agents.actor_id / capability.
-- CREATE TABLE IF NOT EXISTS above is a no-op on an already-present table, so
-- without this line `restricted` never arrives on an updated node.
ALTER TABLE sn_config.projects ADD COLUMN IF NOT EXISTS restricted boolean DEFAULT false NOT NULL;

CREATE TABLE IF NOT EXISTS sn_config.project_members (
    project_id integer NOT NULL,
    member_kind text NOT NULL,
    member_id text NOT NULL,
    role text DEFAULT 'member' NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_members_kind_chk CHECK (member_kind IN ('user', 'agent')),
    CONSTRAINT project_members_role_chk CHECK (role IN ('lead', 'member', 'viewer'))
);

CREATE TABLE IF NOT EXISTS sn_config.agent_contacts (
    agent_id integer NOT NULL,
    contact_id integer NOT NULL,
    allowed_channels integer[] DEFAULT '{}'::integer[] NOT NULL,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.agent_knowledge (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer DEFAULT 1 NOT NULL,
    agent_id integer NOT NULL,
    kind text NOT NULL,
    ref text DEFAULT ''::text NOT NULL,
    content text,
    title text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_by text,
    archived_at timestamp with time zone,
    status text DEFAULT 'active'::text NOT NULL,
    CONSTRAINT agent_knowledge_kind_check CHECK ((kind = ANY (ARRAY['text'::text, 'doci'::text, 'url'::text, 'gdrive_file'::text, 'file'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.agent_policy_opts (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    agent_id integer NOT NULL,
    policy_slug text NOT NULL,
    action text NOT NULL,
    override_body_md text,
    CONSTRAINT agent_policy_opts_action_check CHECK ((action = ANY (ARRAY['include'::text, 'exclude'::text, 'override'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.agent_references (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer DEFAULT 1 NOT NULL,
    slug text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    target_kind text DEFAULT 'doci'::text NOT NULL,
    target_id text DEFAULT ''::text NOT NULL,
    required boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_by text,
    archived_at timestamp with time zone,
    CONSTRAINT agent_references_target_kind_check CHECK ((target_kind = ANY (ARRAY['doci'::text, 'papers'::text, 'mesh'::text, 'url'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.agent_tools (
    agent_id integer NOT NULL,
    tool_id integer NOT NULL,
    granted_caps text[] DEFAULT '{}'::text[] NOT NULL,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.agent_workflows (
    agent_id integer NOT NULL,
    workflow_id integer NOT NULL,
    granted_caps text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.agents (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    department_id integer,
    slug text NOT NULL,
    title text NOT NULL,
    role text,
    description text,
    specialist_id integer,
    model text DEFAULT 'claude-cli/sonnet'::text NOT NULL,
    schedule_cron text,
    os_user text,
    mesh_workspace text,
    enabled boolean DEFAULT true NOT NULL,
    version text DEFAULT '0.1.0'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_by text,
    archived_at timestamp with time zone,
    policy_categories text[] DEFAULT ARRAY['safety'::text, 'workflow'::text] NOT NULL,
    os_groups text[] DEFAULT '{}'::text[] NOT NULL,
    fs_read text[] DEFAULT '{}'::text[] NOT NULL,
    fs_write text[] DEFAULT '{}'::text[] NOT NULL,
    forbidden_fs text[] DEFAULT '{}'::text[] NOT NULL,
    forbidden_behavior text[] DEFAULT '{}'::text[] NOT NULL,
    avatar text,
    -- actor_id references the canonical actor for this agent.
    -- Populated by migration 036. FK is in the column-existence-guarded block after
    -- $fk$ (agents_actor_id_fkey). Column is NOT NULL after migration 036
    -- confirms all rows are populated.
    actor_id text,
    -- immutable product-role marker, set at creation,
    -- never updated. Seeds and installer scripts look up agents by capability,
    -- not by the mutable slug. Examples: 'configurator', 'developer', 'sysadmin'.
    capability text NOT NULL DEFAULT '',
    CONSTRAINT agents_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,40}$'::text))
);
-- Existing nodes: additive column. CREATE TABLE IF NOT EXISTS above is a no-op
-- when the table is already there, so without this line actor_id never arrives
-- on an updated node -- the column-existence guard below then skips the FK
-- forever, because nothing applies core-api/migrations/. Same line the
-- users table already carries, and what docs/KNOWN.md recommends.
ALTER TABLE sn_config.agents ADD COLUMN IF NOT EXISTS actor_id text;
-- capability column. NOT NULL DEFAULT '' so the column
-- arrives non-nullable on update; the backfill below populates the correct value.
ALTER TABLE sn_config.agents ADD COLUMN IF NOT EXISTS capability text NOT NULL DEFAULT '';
-- an agent's model is always <runtime>/<model>. The default moves with it,
-- or an insert that leaves the model out would store the bare form the catalog
-- trigger now refuses. Guarded: ALTER takes an exclusive lock on a live table.
DO $default$
BEGIN
    IF (SELECT column_default FROM information_schema.columns
         WHERE table_schema = 'sn_config' AND table_name = 'agents'
           AND column_name = 'model') IS DISTINCT FROM '''claude-cli/sonnet''::text' THEN
        ALTER TABLE sn_config.agents ALTER COLUMN model SET DEFAULT 'claude-cli/sonnet';
    END IF;
END
$default$;

CREATE TABLE IF NOT EXISTS sn_config.aliases (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    alias text NOT NULL,
    target_kind text NOT NULL,
    target_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.applications (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    version text,
    install_path text,
    status text DEFAULT 'installed'::text NOT NULL,
    manifest_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    installed_at timestamp with time zone DEFAULT now() NOT NULL,
    installed_by text,
    archived_at timestamp with time zone,
    CONSTRAINT applications_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,40}$'::text)),
    CONSTRAINT applications_status_check CHECK ((status = ANY (ARRAY['installed'::text, 'disabled'::text, 'broken'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.budgets (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    scope_type text NOT NULL,
    scope_id text NOT NULL,
    period text NOT NULL,
    limit_usd numeric NOT NULL,
    current_spend_usd numeric DEFAULT 0 NOT NULL,
    period_reset_at timestamp with time zone NOT NULL,
    hard_limit boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    -- the owner is told once per period that the budget is spent, and
    -- once before it is spent. Both marks are cleared when the period resets.
    notified_at timestamp with time zone,
    warn_at_pct integer DEFAULT 90 NOT NULL,
    warned_at timestamp with time zone,
    CONSTRAINT budgets_period_check CHECK ((period = ANY (ARRAY['daily'::text, 'weekly'::text, 'monthly'::text]))),
    CONSTRAINT budgets_scope_type_check CHECK ((scope_type = ANY (ARRAY['tenant'::text, 'agent'::text, 'session_metadata_key'::text])))
);

-- Existing nodes: the three budget notification columns, applied additively for
-- the same reason as the agent_runtime ones below.
ALTER TABLE sn_config.budgets ADD COLUMN IF NOT EXISTS notified_at timestamp with time zone;
ALTER TABLE sn_config.budgets ADD COLUMN IF NOT EXISTS warn_at_pct integer DEFAULT 90 NOT NULL;
ALTER TABLE sn_config.budgets ADD COLUMN IF NOT EXISTS warned_at timestamp with time zone;

-- Limit values and policy live here. budgets remains the spend meter during the
-- transition; its limit_usd column is legacy input to the idempotent seed only.
CREATE TABLE IF NOT EXISTS sn_config.limit_defs (
    code text PRIMARY KEY,
    label text NOT NULL,
    unit text NOT NULL,
    control_point text NOT NULL,
    description text NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sn_config.limit_profiles (
    code text NOT NULL REFERENCES sn_config.limit_defs(code),
    scope_type text NOT NULL CHECK (scope_type IN ('default', 'agent', 'node', 'metadata')),
    scope_id text NOT NULL,
    period text NOT NULL CHECK (period IN ('hour', 'day', 'week', 'month', 'turn')),
    value numeric NOT NULL CHECK (value > 0),
    action text NOT NULL CHECK (action IN ('refuse', 'pause', 'ask_owner', 'warn')),
    warn_at_pct integer NOT NULL DEFAULT 80 CHECK (warn_at_pct BETWEEN 1 AND 100),
    notify boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (code, scope_type, scope_id, period),
    CHECK ((scope_type IN ('agent', 'metadata') AND scope_id <> '*') OR
           (scope_type IN ('default', 'node') AND scope_id = '*'))
);
CREATE TABLE IF NOT EXISTS sn_config.node_control (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    node_paused boolean NOT NULL DEFAULT false,
    changed_at timestamptz NOT NULL DEFAULT now(),
    changed_by text
);
CREATE TABLE IF NOT EXISTS sn_core.limit_notices (
    code text NOT NULL,
    scope_type text NOT NULL,
    scope_id text NOT NULL,
    period_start timestamptz NOT NULL,
    kind text NOT NULL CHECK (kind IN ('warn', 'trip')),
    notified_at timestamptz,
    PRIMARY KEY (code, scope_type, scope_id, period_start, kind)
);

-- Compatibility read for the old AI Gateway budget cards and notification
-- endpoints. The meter stays in budgets; every displayed/enforced policy field
-- comes from limit_profiles. Clearing an agent override exposes the default.
CREATE OR REPLACE VIEW sn_config.budget_status AS
SELECT b.id, b.scope_type, b.scope_id, b.period,
       p.value AS limit_usd, b.current_spend_usd, b.period_reset_at,
       (p.action <> 'warn') AS hard_limit, b.enabled,
       p.action, p.warn_at_pct, p.notify, b.notified_at, b.warned_at
  FROM sn_config.budgets b
  LEFT JOIN sn_config.agents a ON b.scope_type='agent' AND a.id::text=b.scope_id
  JOIN LATERAL (
      SELECT lp.* FROM sn_config.limit_profiles lp
       WHERE lp.code = CASE WHEN b.scope_type='tenant' THEN 'node_spend_usd' ELSE 'spend_usd' END
         AND lp.period = CASE b.period WHEN 'daily' THEN 'day'
                                       WHEN 'weekly' THEN 'week' ELSE 'month' END
         AND (b.scope_type='tenant' AND lp.scope_type='node' AND lp.scope_id='*'
              OR b.scope_type='agent' AND lp.scope_type='agent' AND lp.scope_id=a.slug
              OR b.scope_type='agent' AND lp.scope_type='default' AND lp.scope_id='*'
              OR b.scope_type='session_metadata_key' AND lp.scope_type='metadata'
                 AND lp.scope_id=b.scope_id)
       ORDER BY CASE lp.scope_type WHEN 'agent' THEN 0 ELSE 1 END
       LIMIT 1
  ) p ON true
 WHERE b.enabled;

CREATE TABLE IF NOT EXISTS sn_config.changelog_entries (
    id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    entity_kind text NOT NULL,
    entity_id integer NOT NULL,
    version text,
    reason text,
    change_type text,
    changed_by text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT changelog_entries_change_type_check CHECK ((change_type = ANY (ARRAY['arch'::text, 'minor'::text, 'patch'::text]))),
    CONSTRAINT changelog_entries_entity_kind_check CHECK ((entity_kind = ANY (ARRAY['agent'::text, 'flow'::text, 'specialist'::text, 'policy'::text, 'department'::text, 'team'::text, 'reference'::text, 'tool'::text, 'application'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.channels (
    id integer NOT NULL,
    slug text NOT NULL,
    label text NOT NULL,
    transport text NOT NULL,
    mode text NOT NULL,
    adapter_slug text,
    status text DEFAULT 'planned'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.cli_providers (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    slug text NOT NULL,
    binary_path text NOT NULL,
    invocation_args jsonb DEFAULT '[]'::jsonb NOT NULL,
    supported_models text[] NOT NULL,
    env_vars jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
-- The instructions file this runtime reads by itself from the agent's folder
-- (Claude Code CLAUDE.md, Codex AGENTS.md, Flint FLINT.md from the release that
-- reads it as the owner's instructions). core-api writes the agent's composed
-- prompt under this name, and the gateway passes no identity by flag: one source,
-- delivered once.
ALTER TABLE sn_config.cli_providers ADD COLUMN IF NOT EXISTS instructions_file text NOT NULL DEFAULT 'CLAUDE.md';

-- Human-readable name for a gateway provider slug. The slug is the routing key
-- (used in sn_config.cli_providers.slug, sn_config.model_pricing.provider,
-- sn_config.models.provider, sn_core.ai_sessions.provider and the gateway's own
-- backend id) and must never be translated. display_name is what the panel
-- shows above the slug on a provider card: Anthropic, Google, OpenRouter.
-- Unknown slugs fall back to the slug itself, so a new provider added to the
-- gateway shows up without an edit here.
CREATE TABLE IF NOT EXISTS sn_config.providers (
    slug text PRIMARY KEY,
    display_name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.config_audit (
    id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    table_name text NOT NULL,
    row_pk text NOT NULL,
    action text NOT NULL,
    diff_jsonb jsonb,
    changed_by text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT config_audit_action_check CHECK ((action = ANY (ARRAY['insert'::text, 'update'::text, 'delete'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.contact_channels (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    contact_id integer NOT NULL,
    channel_id integer NOT NULL,
    handle text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    -- contact_channels is the ONE handle map, and it records how each
    -- binding was established. verified_by: session|dkim|platform-link|
    -- owner-confirmed|declared|none.
    verified_by text,
    verified_at timestamp with time zone,
    last_seen_at timestamp with time zone
);
-- Existing nodes: additive columns for the same (the IF NOT EXISTS create is a no-op there).
ALTER TABLE sn_config.contact_channels ADD COLUMN IF NOT EXISTS verified_by  text;
ALTER TABLE sn_config.contact_channels ADD COLUMN IF NOT EXISTS verified_at  timestamp with time zone;
ALTER TABLE sn_config.contact_channels ADD COLUMN IF NOT EXISTS last_seen_at timestamp with time zone;

CREATE TABLE IF NOT EXISTS sn_config.contacts (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer DEFAULT 1 NOT NULL,
    kind text NOT NULL,
    display_name text NOT NULL,
    org_name text,
    actor_id text,
    notes text DEFAULT ''::text NOT NULL,
    source text DEFAULT ''::text NOT NULL,
    consent_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT contacts_kind_chk CHECK ((kind = ANY (ARRAY['agent'::text, 'person'::text, 'customer'::text, 'supplier'::text, 'vendor'::text, 'lead'::text, 'partner'::text, 'other'::text])))
);
-- Existing nodes: additive column, for contacts_actor_id_fkey.
ALTER TABLE sn_config.contacts ADD COLUMN IF NOT EXISTS actor_id text;

CREATE TABLE IF NOT EXISTS sn_config.department_policies (
    department_id integer NOT NULL,
    policy_slug text NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.departments (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    slug text NOT NULL,
    name text NOT NULL,
    type_slug text,
    head_agent_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT departments_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,40}$'::text))
);

CREATE TABLE IF NOT EXISTS sn_config.flow_versions (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    flow_id integer NOT NULL,
    version text NOT NULL,
    yaml_snapshot text,
    changed_by text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    changelog_entry text
);

CREATE TABLE IF NOT EXISTS sn_config.flows (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    slug text NOT NULL,
    name text NOT NULL,
    version text DEFAULT '0.1.0'::text NOT NULL,
    yaml_path text,
    status text DEFAULT 'draft'::text NOT NULL,
    department_id integer,
    output_schema_slug text,
    created_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT flows_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'active'::text, 'deprecated'::text, 'archived'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.forbidden_actions (
    key text NOT NULL,
    label text NOT NULL,
    scope text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

-- The node's one secret store (core-api lib/secret_store.py; the gateway's
-- secrets_store.py reads it). Every credential of the node: the gateway's
-- provider keys (gateway/<provider>-key, gateway/claude-cli-token) and the
-- integrations agents and companions use (dotted refs such as
-- replicate.api_key). Values are pgp_sym_encrypt'ed with the node secret key
-- (/etc/smart-node/gateway-secret.key), which is never in the database. Every
-- read, write and refusal is journalled in sn_core.events (secret.read,
-- secret.set, secret.deleted, secret.access_denied).
CREATE TABLE IF NOT EXISTS sn_config.secrets (
    ref text PRIMARY KEY,
    value_enc bytea NOT NULL,
    namespace text NOT NULL,
    provider text,
    note text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text,
    CONSTRAINT secrets_ref_check CHECK ((ref ~ '^[a-zA-Z_][a-zA-Z0-9_-]{0,40}([./][a-zA-Z_][a-zA-Z0-9_-]{0,40}){0,5}$'::text))
);
CREATE INDEX IF NOT EXISTS secrets_namespace_idx ON sn_config.secrets (namespace);

-- Earlier values of a ref, kept thirty days after a replace or a delete.
CREATE TABLE IF NOT EXISTS sn_config.secret_versions (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    ref text NOT NULL,
    value_enc bytea NOT NULL,
    replaced_at timestamp with time zone DEFAULT now() NOT NULL,
    replaced_by text,
    reason text NOT NULL
);
CREATE INDEX IF NOT EXISTS secret_versions_ref_idx ON sn_config.secret_versions (ref, replaced_at DESC);

-- The gateway's own store before the one store. Its rows move over with their
-- refs and the table is kept one release under another name, for recovery.
DO $$
BEGIN
    IF to_regclass('sn_config.gateway_secrets') IS NOT NULL THEN
        INSERT INTO sn_config.secrets (ref, value_enc, namespace, provider, note,
                                       created_at, created_by, updated_at, updated_by)
        SELECT ref, value_enc, split_part(replace(ref, '/', '.'), '.', 1), provider, note,
               created_at, 'migration:gateway_secrets', updated_at, 'migration:gateway_secrets'
        FROM sn_config.gateway_secrets
        ON CONFLICT (ref) DO NOTHING;
        ALTER TABLE sn_config.gateway_secrets RENAME TO gateway_secrets_migrated;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS sn_config.integrations (
    id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    slug text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    kind text DEFAULT 'app'::text NOT NULL,
    status text DEFAULT 'available'::text NOT NULL,
    install_uri text DEFAULT ''::text NOT NULL,
    companion_slug text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    tenant_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    purpose text DEFAULT ''::text NOT NULL,
    install_path text DEFAULT ''::text NOT NULL,
    endpoint text DEFAULT ''::text NOT NULL,
    auth_kind text DEFAULT 'none'::text NOT NULL,
    auth_secret_ref text,
    auth_header_name text,
    last_discovered_at timestamp with time zone,
    capabilities text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT integrations_auth_kind_check CHECK ((auth_kind = ANY (ARRAY['none'::text, 'bearer'::text, 'api_key'::text, 'basic'::text, 'oauth2'::text, 'custom'::text]))),
    CONSTRAINT integrations_kind_check CHECK ((kind = ANY (ARRAY['system'::text, 'app'::text, 'external'::text, 'mcp-only'::text]))),
    CONSTRAINT integrations_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,40}$'::text)),
    CONSTRAINT integrations_status_check CHECK ((status = ANY (ARRAY['available'::text, 'not-installed'::text, 'broken'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.interactions (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    from_agent_id integer NOT NULL,
    to_agent_id integer NOT NULL,
    kind text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT interactions_kind_check CHECK ((kind = ANY (ARRAY['assigns_tasks_to'::text, 'escalates_to'::text, 'notifies'::text, 'reports_to'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.mcp_servers (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    application_id integer,
    slug text NOT NULL,
    name text NOT NULL,
    transport text NOT NULL,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    autoinstall boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_discovered_at timestamp with time zone,
    auth_secret_ref text,
    status text DEFAULT 'unknown'::text NOT NULL,
    CONSTRAINT mcp_servers_status_check CHECK ((status = ANY (ARRAY['unknown'::text, 'available'::text, 'broken'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.merge_ownership (
    target_table text NOT NULL,
    column_or_jsonpath text NOT NULL,
    owner text DEFAULT 'runtime'::text NOT NULL,
    CONSTRAINT merge_ownership_owner_check CHECK ((owner = ANY (ARRAY['profile'::text, 'runtime'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.model_pricing (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    kind text NOT NULL,
    unit_price_usd numeric NOT NULL,
    effective_from date DEFAULT CURRENT_DATE NOT NULL,
    notes text,
    CONSTRAINT model_pricing_kind_check CHECK ((kind = ANY (ARRAY['input_tokens'::text, 'output_tokens'::text, 'image'::text, 'video_second'::text, 'audio_second'::text, 'embedding'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.models (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    label text NOT NULL,
    modality text DEFAULT 'chat'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    vendor text NOT NULL
);

-- `provider` is the RUNTIME that executes a turn (claude-cli, openrouter,
-- gemini, ...), and an agent's model is <provider>/<model>. Who actually makes the
-- model (anthropic, openai, google, ...) is a different fact and gets its own
-- column: it cannot be read off the id (codex-cli/<name> says nothing about the
-- vendor, openrouter/<x> only by convention), and "what did we spend on OpenAI
-- models" must be a GROUP BY, not string parsing.
--
-- NOT NULL with no default: whoever adds a catalog row states the vendor. The
-- backfill below is the one place the vendor is inferred, for rows that predate
-- the column, and it runs only while some row still has none.
ALTER TABLE sn_config.models ADD COLUMN IF NOT EXISTS vendor text;
DO $vendor$
BEGIN
    IF EXISTS (SELECT 1 FROM sn_config.models WHERE vendor IS NULL) THEN
        UPDATE sn_config.models SET vendor = CASE provider
                WHEN 'claude-cli' THEN 'anthropic'
                WHEN 'cli'        THEN 'anthropic'
                WHEN 'codex-cli'  THEN 'openai'
                WHEN 'gemini'     THEN 'google'
                -- openrouter ids are <vendor>/<model> by OpenRouter's own convention
                WHEN 'openrouter' THEN split_part(model, '/', 1)
                ELSE provider
            END
         WHERE vendor IS NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'sn_config' AND table_name = 'models'
                  AND column_name = 'vendor' AND is_nullable = 'YES') THEN
        ALTER TABLE sn_config.models ALTER COLUMN vendor SET NOT NULL;
    END IF;
END
$vendor$;

-- which models an agent may be given is stated in ONE place, the enabled
-- chat rows of sn_config.models -- the same rows the panel offers (GET /api/models)
-- and the gateway resolves the backend from (session_service._canonical_model).
--
-- It used to be stated twice. agents_model_check enumerated model ids, and it had
-- drifted from the catalog in both directions: a catalog row (gemini-flash-latest)
-- that no agent could be set to, and ids the catalog never had. Adding a model to
-- a node meant an ALTER TABLE. So the enumeration is dropped and the column is
-- checked against the catalog instead: a new model is a row, never a migration.
--
-- A trigger, not a foreign key: agents.model is <provider>/<model>, a
-- value no single catalog column holds, so there is no key to point at. The bare
-- catalog name ('sonnet') is refused here: it did not say which runtime runs the
-- turn, and every reader had to guess "no slash = Claude CLI". core-api still
-- rewrites a bare name it is sent, for one release (agent.model_legacy_alias).
--
-- 'none' is not a model id, it is the explicit "this agent has no LLM" state the
-- panel renders as such; it is the one value accepted without a catalog row.
--
-- Only a CHANGED model is checked. Disabling a catalog row must not make every
-- later edit of an agent that still uses it fail on an unrelated field, and the
-- update must leave agents already set to a model exactly as they are.
CREATE OR REPLACE FUNCTION sn_config.agents_model_in_catalog() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.model = 'none' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.model IS NOT DISTINCT FROM OLD.model THEN
        RETURN NEW;
    END IF;
    IF EXISTS (SELECT 1 FROM sn_config.models m
                WHERE m.provider || '/' || m.model = NEW.model
                  AND m.enabled AND m.modality = 'chat') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'model % is not an enabled chat model in sn_config.models', quote_literal(NEW.model)
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'agents_model_in_catalog',
              HINT = 'An agent model is <provider>/<model> of an enabled chat row in sn_config.models; the panel lists exactly those rows.';
END
$fn$;

-- Guarded rather than DROP ... IF EXISTS / re-CREATE on every run: both take an
-- exclusive lock on sn_config.agents, and this file is applied to live nodes
-- An installed node pays for them once.
DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'agents_model_check'
                  AND conrelid = 'sn_config.agents'::regclass) THEN
        ALTER TABLE sn_config.agents DROP CONSTRAINT agents_model_check;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname = 'agents_model_in_catalog'
                      AND tgrelid = 'sn_config.agents'::regclass) THEN
        CREATE TRIGGER agents_model_in_catalog
            BEFORE INSERT OR UPDATE OF model ON sn_config.agents
            FOR EACH ROW EXECUTE FUNCTION sn_config.agents_model_in_catalog();
    END IF;
END
$guard$;

-- agents set before the canonical shape carry a bare catalog name. Each one
-- that names exactly one catalog row becomes <provider>/<model> of that row. Two
-- rows with the same name under different runtimes would make that a guess, so
-- such an agent is left alone, and so are 'none' and a value no row names: the
-- gateway still reads a bare name as claude-cli for one release (docs/upgrade).
-- version and updated_at are left as they are: the agent did not change, only
-- how its model is written.
UPDATE sn_config.agents a
   SET model = c.canonical
  FROM (SELECT m.model AS bare, min(m.provider || '/' || m.model) AS canonical
          FROM sn_config.models m
         WHERE m.enabled AND m.modality = 'chat'
         GROUP BY m.model
        HAVING count(*) = 1) c
 WHERE a.model = c.bare
   AND position('/' IN a.model) = 0
   AND a.model <> 'none';

CREATE TABLE IF NOT EXISTS sn_config.output_schemas (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    scope text NOT NULL,
    scope_id integer,
    slug text NOT NULL,
    name text,
    json_schema jsonb NOT NULL,
    version text DEFAULT '1.0.0'::text NOT NULL,
    CONSTRAINT output_schemas_scope_check CHECK ((scope = ANY (ARRAY['global'::text, 'tenant'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.policies (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    slug text NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    body_md text NOT NULL,
    severity text DEFAULT 'warn'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    version text DEFAULT '0.1.0'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_by text,
    archived_at timestamp with time zone,
    agent_id integer,
    CONSTRAINT policies_category_check CHECK ((category = ANY (ARRAY['safety'::text, 'workflow'::text, 'sales'::text, 'communication'::text, 'data-handling'::text, 'global'::text]))),
    CONSTRAINT policies_severity_check CHECK ((severity = ANY (ARRAY['info'::text, 'warn'::text, 'block'::text]))),
    CONSTRAINT policies_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,60}$'::text))
);

CREATE TABLE IF NOT EXISTS sn_config.projection_ledger (
    id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    param_key text NOT NULL,
    target_table text NOT NULL,
    target_pk text,
    natural_key text,
    content_hash text,
    projected_at timestamp with time zone DEFAULT now() NOT NULL,
    result text,
    behavior text,
    CONSTRAINT projection_ledger_result_chk CHECK (((result IS NULL) OR (result = ANY (ARRAY['projected'::text, 'failed'::text, 'skipped'::text, 'retired'::text]))))
);

CREATE TABLE IF NOT EXISTS sn_config.refs (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    scope text NOT NULL,
    scope_id integer,
    key text NOT NULL,
    target_uri text NOT NULL,
    required boolean DEFAULT false NOT NULL,
    notes text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT refs_scope_check CHECK ((scope = ANY (ARRAY['global'::text, 'tenant'::text, 'department'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.runtimes (
    id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    slug text NOT NULL,
    label text NOT NULL,
    purpose text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    kind text DEFAULT 'other'::text NOT NULL,
    status text DEFAULT 'available'::text NOT NULL,
    install_uri text DEFAULT ''::text NOT NULL,
    install_path text DEFAULT ''::text NOT NULL,
    companion_slug text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    tenant_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT runtimes_kind_check CHECK ((kind = ANY (ARRAY['claude-code'::text, 'codex'::text, 'gpt'::text, 'gemini'::text, 'other'::text]))),
    CONSTRAINT runtimes_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,40}$'::text)),
    CONSTRAINT runtimes_status_check CHECK ((status = ANY (ARRAY['available'::text, 'not-installed'::text, 'broken'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.session_retention (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    scope_type text NOT NULL,
    scope_id text,
    retention_days integer,
    archive_to text,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_retention_retention_days_check CHECK (((retention_days IS NULL) OR (retention_days > 0))),
    CONSTRAINT session_retention_scope_type_check CHECK ((scope_type = ANY (ARRAY['global'::text, 'tenant'::text, 'provider'::text, 'session_type'::text])))
);

-- Context handoff policy.  A global row is the node default; an
-- agent row overrides it.  NULLS NOT DISTINCT makes the global row genuinely
-- unique instead of relying on application code around PostgreSQL's ordinary
-- NULL-distinct UNIQUE behaviour.
CREATE TABLE IF NOT EXISTS sn_config.session_context_limits (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    scope_type text NOT NULL,
    scope_id text,
    threshold_tokens integer DEFAULT 120000 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_context_limits_scope_check CHECK (
        (scope_type = 'global' AND scope_id IS NULL)
        OR (scope_type = 'agent' AND scope_id IS NOT NULL AND length(scope_id) > 0)
    ),
    CONSTRAINT session_context_limits_threshold_check CHECK (
        threshold_tokens BETWEEN 10000 AND 1000000
    )
);

CREATE TABLE IF NOT EXISTS sn_config.specialists (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    slug text NOT NULL,
    name text NOT NULL,
    body_md text DEFAULT ''::text NOT NULL,
    archetype text,
    version text DEFAULT '0.1.0'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_by text,
    archived_at timestamp with time zone,
    category text,
    CONSTRAINT specialists_archetype_check CHECK ((archetype = ANY (ARRAY['head'::text, 'specialist'::text, 'reviewer'::text, 'autonomous'::text, 'shared'::text]))),
    CONSTRAINT specialists_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,60}$'::text))
);

CREATE TABLE IF NOT EXISTS sn_config.team_members (
    team_id integer NOT NULL,
    agent_id integer NOT NULL,
    role_in_team text,
    CONSTRAINT team_members_role_in_team_check CHECK ((role_in_team = ANY (ARRAY['head'::text, 'specialist'::text, 'reviewer'::text, 'autonomous'::text, 'shared'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.teams (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    department_id integer,
    slug text NOT NULL,
    name text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS sn_config.tenant_brand (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    kind text NOT NULL,
    body_md text,
    json_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tenant_brand_kind_check CHECK ((kind = ANY (ARRAY['brand-voice'::text, 'compliance'::text, 'content-calendar'::text, 'platform-limits'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.tenant_profile (
    param_key text NOT NULL,
    value jsonb DEFAULT 'null'::jsonb NOT NULL,
    value_kind text DEFAULT 'literal'::text NOT NULL,
    behavior text NOT NULL,
    target text,
    natural_key text,
    source text DEFAULT 'interview'::text NOT NULL,
    default_hash text,
    status text DEFAULT 'pending'::text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tenant_profile_behavior_check CHECK ((behavior = ANY (ARRAY['config-sync'::text, 'stateful-merge'::text, 'seed-once'::text, 'policy-versioned'::text, 'secret-write-once'::text]))),
    CONSTRAINT tenant_profile_natural_key_required CHECK (((behavior <> ALL (ARRAY['seed-once'::text, 'policy-versioned'::text])) OR (natural_key IS NOT NULL))),
    CONSTRAINT tenant_profile_secret_iff_write_once CHECK (((value_kind = 'secret-ref'::text) = (behavior = 'secret-write-once'::text))),
    CONSTRAINT tenant_profile_source_check CHECK ((source = ANY (ARRAY['vertical-pack-default'::text, 'interview'::text, 'manual'::text]))),
    CONSTRAINT tenant_profile_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'confirmed'::text, 'projected'::text, 'failed'::text, 'diverged'::text, 'retired'::text]))),
    CONSTRAINT tenant_profile_value_kind_check CHECK ((value_kind = ANY (ARRAY['literal'::text, 'pointer'::text, 'secret-ref'::text])))
);

CREATE TABLE IF NOT EXISTS sn_config.tenant_profile_history (
    param_key text NOT NULL,
    version integer NOT NULL,
    value jsonb DEFAULT 'null'::jsonb NOT NULL,
    source text,
    who text,
    ts timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_config.tenants (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    settings_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    preamble_md text,
    CONSTRAINT tenants_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,40}$'::text))
);

CREATE TABLE IF NOT EXISTS sn_config.tools (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    application_id integer,
    slug text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    path_or_endpoint text,
    description text,
    schema_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    caps text[] DEFAULT '{}'::text[] NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tools_kind_check CHECK ((kind = ANY (ARRAY['cli'::text, 'mcp'::text, 'api'::text, 'builtin'::text]))),
    CONSTRAINT tools_slug_check CHECK ((slug ~ '^[a-z][a-z0-9-]{1,40}$'::text))
);

CREATE TABLE IF NOT EXISTS sn_config.user_channels (
    id integer NOT NULL,
    tenant_id integer,
    user_id integer,
    channel_slug text,
    address text,
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sn_config.users (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    tenant_id integer,
    email text NOT NULL,
    name text,
    role text,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    contact_email text,
    handle text,
    -- an account is a person who can log in, i.e. a contact with a login.
    contact_id integer,
    -- canonical actor identity for this account.
    actor_id text
);
-- Existing nodes: additive columns + guarded FKs.
ALTER TABLE sn_config.users ADD COLUMN IF NOT EXISTS contact_id integer;
-- actor_id references the canonical actor for this account.
ALTER TABLE sn_config.users ADD COLUMN IF NOT EXISTS actor_id text;
-- The person's own time zone (IANA name, from their browser): "tomorrow at 9" is
-- their 9, whatever the node's clock says. NULL = the node's local time.
ALTER TABLE sn_config.users ADD COLUMN IF NOT EXISTS timezone text;
DO $users_contact_fk$
BEGIN
    -- The primary key on contacts is added by the constraint-repair block at the
    -- END of this file, four hundred lines below, because that is where a dump
    -- puts every key. This foreign key needs it NOW: a reference requires a
    -- unique constraint on the far side, and on a FRESH database there is not one
    -- yet. Postgres refuses with "there is no unique constraint matching given
    -- keys for referenced table contacts", B02 exits, and the install stops at
    -- exit 3 having built nothing.
    --
    -- It never showed on an existing node, where contacts_pkey was already there
    -- from an earlier release. So a fresh install has been impossible now
    -- added this column, and nobody built a node from scratch until 2026-08-16.
    -- Ensure the key here rather than moving the foreign key: the repair block
    -- below is idempotent and simply finds it present.
    -- conrelid, not conname alone: a constraint name is unique per TABLE, and
    -- this database has five table names living in two schemas at once.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'contacts_pkey'
                     AND conrelid = 'sn_config.contacts'::regclass) THEN
        ALTER TABLE sn_config.contacts ADD CONSTRAINT contacts_pkey PRIMARY KEY (id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'users_contact_id_fkey'
                     AND conrelid = 'sn_config.users'::regclass) THEN
        ALTER TABLE sn_config.users
            ADD CONSTRAINT users_contact_id_fkey
            FOREIGN KEY (contact_id) REFERENCES sn_config.contacts(id);
    END IF;
END
$users_contact_fk$;

-- users.actor_id FK is in the column-existence-guarded block after
-- $fk$ (actors_pkey is in the $pk$ block and must exist first; the migration moved
-- it out of $fk$ so the update path is safe when actor_id is not yet present).

CREATE TABLE IF NOT EXISTS sn_core.actors (
    id text NOT NULL,
    display_name text NOT NULL,
    type text NOT NULL,
    layer text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    -- alias resolution scaffolding. NULL = this row IS
    -- canonical. Non-null = alias pointing to its canonical counterpart.
    -- Scaffolding removed in step 9, one release cycle after step 1.
    canonical_id text CONSTRAINT actors_canonical_id_self_chk CHECK (canonical_id <> id),
    aliased_at timestamp with time zone,
    -- the identity rule defines two axes of identity.
    -- kind: 'human', 'agent', 'component'. Immutable after first write.
    -- relation: 'internal', 'external'. Changes only through an audited endpoint.
    -- Both become NOT NULL after migration 037 classifies all existing actors.
    kind text,
    relation text,
    CONSTRAINT actors_layer_chk CHECK ((layer = ANY (ARRAY['specialist'::text, 'orchestrator'::text, 'guard'::text, 'human'::text, 'system'::text])))
);

CREATE TABLE IF NOT EXISTS sn_core.agent_runtime (
    agent_slug text NOT NULL,
    status text DEFAULT 'free'::text NOT NULL,
    current_ref text,
    lease_until timestamp with time zone,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    -- The wake flag permits: ONE mark per agent,
    -- not one row per event, so ten tasks assigned at once are one wake and not
    -- ten launches. A timestamp rather than a boolean, because the run that
    -- clears it has to answer "was this re-set while I was working?" -- clear
    -- only when wake_requested_at still equals the value captured at claim
    -- (wake_seen_at); otherwise work that arrived mid-run is erased unanswered.
    wake_requested_at timestamp with time zone,
    wake_seen_at timestamp with time zone,
    -- an exhausted budget pauses the agent instead of breaking it. While
    -- this is set and in the future the runner does not wake the agent; the period
    -- reset clears it. The runner's pending-work query reads this column, so a node
    -- without it cannot dispatch any agent at all.
    budget_suspended_until timestamp with time zone,
    -- the other wall an agent can hit. Kept separate from the budget
    -- one because the reason and the reset differ, and an operator has to be
    -- able to tell which of the two is holding an agent down. Set when the
    -- agent went over its unattended-wake cap; it lifts by itself once the
    -- window drains. Work with a person behind it is never held by this.
    wake_suspended_until timestamp with time zone
);

-- Existing nodes: the CREATE above is IF NOT EXISTS, so it does nothing on a
-- node that already has the table. These are the same two columns, applied
-- additively, so B02 upgrades a live node instead of only shaping a fresh one.
ALTER TABLE sn_core.agent_runtime ADD COLUMN IF NOT EXISTS wake_requested_at timestamp with time zone;
ALTER TABLE sn_core.agent_runtime ADD COLUMN IF NOT EXISTS wake_seen_at timestamp with time zone;
ALTER TABLE sn_core.agent_runtime ADD COLUMN IF NOT EXISTS budget_suspended_until timestamp with time zone;
ALTER TABLE sn_core.agent_runtime ADD COLUMN IF NOT EXISTS wake_suspended_until timestamp with time zone;
-- The messages one wake is for, {"ref": <current_ref>, "ids": [...]}: a wake for
-- one chat answers that chat only, in its own context. Read
-- only while that same claim stands (same ref, busy, lease valid).
ALTER TABLE sn_core.agent_runtime ADD COLUMN IF NOT EXISTS wake_scope jsonb;

-- Existing nodes: alias resolution columns.
ALTER TABLE sn_core.actors ADD COLUMN IF NOT EXISTS canonical_id text;
ALTER TABLE sn_core.actors ADD COLUMN IF NOT EXISTS aliased_at timestamp with time zone;
-- Existing nodes: kind and relation axes.
-- Both become NOT NULL after migration 037 classifies all actors.
ALTER TABLE sn_core.actors ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE sn_core.actors ADD COLUMN IF NOT EXISTS relation text;

-- One row per model call an agent's Claude Code process made through the
-- gateway (anthropic_relay.py). session_id is the gateway session the CLI
-- names in x-claude-code-session-id; auth says whether the process held a
-- gateway token or the node credential; outcome is ok, upstream_<status>,
-- refused:<why> or error. Usage is what the provider reported for this one
-- call; the per-turn usage the CLI reports stays in ai_sessions.usage.
CREATE TABLE IF NOT EXISTS sn_core.model_calls (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    rid text,
    session_id text,
    agent text,
    os_user text,
    model text,
    auth text,
    outcome text,
    status integer,
    input_tokens integer,
    output_tokens integer,
    cache_read integer,
    cache_write integer,
    stop_reason text,
    tool_use jsonb DEFAULT '[]'::jsonb NOT NULL,
    duration_ms integer,
    user_agent text,
    provider text
);
ALTER TABLE sn_core.model_calls ADD COLUMN IF NOT EXISTS provider text;
CREATE INDEX IF NOT EXISTS model_calls_ts_idx ON sn_core.model_calls (ts DESC);
CREATE INDEX IF NOT EXISTS model_calls_agent_ts_idx ON sn_core.model_calls (agent, ts DESC);
CREATE INDEX IF NOT EXISTS model_calls_session_idx ON sn_core.model_calls (session_id);

-- The model calls in flight right now, one row per call, written by the relay:
-- inserted when the call goes upstream, last_event_at and bytes moved while the
-- answer streams (at most every few seconds), deleted when the call ends (its
-- model_calls row is written then). A model_calls row exists only once a call
-- is over, so a call that hangs was invisible until it ended: this table is
-- how the node sees "waiting on the model, nothing received for N seconds".
-- last_event_at is the last event that carries the answer forward (text,
-- a tool call, the end of a message); ping events and SSE comments keep a
-- connection open but say nothing about the model, so they do not count.
-- The door empties it when it starts: rows of a door that died are not calls.
-- UNLOGGED: no WAL for rows that live seconds; a crash empties it, which is
-- what the door does on start anyway. One row per call, each written only by
-- its own call, so concurrent agents never wait on each other's rows.
CREATE UNLOGGED TABLE IF NOT EXISTS sn_core.model_calls_live (
    rid text PRIMARY KEY,
    session_id text,
    agent text,
    model text,
    provider text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    last_event_at timestamp with time zone,
    bytes bigint DEFAULT 0 NOT NULL
);
CREATE INDEX IF NOT EXISTS model_calls_live_session_idx ON sn_core.model_calls_live (session_id);

-- The token an agent's Claude Code process presents at the door, as a digest
-- (model_tokens.py). Minted by the runtime at spawn, revoked when the process
-- ends, and all of them when the runtime starts or stops: the door and the
-- runtime are two processes, so the token cannot live in either one's memory.
CREATE TABLE IF NOT EXISTS sn_core.model_tokens (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    digest text NOT NULL UNIQUE,
    session_id text,
    agent text,
    os_user text,
    model text,
    provider text,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    revoke_reason text
);
ALTER TABLE sn_core.model_tokens ADD COLUMN IF NOT EXISTS provider text;
CREATE INDEX IF NOT EXISTS model_tokens_live_session_idx ON sn_core.model_tokens (session_id)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS sn_core.ai_session_messages (
    id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    session_id uuid NOT NULL,
    seq integer NOT NULL,
    role text NOT NULL,
    content_type text NOT NULL,
    content text,
    artifact_uri text,
    tool_calls jsonb,
    tool_call_id text,
    is_partial boolean DEFAULT false NOT NULL,
    is_injected boolean DEFAULT false NOT NULL,
    usage jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ai_session_messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'system'::text, 'operator'::text, 'tool'::text])))
);

CREATE TABLE IF NOT EXISTS sn_core.ai_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id integer NOT NULL,
    type text NOT NULL,
    model text NOT NULL,
    provider text NOT NULL,
    is_opaque boolean DEFAULT false NOT NULL,
    agent_id integer,
    status text DEFAULT 'active'::text NOT NULL,
    system_prompt text,
    parent_session_id uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    usage jsonb DEFAULT '{}'::jsonb NOT NULL,
    budget_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_activity_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    error text,
    -- 'reaped' added by migration 025. The session reaper had been
    -- writing it since June against a constraint that never allowed it, and the
    -- failure was silent. A reaped session is its own outcome -- killed from
    -- outside while idle -- not a cancellation and not a completion.
    CONSTRAINT ai_sessions_status_check CHECK ((status = ANY (ARRAY['active'::text, 'streaming'::text, 'idle_released'::text, 'completed'::text, 'failed'::text, 'cancelled'::text, 'reaped'::text]))),
    CONSTRAINT ai_sessions_type_check CHECK ((type = ANY (ARRAY['chat'::text, 'image_gen'::text, 'video_gen'::text, 'tts'::text, 'stt'::text, 'embed'::text])))
);

-- the vendor of the model a session ran on, copied from the catalog when the
-- session is written, so spend per vendor is SUM(usage->>'cost_usd') GROUP BY
-- vendor and survives later edits of the catalog. Stamped by the database rather
-- than by each writer: the gateway writes sessions from two places (the session
-- service stores the canonical <runtime>/<model>, the stateless chat stores the
-- runtime and its own model id apart), and a writer that forgot would leave spend
-- unattributed without an error anywhere. NULL = no catalog row names the model.
ALTER TABLE sn_core.ai_sessions ADD COLUMN IF NOT EXISTS vendor text;

-- Every desk-list row looks its sessions up by metadata->>'conversation'. Without
-- this index each of the three correlated subqueries in GET /api/desks scanned
-- ai_sessions per desk, and a few of them held every core-api connection.
-- Already present on a node that got it by hand; IF NOT EXISTS keeps that a no-op.
CREATE INDEX IF NOT EXISTS idx_sessions_conversation
    ON sn_core.ai_sessions USING btree ((metadata ->> 'conversation'));

-- The desk list and the unread counts read each desk's messages by three keys: the session's
-- conversation (equal to desk:<id>, or ending in :desk:<id>), the session's user, and the bus
-- message's recipient. Without an index for each, a GET /api/desks/unread took 40 s on a node with
-- 13 thousand sessions, the Studio poll came back after the read mark was sent and the unread
-- badge returned. The trigram index serves the suffix match; pg_trgm is created by B02-persistence.sh.
-- The trigram index is an optimisation, never a reason for an install to stop: it is made only
-- where pg_trgm exists and this role may use its schema (a bare database, as the schema tests
-- use, has neither). The extension's own schema is read, not assumed.
DO $trgm$
DECLARE ext_schema text;
BEGIN
    SELECT n.nspname INTO ext_schema FROM pg_extension e
      JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm';
    IF ext_schema IS NULL THEN
        RAISE NOTICE 'pg_trgm is not installed: idx_sessions_conversation_trgm skipped';
    ELSE
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_sessions_conversation_trgm '
                       'ON sn_core.ai_sessions USING gin ((metadata ->> ''conversation'') %I.gin_trgm_ops)', ext_schema);
    END IF;
EXCEPTION WHEN insufficient_privilege OR undefined_object THEN
    RAISE NOTICE 'idx_sessions_conversation_trgm skipped: %', SQLERRM;
END
$trgm$;
CREATE INDEX IF NOT EXISTS idx_sessions_user_key
    ON sn_core.ai_sessions USING btree ((metadata ->> 'user'));
-- idx_messages_recipient is made right after sn_core.messages is created, further down.

-- SECURITY DEFINER with a pinned search_path like every sn_core function
-- (tests/agent-lock-one-rule.test.sh): the writers of sessions need not read the
-- catalog themselves, and an unpinned search_path is a way in.
CREATE OR REPLACE FUNCTION sn_core.ai_sessions_stamp_vendor() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $fn$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.model IS DISTINCT FROM OLD.model
                        OR NEW.provider IS DISTINCT FROM OLD.provider THEN
        NEW.vendor := (SELECT m.vendor FROM sn_config.models m
                        WHERE m.provider || '/' || m.model = NEW.model
                           OR (m.provider = NEW.provider AND m.model = NEW.model)
                        ORDER BY m.enabled DESC, m.id
                        LIMIT 1);
    END IF;
    RETURN NEW;
END
$fn$;

DO $stamp$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname = 'ai_sessions_stamp_vendor'
                      AND tgrelid = 'sn_core.ai_sessions'::regclass) THEN
        CREATE TRIGGER ai_sessions_stamp_vendor
            BEFORE INSERT OR UPDATE OF model, provider ON sn_core.ai_sessions
            FOR EACH ROW EXECUTE FUNCTION sn_core.ai_sessions_stamp_vendor();
    END IF;
END
$stamp$;

-- Sessions written before the column: stamp the ones a catalog row names. Before
-- Previously the stateless chat logged the Claude CLI as provider 'cli', so that
-- spelling is matched here once for history; new rows are written as claude-cli.
-- Touches only rows still unstamped that a row can name, so a re-run is cheap.
UPDATE sn_core.ai_sessions s
   SET vendor = m.vendor
  FROM sn_config.models m
 WHERE s.vendor IS NULL
   AND (m.provider || '/' || m.model = s.model
        OR (m.provider = s.provider AND m.model = s.model)
        OR (s.provider = 'cli' AND m.provider = 'claude-cli' AND m.model = s.model));

CREATE TABLE IF NOT EXISTS sn_core.cron_runs (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    cron_id integer NOT NULL,
    started_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone,
    exit_code integer,
    duration_ms integer,
    output_snippet text DEFAULT ''::text,
    created_at timestamp without time zone DEFAULT now(),
    tenant_id integer DEFAULT 1 NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_core.crons (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    schedule character varying(100) NOT NULL,
    command text NOT NULL,
    script_path character varying(500),
    description character varying(500) DEFAULT ''::character varying,
    "group" character varying(50) DEFAULT 'other'::character varying,
    log_file character varying(500) DEFAULT ''::character varying,
    crontab_user character varying(50) DEFAULT 'root'::character varying,
    enabled boolean DEFAULT true,
    disabled_flag character varying(500),
    notes text DEFAULT ''::text,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    tenant_id integer DEFAULT 1 NOT NULL,
    slug text
);

CREATE TABLE IF NOT EXISTS sn_core.desks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id integer NOT NULL,
    owner_user_id integer NOT NULL,
    title text DEFAULT 'Untitled desk'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS sn_core.events (
    id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    actor text NOT NULL,
    event_type text NOT NULL,
    entity_type text,
    entity_id text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    parent_event_id bigint,
    tenant_id integer DEFAULT 1 NOT NULL
);

-- ----------------------------------------------------------------------
-- message_attachments -- per-message file metadata for inbound attachments on
-- any channel. Bytes live on disk under
-- /srv/data/attachments/{storage_path}; access is decided by the PARENT
-- message, so this table carries no ACL of its own.
--
-- It lived only in companions/core-api/migrations/029_message_attachments.sql,
-- and that directory says of itself that it is documentation and not a runner.
-- Meanwhile communication.sh GRANTs SELECT, INSERT on this table, the email
-- adapter INSERTs through common/db.py, and core-api/routes/attachments.py
-- SELECTs from it. So on a node built from this schema the communication
-- companion cannot install at all: the moment ANY channel is configured it
-- dies on
--     ERROR:  relation "sn_core.message_attachments" does not exist
-- and the install stops with "Phase 2 companion communication failed (exit 5)".
--
-- It stayed hidden because the companion is SKIPPED when no channel is given.
-- The failure waits for the first operator who passes --tg-token or
-- --mail-host, which is to say for the first node that tries to talk to
-- anyone. Reported from a node in the field when they wired a
-- Telegram bot.
--
-- Columns match 029. The SHAPE does not, and must not: this file declares
-- tables with CHECK and DEFAULT inline and adds every PK and FK in the
-- constraint lists below. 029 writes the PK and the FK inline, and a table
-- pasted here with a `REFERENCES sn_core.messages` on it fails outright on a
-- fresh database, because messages is created eighty lines further down. That
-- is not a hypothetical: it is what this file did when the migration was
-- pasted in verbatim, proven on a throwaway database before this comment was
-- written.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sn_core.message_attachments (
    id               uuid        DEFAULT gen_random_uuid() NOT NULL,
    -- The parent is a channel message, a Studio desk OR a planner task,
    -- exactly one of the three: a desk chat lives in gateway sessions, not in
    -- sn_core.messages, so a file the owner attaches in the Studio has no
    -- message to hang from, and a file on a task (a hitl answer's photo) has
    -- neither. The CHECK and the desk FK are in the lists below. task_id has no
    -- FK: planner tasks live in the planner's own database.
    message_id       bigint,
    desk_id          uuid,
    task_id          bigint,
    tenant_id        integer     DEFAULT 1 NOT NULL,
    channel_type     text        NOT NULL,
    filename         text,
    content_type     text,
    size_bytes       bigint,
    content_hash     text,       -- SHA-256 hex of the stored bytes
    storage_path     text,       -- relative path within /srv/data/attachments/
    status           text        DEFAULT 'received'::text NOT NULL,
    -- Why the scan stage gave this status (the attachment scan stage; phase 1 records
    -- 'owner-upload' or 'trusted-sender', later scan stages add the detailed rules).
    scan_reason      text,
    -- received_at mirrors the parent message
    received_at      timestamptz,
    created_at       timestamptz DEFAULT now() NOT NULL
);
-- An installed node converges to the shape above: the desk parent,
-- the scan reason, a message_id that may be NULL for a desk file, and
-- 'approved' in the status set. The status CHECK is dropped and re-added by
-- name rather than declared inline, so that adding a value is one edit here
-- and not a constraint an old node keeps forever.
ALTER TABLE sn_core.message_attachments ADD COLUMN IF NOT EXISTS desk_id uuid;
ALTER TABLE sn_core.message_attachments ADD COLUMN IF NOT EXISTS task_id bigint;
ALTER TABLE sn_core.message_attachments ADD COLUMN IF NOT EXISTS scan_reason text;
ALTER TABLE sn_core.message_attachments ALTER COLUMN message_id DROP NOT NULL;
ALTER TABLE sn_core.message_attachments DROP CONSTRAINT IF EXISTS message_attachments_status_check;
ALTER TABLE sn_core.message_attachments ADD CONSTRAINT message_attachments_status_check
    CHECK (status IN ('received', 'approved', 'quarantined', 'processed', 'expired'));
-- The one-parent CHECK is added below only when no constraint of its name
-- exists. A node that has the two-parent form (message XOR desk) would keep it
-- and refuse every task file, so that form is dropped here and the list
-- re-adds the three-parent one. A node that already counts task_id is left alone.
DO $one_parent$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'message_attachments_one_parent_chk'
                  AND conrelid = 'sn_core.message_attachments'::regclass
                  AND pg_get_constraintdef(oid) NOT LIKE '%task_id%') THEN
        ALTER TABLE sn_core.message_attachments DROP CONSTRAINT message_attachments_one_parent_chk;
    END IF;
END
$one_parent$;

CREATE TABLE IF NOT EXISTS sn_core.message_state (
    message_id bigint NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    processed_at timestamp with time zone,
    error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    -- Reply obligation . A message whose sender asked for a reply
    -- and whose recipient ended its turn without one is 'awaiting_reply':
    -- wake_next_at is when it is next due a reminder, wake_attempts how many
    -- wakes it has had. Both mean something only in that status.
    wake_next_at timestamp with time zone,
    wake_attempts smallint DEFAULT 0 NOT NULL,
    -- Outbound retry. A send that failed for a reason that passes (the
    -- provider down, a timeout, a 5xx) stays 'new' with send_next_at in the
    -- future; send_attempts counts the tries. Both mean something only on
    -- outbound rows.
    send_next_at timestamp with time zone,
    send_attempts smallint DEFAULT 0 NOT NULL,
    -- 'history': recorded as evidence, deliberately never routed --
    -- it predates the moment this channel instance was bound to an agent.
    -- Distinct from 'processed' (an agent dealt with it) and from 'blocked'
    -- (policy refused it): a queue that calls all three the same cannot answer
    -- "why did nobody reply to this".
    -- The single authority for this set is the guarded re-assert in
    -- B02-sn-core-seed.sql; extending it here alone reaches fresh nodes only.
    CONSTRAINT message_state_status_chk CHECK ((status = ANY (ARRAY['new'::text, 'routed'::text, 'processed'::text, 'failed'::text, 'blocked'::text, 'history'::text, 'dry_run'::text, 'awaiting_reply'::text])))
);
-- A node installed before the reply obligation has the table without these.
ALTER TABLE sn_core.message_state ADD COLUMN IF NOT EXISTS wake_next_at timestamp with time zone;
ALTER TABLE sn_core.message_state ADD COLUMN IF NOT EXISTS wake_attempts smallint DEFAULT 0 NOT NULL;
-- A node installed before outbound retry has the table without these.
ALTER TABLE sn_core.message_state ADD COLUMN IF NOT EXISTS send_next_at timestamp with time zone;
ALTER TABLE sn_core.message_state ADD COLUMN IF NOT EXISTS send_attempts smallint DEFAULT 0 NOT NULL;
-- Seen: an inbound message handed to one of its agent's wakes and not answered
-- yet. Written when the Runner claims the agent for that wake. It is what tells
-- "not seen" from "seen, still open": the Runner kept that in its own memory,
-- as one high-water id, which a restart lost and a wake for one chat made wrong
-- (2026-10-29). handed_count is how many wakes it has had.
ALTER TABLE sn_core.message_state ADD COLUMN IF NOT EXISTS handed_at timestamp with time zone;
ALTER TABLE sn_core.message_state ADD COLUMN IF NOT EXISTS handed_ref text;
ALTER TABLE sn_core.message_state ADD COLUMN IF NOT EXISTS handed_count smallint DEFAULT 0 NOT NULL;

CREATE TABLE IF NOT EXISTS sn_core.messages (
    id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    channel_type text NOT NULL,
    channel_id text,
    external_id text NOT NULL,
    thread_id text,
    direction text NOT NULL,
    sender text,
    recipient text,
    content text,
    content_type text,
    provenance_chain jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    ai_session_id uuid,
    tenant_id integer DEFAULT 1 NOT NULL,
    CONSTRAINT messages_direction_chk CHECK ((direction = ANY (ARRAY['inbound'::text, 'outbound'::text])))
);

-- The desk list and the unread counts read bus messages by recipient (desk:<id>); see the
-- note at idx_sessions_conversation_trgm. Made here because the table is created here.
CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON sn_core.messages USING btree (recipient);

-- The recipient SET. A message goes to a group, not to one address:
-- reply-all, adding a person to a thread, and later several agents in one chat.
-- The set is channel-agnostic and lives here, in the journal every channel
-- already shares; the ROLE is not -- `cc` is an email idea and only the email
-- adapter reads it. Chat will use the same set for who is in a thread and will
-- never set one.
--
-- Shape: [{"addr": "...", "role": "to"|"cc"}]. `recipient` stays the primary
-- address and stays authoritative for every caller not yet changed, so this is
-- additive: a row with no set behaves exactly as before. An ALTER rather than a
-- column in the CREATE above because CREATE TABLE IF NOT EXISTS skips entirely
-- on a node that already has the table, which would reach fresh nodes only.
--
-- Not here yet, deliberately: `bcc`. It is not one more address -- a blind
-- recipient must not appear in the headers the others receive and must not be
-- gathered by a later reply-all -- so it is a rendering rule and waits until
-- the simple case carries traffic.
ALTER TABLE sn_core.messages ADD COLUMN IF NOT EXISTS recipients jsonb;

-- The second of exactly two places where this file references a table before the
-- key block at the bottom has given that table a key. See the longer note beside
-- users_contact_id_fkey above: a foreign key needs a unique constraint on the far
-- side AT CREATION, the dump puts every key at the end, and on a FRESH database
-- the reference therefore fails and takes the whole install with it. The table
-- below carries an inline foreign key to sn_core.messages, so the key has to
-- exist by now.
--
-- Two, not more: every REFERENCES above the key block points at either
-- sn_config.contacts or sn_core.messages. If a third is ever added, it belongs
-- here in the same shape rather than as a third special case -- and at that
-- point the honest change is to move the key block up instead.
DO $messages_pk$
BEGIN
    -- conrelid, not conname alone. A constraint name is unique per TABLE, not
    -- per database, and public.messages -- the legacy twin -- already carries a
    -- messages_pkey. A bare name check finds that one, concludes the key exists
    -- and creates nothing, and the reference below fails anyway. The first
    -- version of this block did exactly that, which is the same unqualified-name
    -- defect this release exists to remove.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'messages_pkey'
                     AND conrelid = 'sn_core.messages'::regclass) THEN
        ALTER TABLE sn_core.messages ADD CONSTRAINT messages_pkey PRIMARY KEY (id);
    END IF;
END
$messages_pk$;

-- the identity verdict recorded AT ARRIVAL, one row per message, beside
-- messages (which is itself append-only by trigger). Proof exists only at the
-- moment a message arrives -- the Authum session was live for that request, the
-- Authentication-Results header was on that mail -- and cannot be reconstructed
-- later, so it is written down here rather than derived on read. contact_id is
-- NULL for an unmapped handle.
CREATE TABLE IF NOT EXISTS sn_core.message_identity (
    message_id  bigint NOT NULL,
    contact_id  integer,
    handle      text NOT NULL,
    trust       text NOT NULL,
    evidence    jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT message_identity_pkey PRIMARY KEY (message_id),
    CONSTRAINT message_identity_trust_chk CHECK ((trust = ANY (ARRAY['session'::text, 'proved'::text, 'linked'::text, 'declared'::text, 'unknown'::text]))),
    CONSTRAINT message_identity_message_id_fkey FOREIGN KEY (message_id) REFERENCES sn_core.messages(id) ON DELETE CASCADE,
    CONSTRAINT message_identity_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES sn_config.contacts(id)
);
-- The read side walks a person's history by contact across channels.
CREATE INDEX IF NOT EXISTS message_identity_contact_idx ON sn_core.message_identity USING btree (contact_id, resolved_at DESC);

CREATE TABLE IF NOT EXISTS sn_core.node_service_sweeps (
    kind text NOT NULL,
    collected_at timestamp with time zone NOT NULL,
    ok boolean DEFAULT true NOT NULL,
    error text DEFAULT ''::text NOT NULL,
    count integer DEFAULT 0 NOT NULL,
    CONSTRAINT node_service_sweeps_kind_check CHECK ((kind = ANY (ARRAY['container'::text, 'systemd'::text])))
);

CREATE TABLE IF NOT EXISTS sn_core.node_services (
    id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    health text DEFAULT 'warn'::text NOT NULL,
    status text DEFAULT ''::text NOT NULL,
    version text DEFAULT ''::text NOT NULL,
    detail text DEFAULT ''::text NOT NULL,
    collected_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT node_services_health_check CHECK ((health = ANY (ARRAY['ok'::text, 'warn'::text, 'fail'::text]))),
    CONSTRAINT node_services_kind_check CHECK ((kind = ANY (ARRAY['container'::text, 'systemd'::text])))
);

CREATE TABLE IF NOT EXISTS sn_core.tools (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    install_uri text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'available'::text NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT 'system'::text NOT NULL,
    updated_by text DEFAULT 'system'::text NOT NULL,
    integration_slug text,
    input_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    http_method text,
    http_path text,
    last_seen timestamp with time zone,
    mcp_server_id integer,
    tenant_id integer DEFAULT 1 NOT NULL,
    usage_md text,
    CONSTRAINT sn_core_tools_kind_check CHECK ((kind = ANY (ARRAY['cli'::text, 'mcp'::text, 'api'::text, 'builtin'::text]))),
    CONSTRAINT sn_core_tools_status_check CHECK ((status = ANY (ARRAY['available'::text, 'broken'::text, 'stub'::text]))),
    CONSTRAINT tools_http_method_check CHECK (((http_method IS NULL) OR (http_method = ANY (ARRAY['GET'::text, 'POST'::text, 'PUT'::text, 'DELETE'::text, 'PATCH'::text]))))
);
-- Granted to every agent by default, with these caps (lib/default_grants.py):
-- the registry says what a new agent gets, not a list in code.
ALTER TABLE sn_core.tools ADD COLUMN IF NOT EXISTS default_caps text[];
-- What a chat shows while the tool runs ("Searching the web"); title when empty.
-- People read this, never the tool's technical name.
ALTER TABLE sn_core.tools ADD COLUMN IF NOT EXISTS doing text;
-- In every agent's .mcp.json without a grant (messaging, reminders), the same way.
ALTER TABLE sn_config.mcp_servers ADD COLUMN IF NOT EXISTS default_on boolean DEFAULT false NOT NULL;

-- Agent health check results written by the health-checker probe cron.
-- One row per (agent_slug, grant_name, check_type); upserted on each probe run.
-- The panel reads this table read-only; the cron is the only writer.
CREATE TABLE IF NOT EXISTS sn_core.agent_health_checks (
    agent_slug       text        NOT NULL,
    grant_name       text        NOT NULL,
    check_type       text        NOT NULL CHECK (check_type IN ('tool', 'channel')),
    status           text        NOT NULL CHECK (status IN ('ok', 'fail', 'untestable')),
    last_checked_at  timestamptz NOT NULL DEFAULT now(),
    error_detail     text
);

CREATE TABLE IF NOT EXISTS sn_core.workflows (
    id integer GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    name text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'available'::text NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    spec_version text,
    team text,
    definition_path text,
    trigger text,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    usage_md text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT 'system'::text NOT NULL,
    updated_by text DEFAULT 'system'::text NOT NULL,
    tenant_id integer DEFAULT 1 NOT NULL
);

-- The agents the owner took off a flow on the panel. The registrar and the
-- import door grant a flow to the agents named in its blocks each time they
-- run; a row here keeps that grant from coming back. Giving the agent the flow
-- again deletes the row.
CREATE TABLE IF NOT EXISTS sn_config.agent_workflow_revokes (
    agent_id integer NOT NULL,
    workflow_id integer NOT NULL,
    revoked_at timestamptz NOT NULL DEFAULT now(),
    revoked_by text,
    PRIMARY KEY (agent_id, workflow_id)
);

-- ======================================================================
-- Standalone sequences (serial-style columns: channels, user_channels)
-- ======================================================================
CREATE SEQUENCE IF NOT EXISTS sn_config.channels_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

CREATE SEQUENCE IF NOT EXISTS sn_config.user_channels_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE sn_config.channels_id_seq OWNED BY sn_config.channels.id;
ALTER SEQUENCE sn_config.user_channels_id_seq OWNED BY sn_config.user_channels.id;
ALTER TABLE sn_config.channels ALTER COLUMN id SET DEFAULT nextval('sn_config.channels_id_seq'::regclass);
ALTER TABLE sn_config.user_channels ALTER COLUMN id SET DEFAULT nextval('sn_config.user_channels_id_seq'::regclass);

-- ======================================================================
-- Primary key & unique constraints (guarded)
-- ======================================================================
DO $pk$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('sn_config.agent_channels', 'agent_channels_pkey',
             'ALTER TABLE sn_config.agent_channels ADD CONSTRAINT agent_channels_pkey PRIMARY KEY (agent_id, channel_id)'),
            ('sn_config.agent_contacts', 'agent_contacts_pkey',
             'ALTER TABLE sn_config.agent_contacts ADD CONSTRAINT agent_contacts_pkey PRIMARY KEY (agent_id, contact_id)'),
            ('sn_config.projects', 'projects_pkey',
             'ALTER TABLE sn_config.projects ADD CONSTRAINT projects_pkey PRIMARY KEY (id)'),
            ('sn_config.projects', 'projects_slug_key',
             'ALTER TABLE sn_config.projects ADD CONSTRAINT projects_slug_key UNIQUE (slug)'),
            ('sn_config.projects', 'projects_unix_group_key',
             'ALTER TABLE sn_config.projects ADD CONSTRAINT projects_unix_group_key UNIQUE (unix_group)'),
            ('sn_config.project_members', 'project_members_pkey',
             'ALTER TABLE sn_config.project_members ADD CONSTRAINT project_members_pkey PRIMARY KEY (project_id, member_kind, member_id)'),
            ('sn_config.agent_knowledge', 'agent_knowledge_pkey',
             'ALTER TABLE sn_config.agent_knowledge ADD CONSTRAINT agent_knowledge_pkey PRIMARY KEY (id)'),
            ('sn_config.agent_policy_opts', 'agent_policy_opts_agent_id_policy_slug_key',
             'ALTER TABLE sn_config.agent_policy_opts ADD CONSTRAINT agent_policy_opts_agent_id_policy_slug_key UNIQUE (agent_id, policy_slug)'),
            ('sn_config.agent_policy_opts', 'agent_policy_opts_pkey',
             'ALTER TABLE sn_config.agent_policy_opts ADD CONSTRAINT agent_policy_opts_pkey PRIMARY KEY (id)'),
            ('sn_config.agent_references', 'agent_references_pkey',
             'ALTER TABLE sn_config.agent_references ADD CONSTRAINT agent_references_pkey PRIMARY KEY (id)'),
            ('sn_config.agent_tools', 'agent_tools_pkey',
             'ALTER TABLE sn_config.agent_tools ADD CONSTRAINT agent_tools_pkey PRIMARY KEY (agent_id, tool_id)'),
            ('sn_config.agent_workflows', 'agent_workflows_pkey',
             'ALTER TABLE sn_config.agent_workflows ADD CONSTRAINT agent_workflows_pkey PRIMARY KEY (agent_id, workflow_id)'),
            ('sn_config.agents', 'agents_pkey',
             'ALTER TABLE sn_config.agents ADD CONSTRAINT agents_pkey PRIMARY KEY (id)'),
            ('sn_config.aliases', 'aliases_pkey',
             'ALTER TABLE sn_config.aliases ADD CONSTRAINT aliases_pkey PRIMARY KEY (id)'),
            ('sn_config.aliases', 'aliases_tenant_id_alias_key',
             'ALTER TABLE sn_config.aliases ADD CONSTRAINT aliases_tenant_id_alias_key UNIQUE (tenant_id, alias)'),
            ('sn_config.applications', 'applications_pkey',
             'ALTER TABLE sn_config.applications ADD CONSTRAINT applications_pkey PRIMARY KEY (id)'),
            ('sn_config.applications', 'applications_slug_key',
             'ALTER TABLE sn_config.applications ADD CONSTRAINT applications_slug_key UNIQUE (slug)'),
            ('sn_config.budgets', 'budgets_pkey',
             'ALTER TABLE sn_config.budgets ADD CONSTRAINT budgets_pkey PRIMARY KEY (id)'),
            ('sn_config.budgets', 'budgets_scope_type_scope_id_period_key',
             'ALTER TABLE sn_config.budgets ADD CONSTRAINT budgets_scope_type_scope_id_period_key UNIQUE (scope_type, scope_id, period)'),
            ('sn_config.changelog_entries', 'changelog_entries_pkey',
             'ALTER TABLE sn_config.changelog_entries ADD CONSTRAINT changelog_entries_pkey PRIMARY KEY (id)'),
            ('sn_config.channels', 'channels_pkey',
             'ALTER TABLE sn_config.channels ADD CONSTRAINT channels_pkey PRIMARY KEY (id)'),
            ('sn_config.channels', 'channels_slug_key',
             'ALTER TABLE sn_config.channels ADD CONSTRAINT channels_slug_key UNIQUE (slug)'),
            ('sn_config.cli_providers', 'cli_providers_pkey',
             'ALTER TABLE sn_config.cli_providers ADD CONSTRAINT cli_providers_pkey PRIMARY KEY (id)'),
            ('sn_config.cli_providers', 'cli_providers_slug_key',
             'ALTER TABLE sn_config.cli_providers ADD CONSTRAINT cli_providers_slug_key UNIQUE (slug)'),
            ('sn_config.config_audit', 'config_audit_pkey',
             'ALTER TABLE sn_config.config_audit ADD CONSTRAINT config_audit_pkey PRIMARY KEY (id)'),
            ('sn_config.contact_channels', 'contact_channels_pkey',
             'ALTER TABLE sn_config.contact_channels ADD CONSTRAINT contact_channels_pkey PRIMARY KEY (id)'),
            ('sn_config.contact_channels', 'contact_channels_handle_key',
             'ALTER TABLE sn_config.contact_channels ADD CONSTRAINT contact_channels_handle_key UNIQUE (contact_id, channel_id, handle)'),
            ('sn_config.contacts', 'contacts_pkey',
             'ALTER TABLE sn_config.contacts ADD CONSTRAINT contacts_pkey PRIMARY KEY (id)'),
            ('sn_config.department_policies', 'department_policies_pkey',
             'ALTER TABLE sn_config.department_policies ADD CONSTRAINT department_policies_pkey PRIMARY KEY (department_id, policy_slug)'),
            ('sn_config.departments', 'departments_pkey',
             'ALTER TABLE sn_config.departments ADD CONSTRAINT departments_pkey PRIMARY KEY (id)'),
            ('sn_config.departments', 'departments_tenant_id_slug_key',
             'ALTER TABLE sn_config.departments ADD CONSTRAINT departments_tenant_id_slug_key UNIQUE (tenant_id, slug)'),
            ('sn_config.flow_versions', 'flow_versions_pkey',
             'ALTER TABLE sn_config.flow_versions ADD CONSTRAINT flow_versions_pkey PRIMARY KEY (id)'),
            ('sn_config.flows', 'flows_pkey',
             'ALTER TABLE sn_config.flows ADD CONSTRAINT flows_pkey PRIMARY KEY (id)'),
            ('sn_config.flows', 'flows_tenant_id_slug_key',
             'ALTER TABLE sn_config.flows ADD CONSTRAINT flows_tenant_id_slug_key UNIQUE (tenant_id, slug)'),
            ('sn_config.forbidden_actions', 'forbidden_actions_pkey',
             'ALTER TABLE sn_config.forbidden_actions ADD CONSTRAINT forbidden_actions_pkey PRIMARY KEY (key)'),
            ('sn_config.integrations', 'integrations_pkey',
             'ALTER TABLE sn_config.integrations ADD CONSTRAINT integrations_pkey PRIMARY KEY (id)'),
            ('sn_config.integrations', 'integrations_slug_key',
             'ALTER TABLE sn_config.integrations ADD CONSTRAINT integrations_slug_key UNIQUE (slug)'),
            ('sn_config.interactions', 'interactions_from_agent_id_to_agent_id_kind_key',
             'ALTER TABLE sn_config.interactions ADD CONSTRAINT interactions_from_agent_id_to_agent_id_kind_key UNIQUE (from_agent_id, to_agent_id, kind)'),
            ('sn_config.interactions', 'interactions_pkey',
             'ALTER TABLE sn_config.interactions ADD CONSTRAINT interactions_pkey PRIMARY KEY (id)'),
            ('sn_config.mcp_servers', 'mcp_servers_pkey',
             'ALTER TABLE sn_config.mcp_servers ADD CONSTRAINT mcp_servers_pkey PRIMARY KEY (id)'),
            ('sn_config.mcp_servers', 'mcp_servers_slug_key',
             'ALTER TABLE sn_config.mcp_servers ADD CONSTRAINT mcp_servers_slug_key UNIQUE (slug)'),
            ('sn_config.merge_ownership', 'merge_ownership_pkey',
             'ALTER TABLE sn_config.merge_ownership ADD CONSTRAINT merge_ownership_pkey PRIMARY KEY (target_table, column_or_jsonpath)'),
            ('sn_config.model_pricing', 'model_pricing_pkey',
             'ALTER TABLE sn_config.model_pricing ADD CONSTRAINT model_pricing_pkey PRIMARY KEY (id)'),
            ('sn_config.model_pricing', 'model_pricing_provider_model_kind_effective_from_key',
             'ALTER TABLE sn_config.model_pricing ADD CONSTRAINT model_pricing_provider_model_kind_effective_from_key UNIQUE (provider, model, kind, effective_from)'),
            ('sn_config.models', 'models_pkey',
             'ALTER TABLE sn_config.models ADD CONSTRAINT models_pkey PRIMARY KEY (id)'),
            ('sn_config.models', 'models_provider_model_key',
             'ALTER TABLE sn_config.models ADD CONSTRAINT models_provider_model_key UNIQUE (provider, model)'),
            ('sn_config.output_schemas', 'output_schemas_pkey',
             'ALTER TABLE sn_config.output_schemas ADD CONSTRAINT output_schemas_pkey PRIMARY KEY (id)'),
            ('sn_config.policies', 'policies_pkey',
             'ALTER TABLE sn_config.policies ADD CONSTRAINT policies_pkey PRIMARY KEY (id)'),
            ('sn_config.projection_ledger', 'projection_ledger_pkey',
             'ALTER TABLE sn_config.projection_ledger ADD CONSTRAINT projection_ledger_pkey PRIMARY KEY (id)'),
            ('sn_config.projection_ledger', 'projection_ledger_uniq',
             'ALTER TABLE sn_config.projection_ledger ADD CONSTRAINT projection_ledger_uniq UNIQUE NULLS NOT DISTINCT (param_key, target_table, target_pk)'),
            ('sn_config.refs', 'refs_pkey',
             'ALTER TABLE sn_config.refs ADD CONSTRAINT refs_pkey PRIMARY KEY (id)'),
            ('sn_config.runtimes', 'runtimes_pkey',
             'ALTER TABLE sn_config.runtimes ADD CONSTRAINT runtimes_pkey PRIMARY KEY (id)'),
            ('sn_config.runtimes', 'runtimes_slug_key',
             'ALTER TABLE sn_config.runtimes ADD CONSTRAINT runtimes_slug_key UNIQUE (slug)'),
            ('sn_config.session_retention', 'session_retention_pkey',
             'ALTER TABLE sn_config.session_retention ADD CONSTRAINT session_retention_pkey PRIMARY KEY (id)'),
            ('sn_config.session_retention', 'session_retention_scope_type_scope_id_key',
             'ALTER TABLE sn_config.session_retention ADD CONSTRAINT session_retention_scope_type_scope_id_key UNIQUE (scope_type, scope_id)'),
            ('sn_config.session_context_limits', 'session_context_limits_pkey',
             'ALTER TABLE sn_config.session_context_limits ADD CONSTRAINT session_context_limits_pkey PRIMARY KEY (id)'),
            ('sn_config.session_context_limits', 'session_context_limits_scope_key',
             'ALTER TABLE sn_config.session_context_limits ADD CONSTRAINT session_context_limits_scope_key UNIQUE NULLS NOT DISTINCT (scope_type, scope_id)'),
            ('sn_config.specialists', 'specialists_pkey',
             'ALTER TABLE sn_config.specialists ADD CONSTRAINT specialists_pkey PRIMARY KEY (id)'),
            ('sn_config.team_members', 'team_members_pkey',
             'ALTER TABLE sn_config.team_members ADD CONSTRAINT team_members_pkey PRIMARY KEY (team_id, agent_id)'),
            ('sn_config.teams', 'teams_pkey',
             'ALTER TABLE sn_config.teams ADD CONSTRAINT teams_pkey PRIMARY KEY (id)'),
            ('sn_config.teams', 'teams_tenant_id_slug_key',
             'ALTER TABLE sn_config.teams ADD CONSTRAINT teams_tenant_id_slug_key UNIQUE (tenant_id, slug)'),
            ('sn_config.tenant_brand', 'tenant_brand_pkey',
             'ALTER TABLE sn_config.tenant_brand ADD CONSTRAINT tenant_brand_pkey PRIMARY KEY (id)'),
            ('sn_config.tenant_brand', 'tenant_brand_tenant_id_kind_key',
             'ALTER TABLE sn_config.tenant_brand ADD CONSTRAINT tenant_brand_tenant_id_kind_key UNIQUE (tenant_id, kind)'),
            ('sn_config.tenant_profile_history', 'tenant_profile_history_pkey',
             'ALTER TABLE sn_config.tenant_profile_history ADD CONSTRAINT tenant_profile_history_pkey PRIMARY KEY (param_key, version)'),
            ('sn_config.tenant_profile', 'tenant_profile_pkey',
             'ALTER TABLE sn_config.tenant_profile ADD CONSTRAINT tenant_profile_pkey PRIMARY KEY (param_key)'),
            ('sn_config.tenants', 'tenants_pkey',
             'ALTER TABLE sn_config.tenants ADD CONSTRAINT tenants_pkey PRIMARY KEY (id)'),
            ('sn_config.tenants', 'tenants_slug_key',
             'ALTER TABLE sn_config.tenants ADD CONSTRAINT tenants_slug_key UNIQUE (slug)'),
            ('sn_config.tools', 'tools_pkey',
             'ALTER TABLE sn_config.tools ADD CONSTRAINT tools_pkey PRIMARY KEY (id)'),
            ('sn_config.tools', 'tools_slug_key',
             'ALTER TABLE sn_config.tools ADD CONSTRAINT tools_slug_key UNIQUE (slug)'),
            ('sn_config.user_channels', 'user_channels_pkey',
             'ALTER TABLE sn_config.user_channels ADD CONSTRAINT user_channels_pkey PRIMARY KEY (id)'),
            ('sn_config.user_channels', 'user_channels_user_id_channel_slug_key',
             'ALTER TABLE sn_config.user_channels ADD CONSTRAINT user_channels_user_id_channel_slug_key UNIQUE (user_id, channel_slug)'),
            ('sn_config.users', 'users_pkey',
             'ALTER TABLE sn_config.users ADD CONSTRAINT users_pkey PRIMARY KEY (id)'),
            ('sn_config.users', 'users_tenant_id_email_key',
             'ALTER TABLE sn_config.users ADD CONSTRAINT users_tenant_id_email_key UNIQUE (tenant_id, email)'),
            ('sn_core.actors', 'actors_pkey',
             'ALTER TABLE sn_core.actors ADD CONSTRAINT actors_pkey PRIMARY KEY (id)'),
            ('sn_core.actors', 'actors_canonical_id_fkey',
             'ALTER TABLE sn_core.actors ADD CONSTRAINT actors_canonical_id_fkey FOREIGN KEY (canonical_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL'),
            ('sn_core.actors', 'actors_canonical_id_self_chk',
             'ALTER TABLE sn_core.actors ADD CONSTRAINT actors_canonical_id_self_chk CHECK (canonical_id <> id)'),
            ('sn_core.agent_health_checks', 'agent_health_checks_pkey',
             'ALTER TABLE sn_core.agent_health_checks ADD CONSTRAINT agent_health_checks_pkey PRIMARY KEY (agent_slug, grant_name, check_type)'),
            ('sn_core.agent_runtime', 'agent_runtime_pkey',
             'ALTER TABLE sn_core.agent_runtime ADD CONSTRAINT agent_runtime_pkey PRIMARY KEY (agent_slug)'),
            ('sn_core.ai_session_messages', 'ai_session_messages_pkey',
             'ALTER TABLE sn_core.ai_session_messages ADD CONSTRAINT ai_session_messages_pkey PRIMARY KEY (id)'),
            ('sn_core.ai_session_messages', 'ai_session_messages_session_id_seq_key',
             'ALTER TABLE sn_core.ai_session_messages ADD CONSTRAINT ai_session_messages_session_id_seq_key UNIQUE (session_id, seq)'),
            ('sn_core.ai_sessions', 'ai_sessions_pkey',
             'ALTER TABLE sn_core.ai_sessions ADD CONSTRAINT ai_sessions_pkey PRIMARY KEY (id)'),
            ('sn_core.cron_runs', 'cron_runs_pkey',
             'ALTER TABLE sn_core.cron_runs ADD CONSTRAINT cron_runs_pkey PRIMARY KEY (id)'),
            ('sn_core.crons', 'crons_pkey',
             'ALTER TABLE sn_core.crons ADD CONSTRAINT crons_pkey PRIMARY KEY (id)'),
            ('sn_core.crons', 'crons_schedule_command_crontab_user_key',
             'ALTER TABLE sn_core.crons ADD CONSTRAINT crons_schedule_command_crontab_user_key UNIQUE (schedule, command, crontab_user)'),
            ('sn_core.desks', 'desks_pkey',
             'ALTER TABLE sn_core.desks ADD CONSTRAINT desks_pkey PRIMARY KEY (id)'),
            ('sn_core.events', 'events_pkey',
             'ALTER TABLE sn_core.events ADD CONSTRAINT events_pkey PRIMARY KEY (id)'),
            ('sn_core.message_attachments', 'message_attachments_pkey',
             'ALTER TABLE sn_core.message_attachments ADD CONSTRAINT message_attachments_pkey PRIMARY KEY (id)'),
            ('sn_core.message_state', 'message_state_pkey',
             'ALTER TABLE sn_core.message_state ADD CONSTRAINT message_state_pkey PRIMARY KEY (message_id)'),
            ('sn_core.messages', 'messages_pkey',
             'ALTER TABLE sn_core.messages ADD CONSTRAINT messages_pkey PRIMARY KEY (id)'),
            ('sn_core.node_service_sweeps', 'node_service_sweeps_pkey',
             'ALTER TABLE sn_core.node_service_sweeps ADD CONSTRAINT node_service_sweeps_pkey PRIMARY KEY (kind)'),
            ('sn_core.node_services', 'node_services_kind_name_key',
             'ALTER TABLE sn_core.node_services ADD CONSTRAINT node_services_kind_name_key UNIQUE (kind, name)'),
            ('sn_core.node_services', 'node_services_pkey',
             'ALTER TABLE sn_core.node_services ADD CONSTRAINT node_services_pkey PRIMARY KEY (id)'),
            ('sn_core.tools', 'tools_name_key',
             'ALTER TABLE sn_core.tools ADD CONSTRAINT tools_name_key UNIQUE (name)'),
            ('sn_core.tools', 'tools_pkey',
             'ALTER TABLE sn_core.tools ADD CONSTRAINT tools_pkey PRIMARY KEY (id)'),
            ('sn_core.workflows', 'workflows_pkey',
             'ALTER TABLE sn_core.workflows ADD CONSTRAINT workflows_pkey PRIMARY KEY (id)')
        ) AS t(tbl, conname, ddl)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                      WHERE conname = r.conname AND conrelid = r.tbl::regclass) THEN
            EXECUTE r.ddl;
        END IF;
    END LOOP;
END
$pk$;

-- ======================================================================
-- Foreign keys (guarded; added after all PK/UNIQUE exist)
-- ======================================================================
DO $fk$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('sn_config.agent_channels', 'agent_channels_agent_id_fkey',
             'ALTER TABLE sn_config.agent_channels ADD CONSTRAINT agent_channels_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.agent_channels', 'agent_channels_channel_id_fkey',
             'ALTER TABLE sn_config.agent_channels ADD CONSTRAINT agent_channels_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES sn_config.channels(id) ON DELETE CASCADE'),
            ('sn_config.agent_contacts', 'agent_contacts_agent_id_fkey',
             'ALTER TABLE sn_config.agent_contacts ADD CONSTRAINT agent_contacts_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.project_members', 'project_members_project_id_fkey',
             'ALTER TABLE sn_config.project_members ADD CONSTRAINT project_members_project_id_fkey FOREIGN KEY (project_id) REFERENCES sn_config.projects(id) ON DELETE CASCADE'),
            ('sn_config.agent_contacts', 'agent_contacts_contact_id_fkey',
             'ALTER TABLE sn_config.agent_contacts ADD CONSTRAINT agent_contacts_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES sn_config.contacts(id) ON DELETE CASCADE'),
            ('sn_config.contact_channels', 'contact_channels_contact_id_fkey',
             'ALTER TABLE sn_config.contact_channels ADD CONSTRAINT contact_channels_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES sn_config.contacts(id) ON DELETE CASCADE'),
            ('sn_config.contact_channels', 'contact_channels_channel_id_fkey',
             'ALTER TABLE sn_config.contact_channels ADD CONSTRAINT contact_channels_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES sn_config.channels(id) ON DELETE CASCADE'),
            ('sn_config.contacts', 'contacts_tenant_id_fkey',
             'ALTER TABLE sn_config.contacts ADD CONSTRAINT contacts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.agent_knowledge', 'agent_knowledge_agent_id_fkey',
             'ALTER TABLE sn_config.agent_knowledge ADD CONSTRAINT agent_knowledge_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.agent_knowledge', 'agent_knowledge_tenant_id_fkey',
             'ALTER TABLE sn_config.agent_knowledge ADD CONSTRAINT agent_knowledge_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.agent_policy_opts', 'agent_policy_opts_agent_id_fkey',
             'ALTER TABLE sn_config.agent_policy_opts ADD CONSTRAINT agent_policy_opts_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.agent_references', 'agent_references_tenant_fk',
             'ALTER TABLE sn_config.agent_references ADD CONSTRAINT agent_references_tenant_fk FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.agent_tools', 'agent_tools_agent_id_fkey',
             'ALTER TABLE sn_config.agent_tools ADD CONSTRAINT agent_tools_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.agent_tools', 'agent_tools_tool_id_fkey',
             'ALTER TABLE sn_config.agent_tools ADD CONSTRAINT agent_tools_tool_id_fkey FOREIGN KEY (tool_id) REFERENCES sn_core.tools(id) ON DELETE CASCADE'),
            ('sn_config.agent_workflows', 'agent_workflows_agent_id_fkey',
             'ALTER TABLE sn_config.agent_workflows ADD CONSTRAINT agent_workflows_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.agent_workflows', 'agent_workflows_workflow_id_fkey',
             'ALTER TABLE sn_config.agent_workflows ADD CONSTRAINT agent_workflows_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES sn_core.workflows(id) ON DELETE CASCADE'),
            ('sn_config.agent_workflow_revokes', 'agent_workflow_revokes_agent_id_fkey',
             'ALTER TABLE sn_config.agent_workflow_revokes ADD CONSTRAINT agent_workflow_revokes_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.agent_workflow_revokes', 'agent_workflow_revokes_workflow_id_fkey',
             'ALTER TABLE sn_config.agent_workflow_revokes ADD CONSTRAINT agent_workflow_revokes_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES sn_core.workflows(id) ON DELETE CASCADE'),
            ('sn_config.agents', 'agents_department_id_fkey',
             'ALTER TABLE sn_config.agents ADD CONSTRAINT agents_department_id_fkey FOREIGN KEY (department_id) REFERENCES sn_config.departments(id)'),
            ('sn_config.agents', 'agents_specialist_id_fkey',
             'ALTER TABLE sn_config.agents ADD CONSTRAINT agents_specialist_id_fkey FOREIGN KEY (specialist_id) REFERENCES sn_config.specialists(id)'),
            ('sn_config.agents', 'agents_tenant_id_fkey',
             'ALTER TABLE sn_config.agents ADD CONSTRAINT agents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.aliases', 'aliases_tenant_id_fkey',
             'ALTER TABLE sn_config.aliases ADD CONSTRAINT aliases_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.department_policies', 'department_policies_department_id_fkey',
             'ALTER TABLE sn_config.department_policies ADD CONSTRAINT department_policies_department_id_fkey FOREIGN KEY (department_id) REFERENCES sn_config.departments(id) ON DELETE CASCADE'),
            ('sn_config.departments', 'departments_head_agent_fk',
             'ALTER TABLE sn_config.departments ADD CONSTRAINT departments_head_agent_fk FOREIGN KEY (head_agent_id) REFERENCES sn_config.agents(id) ON DELETE SET NULL'),
            ('sn_config.departments', 'departments_tenant_id_fkey',
             'ALTER TABLE sn_config.departments ADD CONSTRAINT departments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.flow_versions', 'flow_versions_flow_id_fkey',
             'ALTER TABLE sn_config.flow_versions ADD CONSTRAINT flow_versions_flow_id_fkey FOREIGN KEY (flow_id) REFERENCES sn_config.flows(id) ON DELETE CASCADE'),
            ('sn_config.flows', 'flows_department_id_fkey',
             'ALTER TABLE sn_config.flows ADD CONSTRAINT flows_department_id_fkey FOREIGN KEY (department_id) REFERENCES sn_config.departments(id)'),
            ('sn_config.flows', 'flows_tenant_id_fkey',
             'ALTER TABLE sn_config.flows ADD CONSTRAINT flows_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.integrations', 'integrations_tenant_id_fkey',
             'ALTER TABLE sn_config.integrations ADD CONSTRAINT integrations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.interactions', 'interactions_from_agent_id_fkey',
             'ALTER TABLE sn_config.interactions ADD CONSTRAINT interactions_from_agent_id_fkey FOREIGN KEY (from_agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.interactions', 'interactions_tenant_id_fkey',
             'ALTER TABLE sn_config.interactions ADD CONSTRAINT interactions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.interactions', 'interactions_to_agent_id_fkey',
             'ALTER TABLE sn_config.interactions ADD CONSTRAINT interactions_to_agent_id_fkey FOREIGN KEY (to_agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.mcp_servers', 'mcp_servers_application_id_fkey',
             'ALTER TABLE sn_config.mcp_servers ADD CONSTRAINT mcp_servers_application_id_fkey FOREIGN KEY (application_id) REFERENCES sn_config.applications(id)'),
            ('sn_config.policies', 'policies_agent_id_fkey',
             'ALTER TABLE sn_config.policies ADD CONSTRAINT policies_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.policies', 'policies_tenant_id_fkey',
             'ALTER TABLE sn_config.policies ADD CONSTRAINT policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.runtimes', 'runtimes_tenant_id_fkey',
             'ALTER TABLE sn_config.runtimes ADD CONSTRAINT runtimes_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.specialists', 'specialists_tenant_id_fkey',
             'ALTER TABLE sn_config.specialists ADD CONSTRAINT specialists_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.team_members', 'team_members_agent_id_fkey',
             'ALTER TABLE sn_config.team_members ADD CONSTRAINT team_members_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id) ON DELETE CASCADE'),
            ('sn_config.team_members', 'team_members_team_id_fkey',
             'ALTER TABLE sn_config.team_members ADD CONSTRAINT team_members_team_id_fkey FOREIGN KEY (team_id) REFERENCES sn_config.teams(id) ON DELETE CASCADE'),
            ('sn_config.teams', 'teams_department_id_fkey',
             'ALTER TABLE sn_config.teams ADD CONSTRAINT teams_department_id_fkey FOREIGN KEY (department_id) REFERENCES sn_config.departments(id)'),
            ('sn_config.teams', 'teams_tenant_id_fkey',
             'ALTER TABLE sn_config.teams ADD CONSTRAINT teams_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.tenant_brand', 'tenant_brand_tenant_id_fkey',
             'ALTER TABLE sn_config.tenant_brand ADD CONSTRAINT tenant_brand_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_config.tools', 'tools_application_id_fkey',
             'ALTER TABLE sn_config.tools ADD CONSTRAINT tools_application_id_fkey FOREIGN KEY (application_id) REFERENCES sn_config.applications(id)'),
            ('sn_config.user_channels', 'user_channels_user_id_fkey',
             'ALTER TABLE sn_config.user_channels ADD CONSTRAINT user_channels_user_id_fkey FOREIGN KEY (user_id) REFERENCES sn_config.users(id)'),
            ('sn_config.users', 'users_tenant_id_fkey',
             'ALTER TABLE sn_config.users ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.ai_session_messages', 'ai_session_messages_session_id_fkey',
             'ALTER TABLE sn_core.ai_session_messages ADD CONSTRAINT ai_session_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES sn_core.ai_sessions(id) ON DELETE CASCADE'),
            ('sn_core.ai_sessions', 'ai_sessions_agent_id_fkey',
             'ALTER TABLE sn_core.ai_sessions ADD CONSTRAINT ai_sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES sn_config.agents(id)'),
            ('sn_core.ai_sessions', 'ai_sessions_parent_session_id_fkey',
             'ALTER TABLE sn_core.ai_sessions ADD CONSTRAINT ai_sessions_parent_session_id_fkey FOREIGN KEY (parent_session_id) REFERENCES sn_core.ai_sessions(id)'),
            ('sn_core.ai_sessions', 'ai_sessions_tenant_id_fkey',
             'ALTER TABLE sn_core.ai_sessions ADD CONSTRAINT ai_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.cron_runs', 'cron_runs_cron_id_fkey',
             'ALTER TABLE sn_core.cron_runs ADD CONSTRAINT cron_runs_cron_id_fkey FOREIGN KEY (cron_id) REFERENCES sn_core.crons(id) ON DELETE CASCADE'),
            ('sn_core.cron_runs', 'cron_runs_tenant_id_fkey',
             'ALTER TABLE sn_core.cron_runs ADD CONSTRAINT cron_runs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.crons', 'crons_tenant_id_fkey',
             'ALTER TABLE sn_core.crons ADD CONSTRAINT crons_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.desks', 'desks_owner_user_id_fkey',
             'ALTER TABLE sn_core.desks ADD CONSTRAINT desks_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES sn_config.users(id)'),
            ('sn_core.desks', 'desks_tenant_id_fkey',
             'ALTER TABLE sn_core.desks ADD CONSTRAINT desks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.events', 'events_actor_fkey',
             'ALTER TABLE sn_core.events ADD CONSTRAINT events_actor_fkey FOREIGN KEY (actor) REFERENCES sn_core.actors(id)'),
            ('sn_core.events', 'events_parent_event_id_fkey',
             'ALTER TABLE sn_core.events ADD CONSTRAINT events_parent_event_id_fkey FOREIGN KEY (parent_event_id) REFERENCES sn_core.events(id)'),
            ('sn_core.events', 'events_tenant_id_fkey',
             'ALTER TABLE sn_core.events ADD CONSTRAINT events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.message_attachments', 'message_attachments_message_id_fkey',
             'ALTER TABLE sn_core.message_attachments ADD CONSTRAINT message_attachments_message_id_fkey FOREIGN KEY (message_id) REFERENCES sn_core.messages(id) ON DELETE CASCADE'),
            -- a desk file leaves with its desk (the rows here; the
            -- bytes are removed by DELETE /api/desks/{id}), and a row has
            -- exactly one parent: a message, a desk or a planner task.
            ('sn_core.message_attachments', 'message_attachments_desk_id_fkey',
             'ALTER TABLE sn_core.message_attachments ADD CONSTRAINT message_attachments_desk_id_fkey FOREIGN KEY (desk_id) REFERENCES sn_core.desks(id) ON DELETE CASCADE'),
            ('sn_core.message_attachments', 'message_attachments_one_parent_chk',
             'ALTER TABLE sn_core.message_attachments ADD CONSTRAINT message_attachments_one_parent_chk CHECK (num_nonnulls(message_id, desk_id, task_id) = 1)'),
            ('sn_core.message_state', 'message_state_message_id_fkey',
             'ALTER TABLE sn_core.message_state ADD CONSTRAINT message_state_message_id_fkey FOREIGN KEY (message_id) REFERENCES sn_core.messages(id)'),
            ('sn_core.messages', 'messages_ai_session_id_fkey',
             'ALTER TABLE sn_core.messages ADD CONSTRAINT messages_ai_session_id_fkey FOREIGN KEY (ai_session_id) REFERENCES sn_core.ai_sessions(id)'),
            ('sn_core.messages', 'messages_tenant_id_fkey',
             'ALTER TABLE sn_core.messages ADD CONSTRAINT messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.tools', 'sn_core_tools_tenant_id_fkey',
             'ALTER TABLE sn_core.tools ADD CONSTRAINT sn_core_tools_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES sn_config.tenants(id)'),
            ('sn_core.tools', 'tools_integration_slug_fkey',
             'ALTER TABLE sn_core.tools ADD CONSTRAINT tools_integration_slug_fkey FOREIGN KEY (integration_slug) REFERENCES sn_config.integrations(slug) ON UPDATE CASCADE ON DELETE SET NULL'),
            ('sn_core.tools', 'tools_mcp_server_id_fkey',
             'ALTER TABLE sn_core.tools ADD CONSTRAINT tools_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES sn_config.mcp_servers(id) ON DELETE SET NULL')
        ) AS t(tbl, conname, ddl)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                      WHERE conname = r.conname AND conrelid = r.tbl::regclass) THEN
            EXECUTE r.ddl;
        END IF;
    END LOOP;
END
$fk$;

-- ======================================================================
-- Migration-delivered FK constraints (column-existence guarded)
-- ======================================================================
-- actor_id columns on agents, users, and contacts arrive via migrations
-- 036, 034, and 037 respectively (Phase 2, after Layer C). On a clean
-- install B02's CREATE TABLE already includes the column, so the guard
-- runs and the constraint is added here. On an update path the column is
-- absent until the migration runs, so the guard skips and the migration
-- adds the constraint itself (each migration has its own DO block for this).
-- Re-run safety comes from the pg_constraint check in each guard below:
-- PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS, and using it made B02
-- fail with a syntax error on every path.
--
DO $agents_actor_id_fk_guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'sn_config'
           AND table_name   = 'agents'
           AND column_name  = 'actor_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname  = 'agents_actor_id_fkey'
           AND conrelid = 'sn_config.agents'::regclass
    ) THEN
        -- ON DELETE SET NULL so removing an actor row
        -- does not cascade-delete the agent row.
        ALTER TABLE sn_config.agents
            ADD CONSTRAINT agents_actor_id_fkey
            FOREIGN KEY (actor_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL;
    END IF;
END
$agents_actor_id_fk_guard$;

DO $users_actor_id_fk_guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'sn_config'
           AND table_name   = 'users'
           AND column_name  = 'actor_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname  = 'users_actor_id_fkey'
           AND conrelid = 'sn_config.users'::regclass
    ) THEN
        -- ON DELETE SET NULL so removing an actor row
        -- does not cascade-delete the user account.
        ALTER TABLE sn_config.users
            ADD CONSTRAINT users_actor_id_fkey
            FOREIGN KEY (actor_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL;
    END IF;
END
$users_actor_id_fk_guard$;

DO $contacts_actor_id_fk_guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'sn_config'
           AND table_name   = 'contacts'
           AND column_name  = 'actor_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname  = 'contacts_actor_id_fkey'
           AND conrelid = 'sn_config.contacts'::regclass
    ) THEN
        -- ON DELETE SET NULL so removing an actor row
        -- does not cascade-delete the contact row.
        ALTER TABLE sn_config.contacts
            ADD CONSTRAINT contacts_actor_id_fkey
            FOREIGN KEY (actor_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL;
    END IF;
END
$contacts_actor_id_fk_guard$;

-- ======================================================================
-- Indexes (non-constraint)
-- ======================================================================
CREATE UNIQUE INDEX IF NOT EXISTS agent_references_tenant_slug_live ON sn_config.agent_references USING btree (tenant_id, slug) WHERE (archived_at IS NULL);
CREATE UNIQUE INDEX IF NOT EXISTS agents_slug_active ON sn_config.agents USING btree (tenant_id, slug) WHERE (archived_at IS NULL);
-- partial index for actor lookups; NULL until migration 036 runs.
DO $agents_actor_idx_guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'sn_config'
           AND table_name   = 'agents'
           AND column_name  = 'actor_id'
    ) THEN
        CREATE INDEX IF NOT EXISTS agents_actor_idx ON sn_config.agents USING btree (actor_id) WHERE (actor_id IS NOT NULL);
    END IF;
END
$agents_actor_idx_guard$;
-- Enforce the invariant that each active agent has a
-- distinct actor identity. The existing agents_actor_idx is a plain index; a
-- duplicate actor_id was accepted silently, which allowed the onboarding seed to
-- create a second setup agent sharing the identity of a renamed one. A unique
-- partial index on active rows makes the database refuse the duplicate insert.
-- NULL actor_id (before migration 036) is excluded -- the constraint cannot reach
-- rows it cannot see.
DO $agents_actor_id_active_unique_guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'sn_config'
           AND table_name   = 'agents'
           AND column_name  = 'actor_id'
    ) THEN
        CREATE UNIQUE INDEX IF NOT EXISTS agents_actor_id_active_unique
            ON sn_config.agents(actor_id)
            WHERE actor_id IS NOT NULL AND archived_at IS NULL;
    END IF;
END
$agents_actor_id_active_unique_guard$;
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_agent ON sn_config.agent_knowledge USING btree (agent_id) WHERE (archived_at IS NULL);
CREATE INDEX IF NOT EXISTS idx_policies_agent ON sn_config.policies USING btree (agent_id) WHERE (archived_at IS NULL);
CREATE INDEX IF NOT EXISTS integrations_companion_slug_idx ON sn_config.integrations USING btree (companion_slug);
CREATE INDEX IF NOT EXISTS integrations_status_idx ON sn_config.integrations USING btree (status);
CREATE INDEX IF NOT EXISTS policies_category_enabled ON sn_config.policies USING btree (category, enabled) WHERE (archived_at IS NULL);
CREATE UNIQUE INDEX IF NOT EXISTS policies_slug_active ON sn_config.policies USING btree (COALESCE(tenant_id, 0), COALESCE(agent_id, 0), slug) WHERE (archived_at IS NULL);
CREATE INDEX IF NOT EXISTS projection_ledger_param_idx ON sn_config.projection_ledger USING btree (param_key);
CREATE INDEX IF NOT EXISTS refs_scope_key ON sn_config.refs USING btree (scope, scope_id, key);
CREATE INDEX IF NOT EXISTS runtimes_companion_slug_idx ON sn_config.runtimes USING btree (companion_slug);
CREATE INDEX IF NOT EXISTS runtimes_kind_idx ON sn_config.runtimes USING btree (kind);
CREATE INDEX IF NOT EXISTS runtimes_status_idx ON sn_config.runtimes USING btree (status);
CREATE UNIQUE INDEX IF NOT EXISTS specialists_slug_active ON sn_config.specialists USING btree (COALESCE(tenant_id, 0), slug) WHERE (archived_at IS NULL);
CREATE INDEX IF NOT EXISTS tenant_profile_behavior_idx ON sn_config.tenant_profile USING btree (behavior);
CREATE INDEX IF NOT EXISTS tenant_profile_status_idx ON sn_config.tenant_profile USING btree (status);
CREATE UNIQUE INDEX IF NOT EXISTS crons_slug_uniq ON sn_core.crons USING btree (slug);
CREATE INDEX IF NOT EXISTS desks_owner_idx ON sn_core.desks USING btree (tenant_id, owner_user_id, status);
CREATE INDEX IF NOT EXISTS events_actor_idx ON sn_core.events USING btree (actor);
CREATE INDEX IF NOT EXISTS events_entity_idx ON sn_core.events USING btree (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS events_ts_idx ON sn_core.events USING btree (ts);
CREATE INDEX IF NOT EXISTS events_type_idx ON sn_core.events USING btree (event_type);
CREATE INDEX IF NOT EXISTS idx_messages_ai_session ON sn_core.messages USING btree (ai_session_id) WHERE (ai_session_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_tenant ON sn_core.messages USING btree (tenant_id);
CREATE INDEX IF NOT EXISTS idx_msgs_role ON sn_core.ai_session_messages USING btree (session_id, role);
CREATE INDEX IF NOT EXISTS idx_msgs_session_seq ON sn_core.ai_session_messages USING btree (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_pub_events_tenant ON sn_core.events USING btree (tenant_id);
CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sn_core.ai_sessions USING btree (last_activity_at) WHERE (status = ANY (ARRAY['active'::text, 'streaming'::text]));
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sn_core.ai_sessions USING btree (agent_id) WHERE (agent_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_budget ON sn_core.ai_sessions USING btree (budget_key, created_at) WHERE (budget_key IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sn_core.ai_sessions USING btree (parent_session_id) WHERE (parent_session_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_provider ON sn_core.ai_sessions USING btree (provider, status);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_status ON sn_core.ai_sessions USING btree (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_sn_cron_runs_cron_started ON sn_core.cron_runs USING btree (cron_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sn_cron_runs_tenant ON sn_core.cron_runs USING btree (tenant_id);
CREATE INDEX IF NOT EXISTS idx_sn_crons_enabled ON sn_core.crons USING btree (enabled);
CREATE INDEX IF NOT EXISTS idx_sn_crons_group ON sn_core.crons USING btree ("group");
CREATE INDEX IF NOT EXISTS idx_sn_crons_tenant ON sn_core.crons USING btree (tenant_id);
CREATE INDEX IF NOT EXISTS idx_sn_tools_mcp_server_id ON sn_core.tools USING btree (mcp_server_id) WHERE (mcp_server_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sn_tools_tenant ON sn_core.tools USING btree (tenant_id);
CREATE INDEX IF NOT EXISTS message_attachments_message_id_idx ON sn_core.message_attachments USING btree (message_id);
-- A desk's files are listed, capped and deleted by desk.
CREATE INDEX IF NOT EXISTS message_attachments_desk_id_idx ON sn_core.message_attachments USING btree (desk_id) WHERE (desk_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS message_attachments_task_id_idx ON sn_core.message_attachments USING btree (task_id) WHERE (task_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS message_attachments_status_received_at_idx ON sn_core.message_attachments USING btree (status, received_at) WHERE (status = 'received'::text);
-- Idempotent re-ingest: the same bytes under the same filename on one message
-- is one attachment and not two. This is what makes a crash between the
-- message commit and mark_seen safe.
CREATE UNIQUE INDEX IF NOT EXISTS message_attachments_dedup_idx ON sn_core.message_attachments USING btree (message_id, content_hash, filename) WHERE (content_hash IS NOT NULL AND filename IS NOT NULL);
CREATE INDEX IF NOT EXISTS message_state_status_idx ON sn_core.message_state USING btree (status, updated_at);
-- The Runner's poll looks for overdue reply obligations every few seconds;
-- this keeps that look proportional to the open ones, not to the table.
CREATE INDEX IF NOT EXISTS message_state_awaiting_reply_idx ON sn_core.message_state USING btree (wake_next_at) WHERE (status = 'awaiting_reply'::text);
CREATE INDEX IF NOT EXISTS messages_channel_idx ON sn_core.messages USING btree (channel_type, channel_id);
CREATE UNIQUE INDEX IF NOT EXISTS messages_external_uq ON sn_core.messages USING btree (channel_type, external_id);
CREATE INDEX IF NOT EXISTS messages_received_idx ON sn_core.messages USING btree (received_at);
CREATE INDEX IF NOT EXISTS messages_thread_idx ON sn_core.messages USING btree (thread_id);
CREATE INDEX IF NOT EXISTS tools_integration_slug_idx ON sn_core.tools USING btree (integration_slug);
CREATE INDEX IF NOT EXISTS tools_kind_idx ON sn_core.tools USING btree (kind);
CREATE INDEX IF NOT EXISTS tools_status_idx ON sn_core.tools USING btree (status);
CREATE UNIQUE INDEX IF NOT EXISTS workflows_name_uq ON sn_core.workflows USING btree (tenant_id, name);
-- The communication gate matches a message recipient against a granted handle.
-- Both sides are normalised inside the comparison (lower(handle) = lower(extracted)),
-- so this index has to be functional or the gate never uses it.
CREATE INDEX IF NOT EXISTS contact_channels_match_idx ON sn_config.contact_channels USING btree (channel_id, lower(handle));
CREATE INDEX IF NOT EXISTS contact_channels_contact_idx ON sn_config.contact_channels USING btree (contact_id);
CREATE INDEX IF NOT EXISTS contacts_tenant_idx ON sn_config.contacts USING btree (tenant_id);
DO $contacts_actor_idx_guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'sn_config'
           AND table_name   = 'contacts'
           AND column_name  = 'actor_id'
    ) THEN
        CREATE INDEX IF NOT EXISTS contacts_actor_idx ON sn_config.contacts USING btree (actor_id) WHERE (actor_id IS NOT NULL);
    END IF;
END
$contacts_actor_idx_guard$;

-- ======================================================================
-- Triggers (immutability / seed-state on sn_core mirror tables).
-- The public seed-state function writes public.message_state. A sn_core
-- message must seed sn_core.message_state or its FK points at the wrong row.
-- ======================================================================
CREATE OR REPLACE TRIGGER events_no_update_delete BEFORE DELETE OR UPDATE ON sn_core.events FOR EACH ROW EXECUTE FUNCTION public.events_immutable();
CREATE OR REPLACE TRIGGER message_state_guard_trg BEFORE DELETE OR UPDATE ON sn_core.message_state FOR EACH ROW EXECUTE FUNCTION public.message_state_guard();
CREATE OR REPLACE TRIGGER messages_no_update_delete BEFORE DELETE OR UPDATE ON sn_core.messages FOR EACH ROW EXECUTE FUNCTION public.messages_immutable();
CREATE OR REPLACE FUNCTION sn_core.messages_seed_state() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $$
BEGIN
    INSERT INTO sn_core.message_state (message_id, status) VALUES (NEW.id, 'new');
    RETURN NULL;
END;
$$;
CREATE OR REPLACE TRIGGER messages_seed_state_trg AFTER INSERT ON sn_core.messages FOR EACH ROW EXECUTE FUNCTION sn_core.messages_seed_state();


-- ======================================================================
-- Functions (sn_core) -- the agent lock, one implementation
-- ======================================================================
-- The immutability functions live in public and are created by B02-schema.sql.
-- These two are different and belong here: they operate on sn_core.agent_runtime
-- and they exist so that a SECOND service can take the lock without owning a
-- copy of the rule.
--
-- WHY. Two services decide to run an agent: the Runner wakes one that
-- has pending work, and the communication dispatcher answers a chat message on a
-- fast channel. The Runner claimed the lock through core-api; the dispatcher did
-- not, because it talks to brin and never had a reason to know this table
-- existed. So the two paths ran the same agent at the same time. Seen live on
-- a node in the field: the owner asked in telegram how much
-- RAM the node had, the wake carrying the answer was already running, and he got
-- the figures twice in two languages with "I have no direct access to system
-- metrics" in between -- said by the turn that did not know the other one was at
-- that moment carrying exactly that access.
--
-- The lock was right. What was missing was a second honest caller. The rule
-- moved into the database rather than behind an HTTP call because the dispatcher
-- writes to this database directly and has no core-api URL: an HTTP claim would
-- have meant a new config knob AND a second implementation of "may these run at
-- once" -- which is the very thing that produced the defect.
--
-- Semantics are unchanged from the core-api route that used to hold them:
--   claim   -- atomic; succeeds when free, when the lease has expired, or when
--              the agent is 'broken' (so a retry can revive one that a past wake
--              failure left dead). FAIL-CLOSED: false means do not run, and it
--              returns false rather than NULL so a caller writing
--              `if not claimed` need not know which it got.
--   release -- back to free, reporting whether work arrived DURING the run
--              (wake_requested_at moved away from the snapshot claim took), so
--              the caller can leave the flag standing rather than wiping work it
--              never saw.
--
-- SECURITY DEFINER because the dispatcher runs as its own least-privilege role
-- with no grants on sn_core.agent_runtime, and granting it UPDATE there to take
-- a lock would hand it everything else on that table too. EXECUTE is granted to
-- that role by companions/communication.sh, where the rest of its grants live --
-- this file states in its own header that it carries no GRANTs.
-- ======================================================================
CREATE OR REPLACE FUNCTION sn_core.claim_agent(
    p_slug          text,
    p_ref           text,
    p_lease_seconds integer DEFAULT 900
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $claim_agent$
DECLARE
    v_got boolean;
BEGIN
    INSERT INTO sn_core.agent_runtime (agent_slug, status, current_ref, lease_until, updated_at)
    VALUES (p_slug, 'busy', p_ref, now() + (p_lease_seconds || ' seconds')::interval, now())
    ON CONFLICT (agent_slug) DO UPDATE SET
        status       = 'busy',
        current_ref  = EXCLUDED.current_ref,
        lease_until  = EXCLUDED.lease_until,
        last_error   = NULL,
        updated_at   = now(),
        -- Snapshot the wake flag as it stands at the moment of the claim, so
        -- release can tell "nothing new arrived" from "work landed mid-run".
        wake_seen_at = sn_core.agent_runtime.wake_requested_at
    WHERE sn_core.agent_runtime.status = 'free'
       OR (sn_core.agent_runtime.status = 'busy' AND sn_core.agent_runtime.lease_until < now())
       OR sn_core.agent_runtime.status = 'broken'
    RETURNING true INTO v_got;

    RETURN coalesce(v_got, false);
END;
$claim_agent$;

CREATE OR REPLACE FUNCTION sn_core.release_agent(p_slug text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $release_agent$
DECLARE
    v_still_pending boolean;
BEGIN
    UPDATE sn_core.agent_runtime SET
        status            = 'free',
        current_ref       = NULL,
        lease_until       = NULL,
        last_error        = NULL,
        updated_at        = now(),
        wake_requested_at = CASE WHEN wake_requested_at IS NOT DISTINCT FROM wake_seen_at
                                 THEN NULL ELSE wake_requested_at END,
        wake_seen_at      = NULL
    WHERE agent_slug = p_slug
    RETURNING (wake_requested_at IS NOT NULL) INTO v_still_pending;

    RETURN coalesce(v_still_pending, false);
END;
$release_agent$;


-- ======================================================================
-- Functions (sn_core) -- who an inbound message is from, decided once
-- ======================================================================
-- The read side shipped without its corresponding write side.
-- GET /api/messages/by-contact answers "everything this person said, on every
-- channel" by looking their handles up in sn_config.contact_channels, and it
-- works. But nothing ever wrote sn_core.message_identity, and nothing ever
-- ADDED a handle to the map: it grew only when a person typed into the panel.
-- Measured here 2026-09-10: 0 identity rows against 1596 inbound messages.
-- Reported from a node in the field, where the same gap left one
-- person spelled three ways across voice and web-chat and recognised as none
-- of them.
--
-- WHY THE RULE IS IN THE DATABASE. Both writers need the SAME answer and they
-- are different deployments: core-api owns the web-chat, voice and API
-- ingests, while the communication companion's adapters (email, telegram,
-- slack) write their rows straight to this database and share no Python with
-- it. A rule that must not drift belongs where both can call it.
--
-- Resolution, most direct first:
--   map     -- sn_config.contact_channels, the one handle map.
--   account -- sn_config.users whose email or handle IS this handle and whose
--              contact_id is set. The bridge migration 026 left open when it
--              deferred repointing readers.
--
-- Trust ranks the DOOR above the map: the map says who a handle BELONGS TO,
-- the door says who was ACTUALLY HOLDING it. `proof` is what the door
-- established -- 'session' for an authenticated caller, 'dkim' for a verified
-- sender domain -- and never what the message claimed about itself.
--
-- Never raises on an unknown sender: 'declared' is a verdict too, and that row
-- is what tells an operator which handle is missing from the map.
--
-- THE MAP LEARNS EXACTLY ONE WAY: a handle that arrived on a session-proved
-- door and resolved through the account is inserted with verified_by='session'.
-- That is what makes the next channel recognise the person without anyone
-- maintaining a list by hand. A handle that merely claimed a name is recorded
-- as 'declared' and the map is left alone -- a claim in a message body proves
-- nothing.
--
-- SECURITY DEFINER: the adapters run as their own least-privilege role, granted
-- SELECT/INSERT on message_identity only. Touching the handle map is part of
-- the rule, not part of what an adapter may do on its own, so the rule runs as
-- its owner and the adapter keeps its narrow grants.
-- ======================================================================
CREATE OR REPLACE FUNCTION sn_core.stamp_message_identity(
    p_message_id  bigint,
    p_channel     text,
    p_handle      text,
    p_proof       text DEFAULT NULL,
    p_evidence    jsonb DEFAULT '{}'::jsonb
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $stamp_message_identity$
DECLARE
    v_norm    text;
    v_contact integer;
    v_how     text := 'unknown';
    v_trust   text;
    -- What is ON RECORD, which is not always what this call computed. The row
    -- is written once and never revised, so a second call for the same message
    -- must report the verdict that stands rather than the one it would have
    -- written. Returning the computed value there would let a caller log a
    -- verdict the table does not hold.
    v_stored  text;
BEGIN
    v_norm := lower(btrim(coalesce(p_handle, '')));
    IF v_norm = '' THEN
        INSERT INTO sn_core.message_identity (message_id, contact_id, handle, trust, evidence)
        VALUES (p_message_id, NULL, '', 'unknown',
                coalesce(p_evidence, '{}'::jsonb) || jsonb_build_object('resolved_by', 'unknown'))
        ON CONFLICT (message_id) DO NOTHING
        RETURNING trust INTO v_stored;
        IF v_stored IS NULL THEN
            SELECT trust INTO v_stored FROM sn_core.message_identity
             WHERE message_id = p_message_id;
        END IF;
        RETURN coalesce(v_stored, 'unknown');
    END IF;

    SELECT cc.contact_id INTO v_contact
      FROM sn_config.contact_channels cc
      JOIN sn_config.channels ch ON ch.id = cc.channel_id
     WHERE ch.slug = p_channel AND lower(cc.handle) = v_norm AND cc.enabled
     LIMIT 1;
    IF FOUND THEN
        v_how := 'map';
    ELSE
        SELECT u.contact_id INTO v_contact
          FROM sn_config.users u
         WHERE u.contact_id IS NOT NULL
           AND (lower(u.email) = v_norm OR lower(u.handle) = v_norm)
         LIMIT 1;
        IF FOUND THEN
            v_how := 'account';
        END IF;
    END IF;

    v_trust := CASE
        WHEN p_proof = 'session'              THEN 'session'
        WHEN p_proof IN ('dkim', 'proved')    THEN 'proved'
        WHEN v_how IN ('map', 'account')      THEN 'linked'
        ELSE 'declared'
    END;

    INSERT INTO sn_core.message_identity (message_id, contact_id, handle, trust, evidence)
    VALUES (p_message_id, v_contact, v_norm, v_trust,
            coalesce(p_evidence, '{}'::jsonb)
              || jsonb_build_object('resolved_by', v_how)
              || CASE WHEN p_proof IS NULL THEN '{}'::jsonb
                      ELSE jsonb_build_object('door', p_proof) END)
    ON CONFLICT (message_id) DO NOTHING
    RETURNING trust INTO v_stored;
    IF v_stored IS NULL THEN
        SELECT trust INTO v_stored FROM sn_core.message_identity
         WHERE message_id = p_message_id;
    END IF;
    v_trust := coalesce(v_stored, v_trust);

    IF v_contact IS NULL THEN
        RETURN v_trust;
    END IF;

    -- The map remembers when it last saw this handle, which is what makes a
    -- stale binding visible instead of merely old.
    UPDATE sn_config.contact_channels cc
       SET last_seen_at = now()
      FROM sn_config.channels ch
     WHERE ch.id = cc.channel_id AND ch.slug = p_channel
       AND cc.contact_id = v_contact AND lower(cc.handle) = v_norm;

    IF v_how = 'account' AND p_proof = 'session' THEN
        INSERT INTO sn_config.contact_channels
            (contact_id, channel_id, handle, enabled, verified_by, verified_at, last_seen_at)
        SELECT v_contact, ch.id, v_norm, true, 'session', now(), now()
          FROM sn_config.channels ch
         WHERE ch.slug = p_channel
           AND NOT EXISTS (SELECT 1 FROM sn_config.contact_channels x
                            WHERE x.contact_id = v_contact AND x.channel_id = ch.id
                              AND lower(x.handle) = v_norm);
    END IF;

    RETURN v_trust;
END;
$stamp_message_identity$;


-- ======================================================================
-- Views
-- ======================================================================
-- v_actor_events resolves alias actor ids to their canonical counterpart.
-- Use canonical_actor (not actor) when grouping events by who acted.
-- Scaffolding removed in step 9.
CREATE OR REPLACE VIEW sn_core.v_actor_events AS
SELECT e.*,
       COALESCE(a.canonical_id, e.actor) AS canonical_actor
  FROM sn_core.events e
  LEFT JOIN sn_core.actors a ON a.id = e.actor;


-- ======================================================================
-- Retired duplicates -- one home per class
-- ======================================================================
-- Three tables above are created and then removed here, on purpose.
--
-- This file is a baked end state, close to a dump, and the CREATEs are woven
-- through it together with their indexes and their entries in the constraint
-- repair table below. Removing them in six scattered places is a larger and
-- riskier edit than retiring them in one visible block. When this file is next
-- regenerated, drop the CREATEs instead and delete this section.
--
-- WHY THEY GO. Each duplicated the NAME of a live table in another schema while
-- holding nothing, and a same-named empty twin is worse than no table: it reads
-- as the home of a class and answers every existence check.
--
--   sn_config.tools          the catalogue is sn_core.tools (38 rows on one
--                            node, 123 on another). sn_config.agent_tools.tool_id
--                            references sn_core.tools, not this.
--   sn_config.flows          the catalogue is sn_core.workflows, registered from
--   sn_config.flow_versions  a flow YAML by register-workflows.py.
--                            sn_config.agent_workflows.workflow_id references
--                            sn_core.workflows, not these.
--
-- This file's own header has named the sn_config.tools / sn_core.tools collision
-- since it was written; documenting an ambiguity is not the same as ending one.
-- docs/node/entities.md carried sn_config.flows as the Workflow registry, and
-- that wrong cell was written by someone reading the empty twin.
--
-- SAFETY. Refuses to drop a table that holds rows: on any node where these are
-- not empty, something was written that this reasoning did not anticipate, and
-- the operator should see it rather than lose it. flow_versions goes first
-- because it carries the only foreign key into flows.
DO $retire$
DECLARE
    t   text;
    n   bigint;
    kept text[] := '{}';
BEGIN
    FOREACH t IN ARRAY ARRAY['sn_config.flow_versions','sn_config.flows','sn_config.tools']
    LOOP
        IF to_regclass(t) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
        IF n = 0 THEN
            EXECUTE format('DROP TABLE %s', t);
            RAISE NOTICE 'retired duplicate %', t;
        ELSE
            kept := kept || t;
            RAISE WARNING 'KEPT % -- it holds % row(s); a duplicate that is in use is a finding, not a cleanup', t, n;
        END IF;
    END LOOP;

    IF cardinality(kept) > 0 THEN
        RAISE WARNING 'one home per class is NOT restored for: %', array_to_string(kept, ', ');
    END IF;
END
$retire$;
-- ======================================================================
-- Identity backfill: give existing rows the actor identity the release
-- promised them
-- ======================================================================
-- The actor_id columns are baked into this file, but nothing populates
-- them on a node that already exists. companions/core-api/migrations/ is
-- reference only -- core-api.sh states it outright ("nothing here applies
-- it") and no bootstrap or companion code runs that directory -- so the
-- data steps of migrations 034 and 036 never execute anywhere. A node
-- updated to 1.12.33+ therefore carries the identity schema with every
-- actor_id NULL, which is the one thing the release exists to prevent.
--
-- What follows is those data steps, generalised and made idempotent:
-- provision an actor row per active agent, then resolve actor_id for
-- agents and user accounts. Every statement is guarded on actor_id IS NULL
-- or NOT EXISTS, so re-running is a no-op and a freshly installed node
-- (where these are already populated) is untouched.
--
-- Deliberately NOT included:
--   * migration 033's alias mapping. It hardcodes one node's history --
--     specific slugs, specific event counts -- and cannot be generalised.
--     Nodes that need it should keep running it by hand.
--   * migration 037's contacts population. Its rule is literally
--     WHERE id = 1 and WHERE id = 2. There is no portable equivalent, so
--     contacts.actor_id is left NULL here rather than guessed at.
--   * the NOT NULL steps. Applying a constraint during an update, on data
--     nobody has looked at, is not this file's call. Note also that
--     036 step 5 counts NULLs across ALL agents while step 4 populates
--     only the unarchived ones, so any archived agent row makes it raise.

-- 1. An actor row for every active agent. type/layer match _LAYER_BY_PREFIX
--    in events_emit.py (prefix 'agent' -> layer 'specialist'), same as
--    migration 036 step 3.
INSERT INTO sn_core.actors (id, display_name, type, layer)
SELECT 'agent:' || g.slug,
       COALESCE(g.title, initcap(g.slug)),
       'agent',
       'specialist'
  FROM sn_config.agents g
 WHERE g.archived_at IS NULL
   AND NOT EXISTS (
       SELECT 1 FROM sn_core.actors a WHERE a.id = 'agent:' || g.slug
   );

-- 2. actor_id on active agents (migration 036 step 4). COALESCE resolves
--    through an alias when migration 033 has been run on this node, and is
--    a plain identity when it has not.
UPDATE sn_config.agents g
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'agent:' || g.slug
   AND g.archived_at IS NULL
   AND g.actor_id IS NULL;

-- 3. actor_id on user accounts (migration 034 step 4a/4b/4c, unchanged).
--    A row whose actor does not exist keeps actor_id NULL rather than
--    getting an invented one.
UPDATE sn_config.users u
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'user:' || u.handle
   AND u.handle IS NOT NULL AND u.handle <> ''
   AND u.actor_id IS NULL;

UPDATE sn_config.users u
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'agent:' || u.email
   AND u.role = 'agent'
   AND u.actor_id IS NULL;

UPDATE sn_config.users u
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'user:' || u.email
   AND (u.handle IS NULL OR u.handle = '')
   AND u.role <> 'agent'
   AND u.actor_id IS NULL;

-- ======================================================================
-- Identity backfill: populate capability for existing agents
-- ======================================================================
-- The capability column is immutable after creation. On a fresh install
-- every agent is created with a capability value via AgentCreate. On an
-- update the ALTER TABLE above adds the column with DEFAULT '', so this
-- block fills in the correct product-role capability from the agent's role
-- field. Agents with an unrecognised role fall back to their slug, which
-- is the legacy identity key and is the best available approximation.
-- Idempotent: the WHERE clause skips agents already backfilled.
UPDATE sn_config.agents
   SET capability = CASE role
       WHEN 'Foreman'                     THEN 'configurator'
       WHEN 'Developer'                   THEN 'developer'
       WHEN 'sysadmin'                    THEN 'sysadmin'
       WHEN 'Sysadmin'                    THEN 'sysadmin'
       WHEN 'Alex'                        THEN 'secretary'
       WHEN 'Secretary'                   THEN 'secretary'
       WHEN 'Node Architect'              THEN 'architect'
       WHEN 'Architect'                   THEN 'architect'
       WHEN 'PM'                          THEN 'pm'
       WHEN 'Project Manager'             THEN 'pm'
       WHEN 'Personal Finance Consultant' THEN 'finance'
       WHEN 'Finance'                     THEN 'finance'
       WHEN 'Marketing Strategist'        THEN 'marketing'
       WHEN 'Marketing'                   THEN 'marketing'
       WHEN 'Content Manager'             THEN 'content'
       WHEN 'Content'                     THEN 'content'
       WHEN 'Social Media Specialist'     THEN 'social-media'
       WHEN 'Social Media'                THEN 'social-media'
       ELSE slug
   END
 WHERE capability = '';

-- ======================================================================
-- Role model and permission matrix
-- ======================================================================
-- The DB-backed authorisation matrix the central `requires(name, object)`
-- dependency reads at every mutation.
--
-- Four tables:
--   sn_core.actor_roles        -- one role per actor; super-admin partial unique.
--   sn_config.capability_default_role -- capability default on install/update.
--   sn_config.permissions      -- the permission catalogue (name, desc, axis).
--   sn_config.role_permissions -- the role x permission matrix.
--
-- Idempotent on B02 reapply:
--   permissions: ON CONFLICT (name) DO UPDATE SET description, object_axis
--   role_permissions: ON CONFLICT (role, permission) DO NOTHING
--   actor_roles:      ON CONFLICT (actor_id)   DO NOTHING
--   capability_default_role: ON CONFLICT (capability) DO NOTHING
-- so owner customisations are preserved; descriptions and object_axis refresh.
--
-- ROLE NAME NOTE. The middle role is named `user`: every non-admin actor
-- (human or agent) holds it, and `agent` is already the name of an actor
-- KIND (sn_core.actors.kind), so the role must not reuse the word. The
-- ACTOR KIND `agent` is unchanged by this layer.
-- Allowed roles on CHECK constraints: ('super-admin', 'admin', 'user', 'installer').
--
-- The catalogue lists only what a route handler declares. Joining and leaving
-- a desk have no handlers yet, so they have no rows; they are seeded in the
-- release that adds the handlers.

CREATE TABLE IF NOT EXISTS sn_core.actor_roles (
    actor_id   text PRIMARY KEY REFERENCES sn_core.actors(id) ON DELETE RESTRICT,
    role       text NOT NULL
                   CONSTRAINT actor_roles_role_chk
                   CHECK (role IN ('super-admin', 'admin', 'user', 'installer')),
    granted_at timestamp with time zone NOT NULL DEFAULT now(),
    granted_by text REFERENCES sn_core.actors(id),
    source     text NOT NULL
                   CONSTRAINT actor_roles_source_chk
                   CHECK (source IN ('seed', 'class_default', 'owner_grant', 'migration'))
);

-- Exactly one super-admin per node. Partial unique index: it does not block
-- other roles and does not fire when `super-admin` is absent during install.
CREATE UNIQUE INDEX IF NOT EXISTS actor_roles_super_admin_singleton_idx
    ON sn_core.actor_roles (role)
    WHERE role = 'super-admin';

CREATE TABLE IF NOT EXISTS sn_config.capability_default_role (
    capability   text PRIMARY KEY,
    default_role text NOT NULL
                      CONSTRAINT capability_default_role_role_chk
                      CHECK (default_role IN ('admin', 'user'))
);

CREATE TABLE IF NOT EXISTS sn_config.permissions (
    name        text PRIMARY KEY,
    description text NOT NULL,
    object_axis text NOT NULL
                      CONSTRAINT permissions_object_axis_chk
                      CHECK (object_axis IN (
                          'none', 'desk', 'workspace', 'cross_channel_history',
                          'send_as', 'reach', 'agent_tool',
                          'agent_knowledge', 'project'
                      )),
    created_at  timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sn_config.role_permissions (
    role       text NOT NULL
                   CONSTRAINT role_permissions_role_chk
                   CHECK (role IN ('super-admin', 'admin', 'user', 'installer')),
    permission text NOT NULL REFERENCES sn_config.permissions(name) ON DELETE RESTRICT,
    granted_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (role, permission)
);

-- Credentials prove an actor on one named surface. No credential is inferred
-- from a channel-side users row or from a shared Unix account.
CREATE TABLE IF NOT EXISTS sn_config.actor_credentials (
    actor_id   text NOT NULL REFERENCES sn_core.actors(id) ON DELETE RESTRICT,
    cred_type  text NOT NULL,
    cred_value text NOT NULL,
    scope      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (actor_id, cred_type)
);

-- One proof on one surface must never identify two different actors.
CREATE UNIQUE INDEX IF NOT EXISTS actor_credentials_proof_uidx
    ON sn_config.actor_credentials (cred_type, cred_value, scope);

REVOKE ALL ON sn_config.actor_credentials FROM PUBLIC;

CREATE OR REPLACE FUNCTION sn_config.check_actor_credential_relation()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $$
DECLARE actor_relation text;
BEGIN
    SELECT relation INTO actor_relation FROM sn_core.actors WHERE id = NEW.actor_id;
    IF actor_relation IS DISTINCT FROM 'internal'
       AND NEW.cred_type IN ('os_user', 'bearer_token', 'authum_username') THEN
        RAISE EXCEPTION 'external or unclassified actor cannot hold administrative credential';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS actor_credential_relation_guard ON sn_config.actor_credentials;
CREATE TRIGGER actor_credential_relation_guard
BEFORE INSERT OR UPDATE ON sn_config.actor_credentials
FOR EACH ROW EXECUTE FUNCTION sn_config.check_actor_credential_relation();

CREATE OR REPLACE FUNCTION sn_core.check_actor_relation_credentials()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $$
BEGIN
    IF NEW.relation IS DISTINCT FROM 'internal' AND EXISTS (
        SELECT 1 FROM sn_config.actor_credentials
         WHERE actor_id = NEW.id
           AND cred_type IN ('os_user', 'bearer_token', 'authum_username')
    ) THEN
        RAISE EXCEPTION 'actor with administrative credential must remain internal';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS actor_relation_credential_guard ON sn_core.actors;
CREATE TRIGGER actor_relation_credential_guard
BEFORE UPDATE OF relation ON sn_core.actors
FOR EACH ROW EXECUTE FUNCTION sn_core.check_actor_relation_credentials();

-- Object grants used by the central desk and workspace checks. The desk
-- participant is also the durable record created by the admin join route.
CREATE TABLE IF NOT EXISTS sn_core.desk_participants (
    desk_id uuid NOT NULL REFERENCES sn_core.desks(id) ON DELETE CASCADE,
    actor_id text NOT NULL REFERENCES sn_core.actors(id) ON DELETE RESTRICT,
    source text NOT NULL DEFAULT 'grant',
    reason text,
    joined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (desk_id, actor_id)
);

CREATE TABLE IF NOT EXISTS sn_core.workspace_bindings (
    workspace_id text NOT NULL,
    actor_id text NOT NULL REFERENCES sn_core.actors(id) ON DELETE RESTRICT,
    bound_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, actor_id)
);

-- Reminders: a message to a person at a time, said by an agent in the chat where
-- it was asked ("remind me tomorrow at 9"). Not a task: nobody works on it, and it
-- is done once it is delivered. core-api fires it (routes/reminders.py), with no
-- model call, so it works on the smallest node and on every agent runtime.
CREATE TABLE IF NOT EXISTS sn_core.reminders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id integer NOT NULL DEFAULT 1,
    agent_slug text NOT NULL,
    desk_id uuid,
    text text NOT NULL,
    due_at timestamptz NOT NULL,
    repeat text CHECK (repeat IN ('day', 'week', 'month')),
    status text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'sent', 'cancelled')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    sent_count integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reminders_due_idx ON sn_core.reminders (due_at) WHERE status = 'scheduled';
-- One System chat per owner (companions/core-api/lib/system_chat.py).
CREATE UNIQUE INDEX IF NOT EXISTS desks_one_system_chat ON sn_core.desks (owner_user_id)
    WHERE metadata->>'system' = 'true';
-- The daily digest e-mail is the owner's choice (Settings, offered where mail is set up).
ALTER TABLE sn_config.node_control ADD COLUMN IF NOT EXISTS daily_digest boolean NOT NULL DEFAULT false;
-- How far the System chat has read the event journal.
CREATE TABLE IF NOT EXISTS sn_core.system_chat_watch (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    event_id bigint NOT NULL
);
-- Who it is for: the person (the clock writes it into the chat) or the agent (the
-- clock sends it an internal message, the Runner wakes it). The chat's owner is
-- kept here because a deleted chat takes its owner with it, and the reminder then
-- opens a new chat for them.
ALTER TABLE sn_core.reminders ADD COLUMN IF NOT EXISTS recipient text NOT NULL DEFAULT 'person'
    CHECK (recipient IN ('person', 'agent'));
ALTER TABLE sn_core.reminders ADD COLUMN IF NOT EXISTS owner_user_id integer;
UPDATE sn_core.reminders r SET owner_user_id = d.owner_user_id
  FROM sn_core.desks d WHERE r.desk_id = d.id AND r.owner_user_id IS NULL;
-- Waiting on a chat deleted before the owner was kept: nobody left to tell.
UPDATE sn_core.reminders SET status = 'cancelled' WHERE status = 'scheduled' AND owner_user_id IS NULL;

-- Only these capabilities have an explicit admin role. All others, including
-- empty capability, hold no actor_roles row and resolve to user.
INSERT INTO sn_config.capability_default_role (capability, default_role) VALUES
    ('configurator', 'admin'),
    ('sysadmin', 'admin')
ON CONFLICT (capability) DO NOTHING;

-- Backfill the stable actor attributes needed by the role-kind constraint.
-- B02's earlier actor backfill creates rows for active agents without these
-- newer attributes; it does not replace existing non-null values.
UPDATE sn_core.actors a
   SET kind = COALESCE(a.kind, 'agent'),
       relation = COALESCE(a.relation, 'internal')
  FROM sn_config.agents g
 WHERE a.id = g.actor_id AND g.archived_at IS NULL
   AND (a.kind IS NULL OR a.relation IS NULL);

CREATE OR REPLACE FUNCTION sn_core.check_actor_role_kind()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $$
DECLARE actor_kind text; actor_relation text;
BEGIN
    SELECT kind, relation INTO actor_kind, actor_relation
      FROM sn_core.actors WHERE id = NEW.actor_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'actor_roles actor % does not exist', NEW.actor_id;
    END IF;
    IF NEW.role = 'super-admin' AND
       (actor_kind IS DISTINCT FROM 'human' OR actor_relation IS DISTINCT FROM 'internal') THEN
        RAISE EXCEPTION 'super-admin requires an internal human actor';
    END IF;
    IF NEW.role = 'admin' AND actor_relation IS DISTINCT FROM 'internal' THEN
        RAISE EXCEPTION 'admin requires an internal actor';
    END IF;
    IF NEW.role = 'installer' AND actor_kind IS DISTINCT FROM 'component' THEN
        RAISE EXCEPTION 'installer requires a component actor';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS actor_roles_kind_guard ON sn_core.actor_roles;
CREATE TRIGGER actor_roles_kind_guard BEFORE INSERT OR UPDATE ON sn_core.actor_roles
FOR EACH ROW EXECUTE FUNCTION sn_core.check_actor_role_kind();

-- A capability default creates a row only for an elevated role. Existing
-- owner grants win: reapplying B02 never replaces an actor's chosen role.
INSERT INTO sn_core.actor_roles (actor_id, role, source)
SELECT g.actor_id, d.default_role, 'class_default'
  FROM sn_config.agents g
  JOIN sn_config.capability_default_role d ON d.capability = g.capability
 WHERE g.actor_id IS NOT NULL AND g.archived_at IS NULL
   AND d.default_role <> 'user'
ON CONFLICT (actor_id) DO NOTHING;

INSERT INTO sn_config.permissions (name, description, object_axis) VALUES
    ('agents.read', 'List and get agent records, health snapshots, runtime state, pending-work views.', 'none'),
    ('agents.self.wakes.read', 'List wakes raised for the acting agent (`POST /api/agents/me/wakes`).', 'none'),
    ('agents.create', 'Create an agent record (panel and API).', 'none'),
    ('agents.update', 'Update an agent record, including avatar upload.', 'none'),
    ('agents.archive', 'Archive an agent (`DELETE /api/agents/{slug}`).', 'none'),
    ('agents.regenerate', 'Regenerate an agent''s prompt.', 'none'),
    ('agents.system_prompt.read', 'Read an agent''s rendered system prompt.', 'none'),
    ('agents.tools.read', 'Read an agent''s effective tools.', 'none'),
    ('agents.grants.read', 'Read an agent''s grants.', 'none'),
    ('agents.grants.write', 'Edit an agent''s grants.', 'none'),
    ('agents.policies.read', 'Read an agent''s policies (including contacts list visibility).', 'none'),
    ('agents.policies.write', 'Edit an agent''s policies (including per-agent "who you may contact" list).', 'none'),
    ('agents.channels.read', 'Read an agent''s channels.', 'none'),
    ('agents.channels.bind', 'Bind or unbind a channel to an agent.', 'none'),
    ('agents.memory.read', 'Read what an agent remembers about people (its runtime memory).', 'none'),
    ('agents.memory.forget', 'Make an agent forget one remembered entry, or all of them.', 'none'),
    ('reminders.read', 'List reminders (an agent sees its own).', 'none'),
    ('reminders.write', 'Set or cancel a reminder (an agent only its own).', 'none'),
    ('agents.documents.read', 'List and get documents attached to an agent.', 'none'),
    ('agents.documents.write', 'Add text documents to an agent, upload document files.', 'none'),
    ('agents.documents.delete', 'Delete a document attached to an agent.', 'none'),
    ('agents.documents.pin.write', 'Pin or unpin a document on an agent.', 'none'),
    ('agents.runtime.control', 'Perform agent runtime status transitions (claim / release / fault).', 'none'),
    ('agents.wake', 'Raise another agent''s wake flag.', 'none'),
    ('agents.stop', 'Stop an agent: end its processes now and keep it from waking until enabled again.', 'none'),
    ('hitl.delivery', 'Deliver verified HITL task waits and answers from the dedicated local service.', 'none'),
    ('agents.health.refresh', 'Trigger an immediate per-agent health probe run outside the 15-minute cron.', 'none'),
    ('ai.status.read', 'Read AI gateway status snapshot (sessions/budgets/providers; cross-tenant for super-admin).', 'none'),
    -- attachments.read keeps handler scoping: an attachment hangs on a
    -- message, a message need not belong to a desk, and the handler already
    -- decides by the parent message who may read it.
    ('attachments.read', 'Read attachment metadata, bytes, and extracted text, scoped by parent-message access inside the handler.', 'none'),
    ('budgets.read', 'List budgets and read spend-warning check.', 'none'),
    ('work.read', 'Read the live work state and the work map of the node.', 'none'),
    ('limits.read', 'Read the limit registry: the turn bound, the profiles, one agent''s limits and usage.', 'none'),
    ('limits.write', 'Change a limit profile or an agent override, pause the node, report a cut turn.', 'none'),
    ('budgets.create', 'Create a budget row.', 'none'),
    ('budgets.update', 'Update a budget row (patch).', 'none'),
    ('budgets.notify_exhaustion', 'Mark budget as notified-of-exhaustion (internal/system use; coupled with budget.disabled events).', 'none'),
    ('context_status.read', 'Read the per-agent context-status snapshot (own agent).', 'none'),
    ('session_context.read', 'List global and per-agent session-context limits.', 'none'),
    ('session_context.write', 'Set or clear the global session-context limit and per-agent overrides.', 'none'),
    ('contacts.read', 'List contact records and their channels (contact book).', 'none'),
    ('contacts.create', 'Create a contact.', 'none'),
    ('contacts.update', 'Update a contact.', 'none'),
    ('contacts.archive', 'Archive a contact.', 'none'),
    ('contacts.channels.write', 'Add or remove a channel to a contact.', 'none'),
    ('agents.contacts.read', 'List an agent''s granted contacts (which contacts an agent may reach).', 'none'),
    -- Axis 'none' on the owner's ruling of 2026-10-04. It shipped in 1.12.49 as
    -- 'reach', an axis with no grant table behind it, so the object check
    -- refused everyone but the owner and nobody an owner delegates to could
    -- give a hired agent its contacts. The role decides until the reach grant
    -- table exists; the routes still name their object for that day.
    ('agents.contacts.write', 'Grant or revoke a contact for an agent.', 'none'),
    ('crons.read', 'List crons, read panel/render view.', 'none'),
    ('crons.render', 'Render the node crontab from DB rows (preview). Does not install.', 'none'),
    ('crons.write', 'Upsert, update, delete a cron row.', 'none'),
    ('crons.runs.read', 'List runs of a cron.', 'none'),
    ('crons.runs.write', 'Record a run (start/finish) for a cron. Internal callback from the runner, must survive agent-tier identity.', 'none'),
    -- desks.read keeps handler scoping: the listing names no desk, and no
    -- code path gives an account a seat on a desk yet, so the object axis
    -- stays 'none' until seats are written when a desk is created or joined.
    ('desks.read', 'List desks, read desk history, sessions, attachments metadata and bytes.', 'none'),
    ('desks.create', 'Create a desk (ensure or create).', 'none'),
    ('desks.update', 'Update desk metadata (patch).', 'desk'),
    ('desks.delete', 'Delete a desk.', 'desk'),
    ('desks.attachments.write', 'Upload an attachment to a desk.', 'desk'),
    ('desks.attachments.delete', 'Delete a desk attachment.', 'desk'),
    ('events.read', 'Read events and histogram (admin audit surface).', 'none'),
    ('internal.events.write', 'Insert an event row (internal/service callback; never granted to agents).', 'none'),
    -- messages.read and messages.send keep handler scoping: most of their
    -- handlers carry no desk, so the object axis stays 'none' until the
    -- mailbox, history and desk verbs are split into their own permissions.
    ('messages.read', 'List messages, histogram, by-contact, search.', 'none'),
    ('messages.inbox.read', 'Read own inbox and inbox snapshot.', 'none'),
    ('messages.inbox.write', 'Read-thread (ack), dismiss, mark-state on OWN inbox entries (handler enforces ownership).', 'none'),
    ('messages.send', 'Send a message as the acting agent (reply or new).', 'none'),
    ('messages.deliver_desk', 'Deliver a message to a desk (admin tool).', 'desk'),
    ('internal.messages.inbound', 'Record an inbound message (webhook/receiver callback; never granted to agents).', 'none'),
    ('internal.messages.voice.write', 'Record voice turn / call-ended / webchat input (receiver callback; never granted to agents).', 'none'),
    ('internal.messages.routing', 'Resolve which agent may read a (channel, recipient) pair (routing lookup by a node service; held by no role).', 'none'),
    ('planner.proxy', 'Call the node planner via core-api proxy. Planner enforces its own authorization on each sub-operation.', 'none'),
    ('models.read', 'List models and providers.', 'none'),
    ('models.write', 'Create, update, delete a model entry.', 'none'),
    ('mcp.read', 'List MCP servers and server tools.', 'none'),
    ('mcp.write', 'Upsert, update, delete an MCP server; bulk upsert of its tools.', 'none'),
    ('tools.read', 'List and get tools catalogue entries.', 'none'),
    ('tools.write', 'Create, update, delete a tool entry.', 'none'),
    ('references.read', 'List and get reference values.', 'none'),
    ('references.write', 'Create, update, delete a reference value.', 'none'),
    ('runtimes.read', 'List runtimes and live runtimes; get one.', 'none'),
    ('runtimes.write', 'Upsert, update, delete a runtime definition.', 'none'),
    ('specialists.read', 'List and get specialists (library and node copies).', 'none'),
    ('specialists.write', 'Create, update, delete a specialist.', 'none'),
    ('workflows.read', 'List and get workflow definitions.', 'none'),
    ('workflows.run', 'Run a workflow by name.', 'none'),
    ('workflows.write', 'Switch a workflow on or off and choose the agents who may start it. The owner only.', 'none'),
    ('workflows.import', 'Import workflow definitions (bulk).', 'none'),
    ('integrations.read', 'List, get, read tools exposed by an integration.', 'none'),
    ('integrations.write', 'Upsert, update, delete an integration.', 'none'),
    ('policies.read', 'List node-level policies.', 'none'),
    ('policies.write', 'Create, update, delete a node-level policy row.', 'none'),
    ('departments.read', 'List departments.', 'none'),
    -- 1.12.49: knowledge.*, projects.*, users.read keep handler scoping until
    -- their per-object grant models are defined.
    ('knowledge.read', 'List knowledge kinds and an agent''s knowledge rows.', 'none'),
    ('knowledge.write', 'Add or delete an agent''s knowledge rows.', 'none'),
    ('provision.read', 'List provisioning parameters.', 'none'),
    ('provision.write', 'Author or retire provisioning parameters; run a projection.', 'none'),
    ('settings.read', 'Read node settings.', 'none'),
    ('settings.write', 'Update node settings.', 'none'),
    ('projects.read', 'List and get projects (own-project view enforced inside handler for agent tier).', 'none'),
    ('projects.write', 'Create and update a project (patch).', 'none'),
    ('projects.members.write', 'Add a member to a project.', 'none'),
    ('projects.reprovision', 'Reprovision a project (idempotent).', 'none'),
    ('projects.archive', 'Archive a project.', 'none'),
    ('agents.sessions.read', 'List sessions attributed to a given agent (studio Sessions tab).', 'none'),
    ('sessions.read', 'Get one session + messages; list sessions cross-agent (admin view).', 'none'),
    ('users.read', 'Resolve identity, cross-channel history, list a user''s notify channels.', 'none'),
    ('users.channels.write', 'Upsert a user''s notify channel (self-only for a non-admin; any for admin+).', 'none'),
    ('google.oauth.connect', 'Begin and finish Google OAuth connect flow for the acting agent (connect/callback).', 'none'),
    ('google.read', 'Read Google integration status, token snapshot, picker config.', 'none'),
    ('google.docs.create', 'Create a Google Doc via the integration.', 'none'),
    ('google.calendar.read', 'Query calendar freebusy.', 'none'),
    ('google.calendar.write', 'Create a calendar event.', 'none'),
    ('system.status.read', 'Read aggregated system status, overview, services, crons, models, system summary.', 'none'),
    ('system.runner.read', 'Read runner state.', 'none'),
    ('system.runner.control', 'Start/stop/restart the runner.', 'none'),
    ('system.models.control', 'Start/stop model service.', 'none'),
    ('test_nodes.read', 'Status probe of a test LXC container.', 'none'),
    ('test_nodes.provision', 'Provision a test LXC container.', 'none'),
    ('test_nodes.destroy', 'Destroy a test LXC container.', 'none'),
    ('setup.complete', 'One-shot: consume the setup token and create the first operator (installer-window only).', 'none'),
    ('notifications.notify', 'Resolve planner assignee to channels and emit outbound rows (planner-outbound callback).', 'none'),
    ('secrets.refs.read', 'List secret refs that exist in the vault (names only, never values).', 'none'),
    ('secrets.read', 'Resolve a secret value by ref (address-only reads; the store applies its own gating).', 'none'),
    ('secrets.write', 'Set or delete a secret value.', 'none'),
    ('secrets.snapshots.read', 'List and read secret snapshots.', 'none'),
    ('gateway_secrets.read', 'Read CLI env vars (gateway-level credentials).', 'none'),
    ('gateway_secrets.write', 'Set CLI credentials, set/delete gateway secrets, run CLI login flow, project CLI credential.', 'none'),
    ('gateway.chatgpt.token', 'The gateway asks for a fresh ChatGPT plan access token. A node service only; no role holds it.', 'none'),
    ('database.read', 'List tables and read table rows via the admin database browser.', 'none'),
    ('memory.read', 'Read every workspace of the node memory (mesh): list, search, open a document. The owner only: it reads every agent''s memory.', 'none'),
    ('access.read', 'Read who can sign in to the node and with what role, and every role''s holders and permissions. The owner only.', 'none'),
    ('data.delete', 'Perform a hard delete on durable data (reserved; no route uses it yet). Subsumed under the general super-admin object-axis bypass.', 'none'),
    ('roles.grant', 'Change a user''s role (write to `sn_core.actor_roles`).', 'none'),
    ('admin.node_status', 'admin-runner op: node_status.', 'none'),
    ('admin.ensure_agent_users', 'admin-runner op: ensure_agent_users.', 'none'),
    ('admin.ensure_project', 'admin-runner op: ensure_project.', 'none'),
    ('admin.materialize_crons', 'admin-runner op: materialize_crons.', 'none'),
    ('admin.render_agent_runtime_env', 'admin-runner op: render_agent_runtime_env.', 'none'),
    ('admin.rollback_crons', 'admin-runner op: rollback_crons.', 'none'),
    ('admin.run_health_probe', 'admin-runner op: run_health_probe.', 'none'),
    ('admin.agent_memory', 'admin-runner op: agent_memory (list or forget what a Flint agent remembers).', 'none'),
    ('admin.lxc_node_provision', 'admin-runner op: lxc_node_provision.', 'none'),
    ('admin.lxc_node_status', 'admin-runner op: lxc_node_status.', 'none'),
    ('admin.lxc_node_destroy', 'admin-runner op: lxc_node_destroy.', 'none'),
    ('admin.run_command', 'admin-runner op: run_command (shell).', 'none'),
    ('admin.service_control', 'admin-runner op: service_control (start/stop/restart a systemd service).', 'none'),
    ('admin.claude_login_start', 'admin-runner op: claude_login_start.', 'none'),
    ('admin.claude_login_complete', 'admin-runner op: claude_login_complete.', 'none'),
    ('admin.lifecycle.reopen', 'admin-runner op: flip `node_state.lifecycle` from `operational` back to `installing` on an owner decision.', 'none')
ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, object_axis = EXCLUDED.object_axis;

-- Rows an earlier build of this catalogue seeded for handlers that do not
-- exist. Removed so a node that carried them matches a fresh one.
DELETE FROM sn_config.role_permissions WHERE permission IN ('desks.join', 'desks.leave');
DELETE FROM sn_config.permissions WHERE name IN ('desks.join', 'desks.leave');

INSERT INTO sn_config.role_permissions (role, permission) VALUES
    ('super-admin', 'work.read'),
    ('admin', 'work.read'),
    ('user', 'work.read'),
    ('super-admin', 'limits.read'),
    ('admin', 'limits.read'),
    ('super-admin', 'limits.write'),
    ('admin', 'limits.write'),
    ('super-admin', 'notifications.notify'),
    ('admin', 'notifications.notify'),
    ('super-admin', 'crons.runs.write'),
    ('admin', 'crons.runs.write'),
    ('super-admin', 'budgets.notify_exhaustion'),
    ('admin', 'budgets.notify_exhaustion'),
    ('super-admin', 'agents.read'),
    ('admin', 'agents.read'),
    ('user', 'agents.read'),
    ('super-admin', 'agents.self.wakes.read'),
    ('admin', 'agents.self.wakes.read'),
    ('user', 'agents.self.wakes.read'),
    ('super-admin', 'agents.tools.read'),
    ('admin', 'agents.tools.read'),
    ('user', 'agents.tools.read'),
    ('super-admin', 'agents.contacts.read'),
    ('admin', 'agents.contacts.read'),
    ('user', 'agents.contacts.read'),
    ('super-admin', 'agents.sessions.read'),
    ('admin', 'agents.sessions.read'),
    ('user', 'agents.sessions.read'),
    ('super-admin', 'attachments.read'),
    ('admin', 'attachments.read'),
    ('user', 'attachments.read'),
    ('super-admin', 'context_status.read'),
    ('admin', 'context_status.read'),
    ('user', 'context_status.read'),
    ('super-admin', 'departments.read'),
    ('admin', 'departments.read'),
    ('user', 'departments.read'),
    ('super-admin', 'desks.read'),
    ('admin', 'desks.read'),
    ('user', 'desks.read'),
    ('super-admin', 'knowledge.read'),
    ('admin', 'knowledge.read'),
    ('user', 'knowledge.read'),
    ('super-admin', 'messages.read'),
    ('admin', 'messages.read'),
    ('user', 'messages.read'),
    ('super-admin', 'messages.inbox.read'),
    ('admin', 'messages.inbox.read'),
    ('user', 'messages.inbox.read'),
    ('super-admin', 'messages.inbox.write'),
    ('admin', 'messages.inbox.write'),
    ('user', 'messages.inbox.write'),
    ('super-admin', 'messages.send'),
    ('admin', 'messages.send'),
    ('user', 'messages.send'),
    ('super-admin', 'planner.proxy'),
    ('admin', 'planner.proxy'),
    ('user', 'planner.proxy'),
    ('super-admin', 'references.read'),
    ('admin', 'references.read'),
    ('user', 'references.read'),
    ('super-admin', 'models.read'),
    ('admin', 'models.read'),
    ('user', 'models.read'),
    ('super-admin', 'tools.read'),
    ('admin', 'tools.read'),
    ('user', 'tools.read'),
    ('super-admin', 'mcp.read'),
    ('admin', 'mcp.read'),
    ('user', 'mcp.read'),
    ('super-admin', 'runtimes.read'),
    ('admin', 'runtimes.read'),
    ('user', 'runtimes.read'),
    ('super-admin', 'specialists.read'),
    ('admin', 'specialists.read'),
    ('user', 'specialists.read'),
    ('super-admin', 'workflows.read'),
    ('admin', 'workflows.read'),
    ('user', 'workflows.read'),
    ('super-admin', 'integrations.read'),
    ('admin', 'integrations.read'),
    ('user', 'integrations.read'),
    ('super-admin', 'policies.read'),
    ('admin', 'policies.read'),
    ('user', 'policies.read'),
    ('super-admin', 'budgets.read'),
    ('admin', 'budgets.read'),
    ('user', 'budgets.read'),
    ('super-admin', 'ai.status.read'),
    ('admin', 'ai.status.read'),
    ('user', 'ai.status.read'),
    ('super-admin', 'google.oauth.connect'),
    ('admin', 'google.oauth.connect'),
    ('user', 'google.oauth.connect'),
    ('super-admin', 'google.read'),
    ('admin', 'google.read'),
    ('user', 'google.read'),
    ('super-admin', 'google.docs.create'),
    ('admin', 'google.docs.create'),
    ('user', 'google.docs.create'),
    ('super-admin', 'google.calendar.read'),
    ('admin', 'google.calendar.read'),
    ('user', 'google.calendar.read'),
    ('super-admin', 'google.calendar.write'),
    ('admin', 'google.calendar.write'),
    ('user', 'google.calendar.write'),
    ('super-admin', 'secrets.read'),
    ('admin', 'secrets.read'),
    ('user', 'secrets.read'),
    ('super-admin', 'secrets.refs.read'),
    ('admin', 'secrets.refs.read'),
    ('user', 'secrets.refs.read'),
    ('super-admin', 'users.read'),
    ('admin', 'users.read'),
    ('user', 'users.read'),
    ('super-admin', 'users.channels.write'),
    ('admin', 'users.channels.write'),
    ('user', 'users.channels.write'),
    ('super-admin', 'system.status.read'),
    ('admin', 'system.status.read'),
    ('user', 'system.status.read'),
    ('super-admin', 'agents.documents.read'),
    ('admin', 'agents.documents.read'),
    ('user', 'agents.documents.read'),
    ('super-admin', 'projects.read'),
    ('admin', 'projects.read'),
    ('user', 'projects.read'),
    ('super-admin', 'agents.policies.read'),
    ('admin', 'agents.policies.read'),
    ('user', 'agents.policies.read'),
    ('super-admin', 'agents.channels.read'),
    ('admin', 'agents.channels.read'),
    ('user', 'agents.channels.read'),
    ('super-admin', 'agents.grants.read'),
    ('admin', 'agents.grants.read'),
    ('user', 'agents.grants.read'),
    ('super-admin', 'sessions.read'),
    ('admin', 'sessions.read'),
    ('super-admin', 'agents.system_prompt.read'),
    ('admin', 'agents.system_prompt.read'),
    ('super-admin', 'events.read'),
    ('admin', 'events.read'),
    ('super-admin', 'agents.create'),
    ('admin', 'agents.create'),
    ('super-admin', 'agents.update'),
    ('admin', 'agents.update'),
    ('super-admin', 'agents.archive'),
    ('admin', 'agents.archive'),
    ('super-admin', 'agents.regenerate'),
    ('admin', 'agents.regenerate'),
    ('super-admin', 'agents.grants.write'),
    ('admin', 'agents.grants.write'),
    ('super-admin', 'agents.policies.write'),
    ('admin', 'agents.policies.write'),
    ('super-admin', 'agents.channels.bind'),
    ('admin', 'agents.channels.bind'),
    ('super-admin', 'agents.documents.write'),
    ('admin', 'agents.documents.write'),
    ('super-admin', 'agents.documents.delete'),
    ('admin', 'agents.documents.delete'),
    ('super-admin', 'agents.documents.pin.write'),
    ('admin', 'agents.documents.pin.write'),
    ('super-admin', 'agents.runtime.control'),
    ('admin', 'agents.runtime.control'),
    ('super-admin', 'agents.wake'),
    ('admin', 'agents.wake'),
    ('super-admin', 'agents.stop'),
    ('admin', 'agents.stop'),
    ('super-admin', 'agents.health.refresh'),
    ('admin', 'agents.health.refresh'),
    ('super-admin', 'agents.contacts.write'),
    ('admin', 'agents.contacts.write'),
    ('super-admin', 'budgets.create'),
    ('admin', 'budgets.create'),
    ('super-admin', 'budgets.update'),
    ('admin', 'budgets.update'),
    ('super-admin', 'contacts.read'),
    ('admin', 'contacts.read'),
    ('super-admin', 'contacts.create'),
    ('admin', 'contacts.create'),
    ('super-admin', 'contacts.update'),
    ('admin', 'contacts.update'),
    ('super-admin', 'contacts.archive'),
    ('admin', 'contacts.archive'),
    ('super-admin', 'contacts.channels.write'),
    ('admin', 'contacts.channels.write'),
    ('super-admin', 'crons.read'),
    ('admin', 'crons.read'),
    ('super-admin', 'crons.render'),
    ('admin', 'crons.render'),
    ('super-admin', 'crons.write'),
    ('admin', 'crons.write'),
    ('super-admin', 'crons.runs.read'),
    ('admin', 'crons.runs.read'),
    ('super-admin', 'desks.create'),
    ('admin', 'desks.create'),
    ('super-admin', 'desks.update'),
    ('admin', 'desks.update'),
    ('super-admin', 'desks.delete'),
    ('admin', 'desks.delete'),
    ('super-admin', 'desks.attachments.write'),
    ('admin', 'desks.attachments.write'),
    ('super-admin', 'desks.attachments.delete'),
    ('admin', 'desks.attachments.delete'),
    ('super-admin', 'integrations.write'),
    ('admin', 'integrations.write'),
    ('super-admin', 'knowledge.write'),
    ('admin', 'knowledge.write'),
    ('super-admin', 'mcp.write'),
    ('admin', 'mcp.write'),
    ('super-admin', 'messages.deliver_desk'),
    ('admin', 'messages.deliver_desk'),
    ('super-admin', 'models.write'),
    ('admin', 'models.write'),
    ('super-admin', 'policies.write'),
    ('admin', 'policies.write'),
    ('super-admin', 'projects.write'),
    ('admin', 'projects.write'),
    ('super-admin', 'projects.members.write'),
    ('admin', 'projects.members.write'),
    ('super-admin', 'projects.reprovision'),
    ('admin', 'projects.reprovision'),
    ('super-admin', 'projects.archive'),
    ('admin', 'projects.archive'),
    ('super-admin', 'provision.read'),
    ('admin', 'provision.read'),
    ('super-admin', 'provision.write'),
    ('admin', 'provision.write'),
    ('super-admin', 'references.write'),
    ('admin', 'references.write'),
    ('super-admin', 'runtimes.write'),
    ('admin', 'runtimes.write'),
    ('super-admin', 'session_context.read'),
    ('admin', 'session_context.read'),
    ('super-admin', 'specialists.write'),
    ('admin', 'specialists.write'),
    ('super-admin', 'tools.write'),
    ('admin', 'tools.write'),
    ('super-admin', 'workflows.run'),
    ('super-admin', 'workflows.write'),
    ('admin', 'workflows.run'),
    ('super-admin', 'workflows.import'),
    ('admin', 'workflows.import'),
    ('super-admin', 'test_nodes.read'),
    ('admin', 'test_nodes.read'),
    ('super-admin', 'test_nodes.provision'),
    ('admin', 'test_nodes.provision'),
    ('super-admin', 'system.runner.read'),
    ('admin', 'system.runner.read'),
    -- The admin tier holds these because the handler names its writers by
    -- capability, and the capabilities it names carry the admin role. The
    -- handler's own check stays and is the narrower of the two.
    ('super-admin', 'secrets.write'),
    ('admin', 'secrets.write'),
    ('super-admin', 'secrets.snapshots.read'),
    ('admin', 'secrets.snapshots.read'),
    ('super-admin', 'gateway_secrets.read'),
    ('super-admin', 'gateway_secrets.write'),
    ('super-admin', 'database.read'),
    ('super-admin', 'memory.read'),
    ('super-admin', 'access.read'),
    ('super-admin', 'data.delete'),
    ('super-admin', 'roles.grant'),
    ('super-admin', 'admin.lifecycle.reopen'),
    ('super-admin', 'session_context.write'),
    ('super-admin', 'settings.read'),
    ('super-admin', 'settings.write'),
    ('super-admin', 'system.runner.control'),
    ('super-admin', 'system.models.control'),
    ('super-admin', 'test_nodes.destroy'),
    ('super-admin', 'admin.run_command'),
    ('super-admin', 'admin.service_control'),
    ('super-admin', 'admin.claude_login_start'),
    ('super-admin', 'admin.claude_login_complete'),
    ('super-admin', 'admin.node_status'),
    ('admin', 'admin.node_status'),
    ('super-admin', 'admin.ensure_agent_users'),
    ('admin', 'admin.ensure_agent_users'),
    ('installer', 'admin.ensure_agent_users'),
    ('super-admin', 'admin.ensure_project'),
    ('admin', 'admin.ensure_project'),
    ('installer', 'admin.ensure_project'),
    ('super-admin', 'admin.materialize_crons'),
    ('admin', 'admin.materialize_crons'),
    ('installer', 'admin.materialize_crons'),
    ('super-admin', 'admin.render_agent_runtime_env'),
    ('admin', 'admin.render_agent_runtime_env'),
    ('installer', 'admin.render_agent_runtime_env'),
    ('super-admin', 'admin.rollback_crons'),
    ('admin', 'admin.rollback_crons'),
    ('super-admin', 'admin.run_health_probe'),
    ('admin', 'admin.run_health_probe'),
    ('super-admin', 'admin.agent_memory'),
    ('admin', 'admin.agent_memory'),
    ('super-admin', 'agents.memory.read'),
    ('admin', 'agents.memory.read'),
    ('super-admin', 'agents.memory.forget'),
    ('admin', 'agents.memory.forget'),
    ('super-admin', 'reminders.read'),
    ('admin', 'reminders.read'),
    ('user', 'reminders.read'),
    ('super-admin', 'reminders.write'),
    ('admin', 'reminders.write'),
    ('user', 'reminders.write'),
    ('super-admin', 'admin.lxc_node_provision'),
    ('admin', 'admin.lxc_node_provision'),
    ('super-admin', 'admin.lxc_node_status'),
    ('admin', 'admin.lxc_node_status'),
    ('super-admin', 'admin.lxc_node_destroy')
ON CONFLICT (role, permission) DO NOTHING;

-- Owner notification trigger on desk.admin_joined events.
-- Best-effort: a failure here does NOT reverse the join or the event; the
-- owner reads the audit trail from sn_core.events whichever way the
-- notification path goes.
--
CREATE OR REPLACE FUNCTION sn_core.desk_admin_joined_notify_owner()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $$
DECLARE
    ch record;
    owner_user record;
    notice_text text;
BEGIN
    SELECT u.id, u.tenant_id INTO owner_user
      FROM sn_core.actor_roles ar
      JOIN sn_config.users u ON u.actor_id = ar.actor_id
     WHERE ar.role = 'super-admin' AND u.active AND u.archived_at IS NULL
     LIMIT 1;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    notice_text := format('Administrator %s joined desk %s. Reason: %s',
        NEW.actor, NEW.entity_id, COALESCE(NEW.payload->>'reason', '(none)'));
    FOR ch IN SELECT channel_slug, address
                FROM sn_config.user_channels
               WHERE user_id = owner_user.id AND enabled = true
                 AND channel_slug IN ('telegram', 'web-chat')
    LOOP
        BEGIN
            INSERT INTO sn_core.messages
                (channel_type, channel_id, external_id, thread_id, direction,
                 sender, recipient, content, content_type, metadata, tenant_id)
            VALUES
                (ch.channel_slug,
                 CASE WHEN ch.channel_slug = 'telegram' THEN 'smart_node_bot'
                      ELSE ch.address END,
                 gen_random_uuid()::text,
                 CASE WHEN ch.channel_slug = 'web-chat'
                      THEN 'desk:' || NEW.entity_id ELSE ch.address END,
                 'outbound', 'component:core-api', ch.address, notice_text,
                 'text/plain',
                 jsonb_build_object('source', 'desk.admin_joined',
                                    'subject', 'Administrator joined a desk',
                                    'event_id', NEW.id), owner_user.tenant_id);
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'desk.admin_joined owner notification failed on %: %',
                ch.channel_slug, SQLERRM;
        END;
    END LOOP;
    RETURN NULL;
EXCEPTION WHEN OTHERS THEN
    -- Best-effort: the join and the event already persisted; a failure
    -- here must not reverse them. The event IS the durable audit record.
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS desk_admin_joined_notify_owner_trg ON sn_core.events;
CREATE TRIGGER desk_admin_joined_notify_owner_trg
    AFTER INSERT ON sn_core.events
    FOR EACH ROW
    WHEN (NEW.event_type = 'desk.admin_joined' AND NEW.entity_type = 'desk')
    EXECUTE FUNCTION sn_core.desk_admin_joined_notify_owner();

-- The one event stream: every row written to sn_core.events, by any
-- writer, is announced on channel sn_events when its transaction commits.
-- core-api listens and passes each person the events of their own chats
-- (GET /api/events/stream), so the Studio stops polling for news. Only the
-- address goes out; a reader that needs the row reads it with its own rights.
CREATE OR REPLACE FUNCTION sn_core.events_notify() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS $$
BEGIN
    PERFORM pg_notify('sn_events', json_build_object(
        'id', NEW.id, 'type', NEW.event_type, 'tenant', NEW.tenant_id,
        'entity_type', NEW.entity_type, 'entity_id', NEW.entity_id,
        'desk', NEW.payload->>'desk')::text);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS events_notify_trg ON sn_core.events;
CREATE TRIGGER events_notify_trg
    AFTER INSERT ON sn_core.events
    FOR EACH ROW EXECUTE FUNCTION sn_core.events_notify();
