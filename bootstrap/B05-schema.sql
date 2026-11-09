-- B05-schema.sql -- Smart Node identity schema (operator AuthZ store).
--
-- Applied by B05-identity.sh into the `smartnode` core database.
--
-- B05 separates AuthN from AuthZ. Authum (a Docker service) proves WHO an
-- operator is -- passkey / Argon2id password, file-backed -- and emits a
-- `Remote-User` header. The roles and permissions that decide WHAT an
-- operator may do are relational data and live HERE, in the core DB, not
-- in Authum. Product code (the B06 panel, agents) resolves Remote-User ->
-- role through these tables.
--
-- This file seeds ROLES only. The super-admin USER row is NOT seeded by
-- B05: per installer-stages.md (canonical) the super-admin account is
-- created by the operator after B06 via a one-time setup token. B05 ships
-- Authum with an empty users.json; B06's first-run flow writes the first
-- `identity_users` row, its `user_roles` grant and its `actors` entry.
--
-- See the implementation plan.
--
-- Idempotent: safe to re-apply -- B05-identity.sh applies it on every run.

\set ON_ERROR_STOP on

-- Objects are owned by the core application role, not the postgres
-- superuser that runs the apply -- same convention as B02-schema.sql.
SET ROLE smartnode;

-- ----------------------------------------------------------------------
-- roles -- the AuthZ role catalogue. Seeded with a small default set;
-- the B06 panel activates templates and adds custom roles.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roles (
    id            text        PRIMARY KEY,
    display_name  text        NOT NULL,
    description   text        NOT NULL DEFAULT '',
    active        boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  roles              IS 'Smart Node operator role catalogue (AuthZ).';
COMMENT ON COLUMN roles.id           IS 'Stable slug, e.g. super-admin.';
COMMENT ON COLUMN roles.active       IS 'false = template role, not yet usable; B06 panel activates.';

-- ----------------------------------------------------------------------
-- identity_users -- one row per internal operator. The username is the
-- key Authum returns in Remote-User; it links the AuthN store (Authum
-- users.json) to this AuthZ store. actor_id ties the operator to the
-- B02 event log so their actions are journaled.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identity_users (
    username      text        PRIMARY KEY,
    display_name  text        NOT NULL,
    actor_id      text        REFERENCES actors(id),
    active        boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  identity_users           IS 'Smart Node internal operators -- the AuthZ side of each Authum identity.';
COMMENT ON COLUMN identity_users.username  IS 'Matches the Remote-User header Authum emits; key into the AuthN store.';
COMMENT ON COLUMN identity_users.actor_id  IS 'FK to actors.id (layer=human) -- one-way; never mutates the append-only event log.';

-- ----------------------------------------------------------------------
-- user_roles -- operator <-> role assignment (many-to-many).
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_roles (
    username      text        NOT NULL REFERENCES identity_users(username) ON DELETE CASCADE,
    role_id       text        NOT NULL REFERENCES roles(id),
    granted_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (username, role_id)
);

COMMENT ON TABLE user_roles IS 'Operator-to-role assignment (many-to-many).';

CREATE INDEX IF NOT EXISTS user_roles_role_idx ON user_roles (role_id);

-- ----------------------------------------------------------------------
-- Seed -- the default role set. super-admin is active (the operator's
-- first account, created via the B06 setup token, is granted it).
-- manager / designer ship as inactive templates -- the B06 panel
-- activates and extends them.
-- ----------------------------------------------------------------------
INSERT INTO roles (id, display_name, description, active) VALUES
    ('super-admin', 'Super Administrator',
     'Full control of the node: operators, companions, configuration.', true),
    ('manager',     'Manager',
     'Operational management; no node configuration. Template -- activate in the panel.', false),
    ('designer',    'Designer',
     'Content and design tasks. Template -- activate in the panel.', false)
ON CONFLICT (id) DO NOTHING;

RESET ROLE;
