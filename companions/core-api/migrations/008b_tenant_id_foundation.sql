-- Migration 008b -- tenant_id foundation across operational tables.
--
-- Adds tenant_id INTEGER FK to sn_config.tenants(id) on every
-- operational data table so multi-tenant filtering / RLS can be added
-- later WITHOUT a backfill migration on accumulated production data.
-- v0 logic is single-tenant: every existing and new row defaults to the
-- 'default' tenant. No app-level filter is enforced yet.
--
-- Rationale: tenant_id is the asymmetric-cost decision -- ~1 hour now
-- vs days-weeks of backfill+FK-rework later. This migration captures
-- the schema commitment cheaply while the data set is small.
--
-- Convention matches the existing sn_config.* tables that already carry
-- tenant_id INTEGER FK to sn_config.tenants(id) (agents, specialists,
-- integrations, flows, runtimes, teams, departments, aliases,
-- interactions, users, tenant_brand).
--
-- Out of scope -- intentionally NOT tagged with tenant_id:
--   * public.actors / roles / user_roles -- RBAC primitives, may span tenants
--   * sn_config.applications / mcp_servers / runtimes -- system catalog
--   * sn_config.tools (vs sn_core.tools) -- secondary catalog
--   * sn_config.agent_tools / team_members / agent_policy_opts /
--     department_policies -- junction tables, inherit through FK
--   * sn_config.config_audit / changelog_entries / flow_versions /
--     output_schemas / refs / tenant_brand -- meta / internal
--
-- These will be revisited when multi-tenant routing is actually wired.

\set ON_ERROR_STOP on
SET ROLE smartnode;

BEGIN;

-- 1. Ensure default tenant row exists (idempotent)
INSERT INTO sn_config.tenants (slug, name, settings_jsonb)
VALUES ('default', 'Default Tenant', '{}'::jsonb)
ON CONFLICT (slug) DO NOTHING;

-- 2. Add tenant_id to every operational table via DO block (captures
--    default-tenant id once, applies idempotently). PG 11+ adds
--    NOT NULL DEFAULT columns in O(1) so no table rewrite.
DO $$
DECLARE
    default_tenant_id INTEGER;
BEGIN
    SELECT id INTO default_tenant_id FROM sn_config.tenants WHERE slug='default';
    IF default_tenant_id IS NULL THEN
        RAISE EXCEPTION 'default tenant row missing -- INSERT in step 1 must have failed';
    END IF;

    -- public.messages
    EXECUTE format(
        'ALTER TABLE public.messages
            ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT %s
            REFERENCES sn_config.tenants(id)', default_tenant_id);
    CREATE INDEX IF NOT EXISTS idx_messages_tenant ON public.messages(tenant_id);

    -- public.message_state
    EXECUTE format(
        'ALTER TABLE public.message_state
            ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT %s
            REFERENCES sn_config.tenants(id)', default_tenant_id);
    CREATE INDEX IF NOT EXISTS idx_message_state_tenant ON public.message_state(tenant_id);

    -- public.events (consolidated in 008a; SUPERSEDED -- the journal is
    -- sn_core.events and public.events no longer exists, see 008a's header)
    EXECUTE format(
        'ALTER TABLE public.events
            ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT %s
            REFERENCES sn_config.tenants(id)', default_tenant_id);
    CREATE INDEX IF NOT EXISTS idx_pub_events_tenant ON public.events(tenant_id);

    -- public.identity_users
    EXECUTE format(
        'ALTER TABLE public.identity_users
            ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT %s
            REFERENCES sn_config.tenants(id)', default_tenant_id);
    CREATE INDEX IF NOT EXISTS idx_identity_users_tenant ON public.identity_users(tenant_id);

    -- sn_core.crons
    EXECUTE format(
        'ALTER TABLE sn_core.crons
            ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT %s
            REFERENCES sn_config.tenants(id)', default_tenant_id);
    CREATE INDEX IF NOT EXISTS idx_sn_crons_tenant ON sn_core.crons(tenant_id);

    -- sn_core.cron_runs
    EXECUTE format(
        'ALTER TABLE sn_core.cron_runs
            ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT %s
            REFERENCES sn_config.tenants(id)', default_tenant_id);
    CREATE INDEX IF NOT EXISTS idx_sn_cron_runs_tenant ON sn_core.cron_runs(tenant_id);

    -- sn_core.tools
    EXECUTE format(
        'ALTER TABLE sn_core.tools
            ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT %s
            REFERENCES sn_config.tenants(id)', default_tenant_id);
    CREATE INDEX IF NOT EXISTS idx_sn_tools_tenant ON sn_core.tools(tenant_id);
END $$;

COMMIT;
