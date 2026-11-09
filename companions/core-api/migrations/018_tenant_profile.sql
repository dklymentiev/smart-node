-- 018_tenant_profile.sql
-- The onboarding config store.
-- Four tables in sn_config: tenant_profile (source of truth for parameters),
-- tenant_profile_history (edit trail), projection_ledger (idempotency), and
-- merge_ownership (fail-closed column registry for stateful-merge).
-- Schema only. Projector, behaviors, and configurator-agent are follow-on tasks.

-- ---------------------------------------------------------------------------
-- tenant_profile: finite parameters set at onboarding, edited occasionally.
-- Single-tenant-per-node: no tenant_id; PK is param_key (flat dotted key).
-- Secrets NEVER sit in value; for secret-write-once, value holds a secret-ref.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sn_config.tenant_profile (
    param_key    text PRIMARY KEY,                    -- "approval.outbound", "brand.voice"
    value        jsonb NOT NULL DEFAULT 'null'::jsonb, -- per value_kind; NEVER a raw secret
    value_kind   text NOT NULL DEFAULT 'literal'
                 CHECK (value_kind IN ('literal','pointer','secret-ref')),
    behavior     text NOT NULL
                 CHECK (behavior IN ('config-sync','stateful-merge','seed-once',
                                     'policy-versioned','secret-write-once')),
    target       text,                                -- table.field / policy slug / solution id
    natural_key  text,                                -- create-decision key for seed-once / policy-versioned
    source       text NOT NULL DEFAULT 'interview'
                 CHECK (source IN ('vertical-pack-default','interview','manual')),
    default_hash text,                                -- hash of the shipped pack default (upgrade merge)
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','confirmed','projected','failed','diverged','retired')),
    version      int  NOT NULL DEFAULT 1,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    -- M-a: secrets and ONLY secrets are write-once
    CONSTRAINT tenant_profile_secret_iff_write_once
        CHECK ((value_kind = 'secret-ref') = (behavior = 'secret-write-once'))
);
CREATE INDEX IF NOT EXISTS tenant_profile_behavior_idx ON sn_config.tenant_profile (behavior);
CREATE INDEX IF NOT EXISTS tenant_profile_status_idx   ON sn_config.tenant_profile (status);

-- ---------------------------------------------------------------------------
-- tenant_profile_history: append-only edit trail. No FK to tenant_profile so
-- history survives retirement/removal of the live row. Plaintext secrets never
-- reach here because value only ever holds a secret-ref for secret-write-once.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sn_config.tenant_profile_history (
    param_key text NOT NULL,
    version   int  NOT NULL,
    value     jsonb NOT NULL DEFAULT 'null'::jsonb,
    source    text,
    who       text,
    ts        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (param_key, version)
);

-- ---------------------------------------------------------------------------
-- projection_ledger: what the projector materialized last run, for idempotency
-- and the reverse reconcile/teardown pass. One row per (param_key,target_pk).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sn_config.projection_ledger (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    param_key    text NOT NULL,
    target_table text NOT NULL,
    target_pk    text,                 -- PK of the materialized target row (as text)
    natural_key  text,
    content_hash text,                 -- hash(ref) for secrets, never hash(secret)
    projected_at timestamptz NOT NULL DEFAULT now(),
    result       text,                 -- projected | failed | skipped | retired
    CONSTRAINT projection_ledger_uniq UNIQUE (param_key, target_table, target_pk)
);
CREATE INDEX IF NOT EXISTS projection_ledger_param_idx ON sn_config.projection_ledger (param_key);

-- ---------------------------------------------------------------------------
-- merge_ownership: fail-closed registry of which columns / JSON-paths on a
-- stateful-merge target the profile owns. Unknown column => runtime (never
-- written by the projector). jsonb ownership is declared at JSON-path level.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sn_config.merge_ownership (
    target_table       text NOT NULL,
    column_or_jsonpath text NOT NULL,
    owner              text NOT NULL DEFAULT 'runtime'
                       CHECK (owner IN ('profile','runtime')),
    PRIMARY KEY (target_table, column_or_jsonpath)
);
