-- Migration 001: sn_config initial schema (B2a, RFC 012)
-- 22 tables. Most have no runtime reader yet; created as canvas per user decision.
-- All wrapped in single transaction; fails if any object already exists.

BEGIN;

CREATE SCHEMA sn_config AUTHORIZATION smartnode;
GRANT USAGE ON SCHEMA sn_config TO smartnode;
ALTER DEFAULT PRIVILEGES IN SCHEMA sn_config GRANT ALL ON TABLES TO smartnode;
ALTER DEFAULT PRIVILEGES IN SCHEMA sn_config GRANT USAGE, SELECT ON SEQUENCES TO smartnode;

-- ============================================================
-- Tenant layer
-- ============================================================

CREATE TABLE sn_config.tenants (
    id              SERIAL PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    name            TEXT NOT NULL,
    settings_jsonb  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at     TIMESTAMPTZ
);

CREATE TABLE sn_config.users (
    id          SERIAL PRIMARY KEY,
    tenant_id   INT REFERENCES sn_config.tenants(id),
    email       TEXT NOT NULL,
    name        TEXT,
    role        TEXT,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ,
    UNIQUE (tenant_id, email)
);

CREATE TABLE sn_config.aliases (
    id           SERIAL PRIMARY KEY,
    tenant_id    INT REFERENCES sn_config.tenants(id),
    alias        TEXT NOT NULL,
    target_kind  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, alias)
);

CREATE TABLE sn_config.tenant_brand (
    id         SERIAL PRIMARY KEY,
    tenant_id  INT REFERENCES sn_config.tenants(id),
    kind       TEXT NOT NULL CHECK (kind IN ('brand-voice','compliance','content-calendar','platform-limits')),
    body_md    TEXT,
    json_data  JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, kind)
);

-- ============================================================
-- Applications & Tools
-- ============================================================

CREATE TABLE sn_config.applications (
    id             SERIAL PRIMARY KEY,
    slug           TEXT UNIQUE NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    name           TEXT NOT NULL,
    version        TEXT,
    install_path   TEXT,
    status         TEXT NOT NULL DEFAULT 'installed'
                       CHECK (status IN ('installed','disabled','broken')),
    manifest_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    installed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    installed_by   TEXT,
    archived_at    TIMESTAMPTZ
);

CREATE TABLE sn_config.tools (
    id               SERIAL PRIMARY KEY,
    application_id   INT REFERENCES sn_config.applications(id),
    slug             TEXT UNIQUE NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    kind             TEXT NOT NULL CHECK (kind IN ('cli','mcp','api','builtin')),
    name             TEXT NOT NULL,
    path_or_endpoint TEXT,
    description      TEXT,
    schema_jsonb     JSONB NOT NULL DEFAULT '{}'::jsonb,
    caps             TEXT[] NOT NULL DEFAULT '{}',   -- subset of {read,write,destructive}
    enabled          BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sn_config.mcp_servers (
    id              SERIAL PRIMARY KEY,
    application_id  INT REFERENCES sn_config.applications(id),
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    transport       TEXT NOT NULL,
    config_jsonb    JSONB NOT NULL DEFAULT '{}'::jsonb,
    autoinstall     BOOLEAN NOT NULL DEFAULT false,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Specialists & Agents
-- ============================================================

CREATE TABLE sn_config.specialists (
    id          SERIAL PRIMARY KEY,
    tenant_id   INT REFERENCES sn_config.tenants(id),
    slug        TEXT NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,60}$'),
    name        TEXT NOT NULL,
    body_md     TEXT NOT NULL DEFAULT '',
    archetype   TEXT CHECK (archetype IN ('head','specialist','reviewer','autonomous','shared')),
    version     TEXT NOT NULL DEFAULT '0.1.0',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT,
    updated_by  TEXT,
    archived_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX specialists_slug_active
    ON sn_config.specialists (tenant_id, slug)
    WHERE archived_at IS NULL;

CREATE TABLE sn_config.departments (
    id            SERIAL PRIMARY KEY,
    tenant_id     INT REFERENCES sn_config.tenants(id),
    slug          TEXT NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    name          TEXT NOT NULL,
    type_slug     TEXT,                                  -- engineering/marketing/... (from git globals)
    head_agent_id INT,                                   -- FK added after agents
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at   TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);

CREATE TABLE sn_config.agents (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT REFERENCES sn_config.tenants(id),
    department_id   INT REFERENCES sn_config.departments(id),
    slug            TEXT NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    title           TEXT NOT NULL,
    role            TEXT,
    description     TEXT,
    specialist_id   INT REFERENCES sn_config.specialists(id),
    model           TEXT NOT NULL DEFAULT 'sonnet'
                        CHECK (model IN ('opus','sonnet','haiku','none')),
    schedule_cron   TEXT,
    os_user         TEXT,
    mesh_workspace  TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    version         TEXT NOT NULL DEFAULT '0.1.0',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT,
    updated_by      TEXT,
    archived_at     TIMESTAMPTZ
);

CREATE UNIQUE INDEX agents_slug_active
    ON sn_config.agents (tenant_id, slug)
    WHERE archived_at IS NULL;

ALTER TABLE sn_config.departments
    ADD CONSTRAINT departments_head_agent_fk
    FOREIGN KEY (head_agent_id) REFERENCES sn_config.agents(id) ON DELETE SET NULL;

CREATE TABLE sn_config.agent_tools (
    agent_id     INT NOT NULL REFERENCES sn_config.agents(id) ON DELETE CASCADE,
    tool_id      INT NOT NULL REFERENCES sn_config.tools(id) ON DELETE CASCADE,
    granted_caps TEXT[] NOT NULL DEFAULT '{}',
    config_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, tool_id)
);

CREATE TABLE sn_config.agent_policy_opts (
    id               SERIAL PRIMARY KEY,
    agent_id         INT NOT NULL REFERENCES sn_config.agents(id) ON DELETE CASCADE,
    policy_slug      TEXT NOT NULL,
    action           TEXT NOT NULL CHECK (action IN ('include','exclude','override')),
    override_body_md TEXT,
    UNIQUE (agent_id, policy_slug)
);

-- ============================================================
-- Departments / Teams / Interactions
-- ============================================================

CREATE TABLE sn_config.department_policies (
    department_id INT NOT NULL REFERENCES sn_config.departments(id) ON DELETE CASCADE,
    policy_slug   TEXT NOT NULL,
    PRIMARY KEY (department_id, policy_slug)
);

CREATE TABLE sn_config.teams (
    id            SERIAL PRIMARY KEY,
    tenant_id     INT REFERENCES sn_config.tenants(id),
    department_id INT REFERENCES sn_config.departments(id),
    slug          TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at   TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);

CREATE TABLE sn_config.team_members (
    team_id      INT NOT NULL REFERENCES sn_config.teams(id) ON DELETE CASCADE,
    agent_id     INT NOT NULL REFERENCES sn_config.agents(id) ON DELETE CASCADE,
    role_in_team TEXT CHECK (role_in_team IN ('head','specialist','reviewer','autonomous','shared')),
    PRIMARY KEY (team_id, agent_id)
);

CREATE TABLE sn_config.interactions (
    id            SERIAL PRIMARY KEY,
    tenant_id     INT REFERENCES sn_config.tenants(id),
    from_agent_id INT NOT NULL REFERENCES sn_config.agents(id) ON DELETE CASCADE,
    to_agent_id   INT NOT NULL REFERENCES sn_config.agents(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('assigns_tasks_to','escalates_to','notifies','reports_to')),
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_agent_id, to_agent_id, kind)
);

-- ============================================================
-- Flows
-- ============================================================

CREATE TABLE sn_config.flows (
    id                 SERIAL PRIMARY KEY,
    tenant_id          INT REFERENCES sn_config.tenants(id),
    slug               TEXT NOT NULL,
    name               TEXT NOT NULL,
    version            TEXT NOT NULL DEFAULT '0.1.0',
    yaml_path          TEXT,
    status             TEXT NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft','active','deprecated','archived')),
    department_id      INT REFERENCES sn_config.departments(id),
    output_schema_slug TEXT,
    created_by         TEXT,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, slug)
);

CREATE TABLE sn_config.flow_versions (
    id              SERIAL PRIMARY KEY,
    flow_id         INT NOT NULL REFERENCES sn_config.flows(id) ON DELETE CASCADE,
    version         TEXT NOT NULL,
    yaml_snapshot   TEXT,
    changed_by      TEXT,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    changelog_entry TEXT
);

-- ============================================================
-- References & Output schemas
-- ============================================================

CREATE TABLE sn_config.refs (        -- "references" is reserved-ish; use refs
    id         SERIAL PRIMARY KEY,
    scope      TEXT NOT NULL CHECK (scope IN ('global','tenant','department')),
    scope_id   INT,
    key        TEXT NOT NULL,
    target_uri TEXT NOT NULL,
    required   BOOLEAN NOT NULL DEFAULT false,
    notes      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX refs_scope_key ON sn_config.refs (scope, scope_id, key);

CREATE TABLE sn_config.output_schemas (
    id          SERIAL PRIMARY KEY,
    scope       TEXT NOT NULL CHECK (scope IN ('global','tenant')),
    scope_id    INT,
    slug        TEXT NOT NULL,
    name        TEXT,
    json_schema JSONB NOT NULL,
    version     TEXT NOT NULL DEFAULT '1.0.0'
);

-- ============================================================
-- Audit
-- ============================================================

CREATE TABLE sn_config.config_audit (
    id         BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    row_pk     TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('insert','update','delete')),
    diff_jsonb JSONB,
    changed_by TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sn_config.changelog_entries (
    id          BIGSERIAL PRIMARY KEY,
    entity_kind TEXT NOT NULL
                    CHECK (entity_kind IN ('agent','flow','specialist','policy','department','team','reference','tool','application')),
    entity_id   INT NOT NULL,
    version     TEXT,
    reason      TEXT,
    change_type TEXT CHECK (change_type IN ('arch','minor','patch')),
    changed_by  TEXT,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
