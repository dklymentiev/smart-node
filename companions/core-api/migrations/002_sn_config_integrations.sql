-- Migration 002: sn_config.integrations (B2 integrations schema)
--
-- Promotes "integration" to a first-class entity. Tools point to integrations
-- via a slug FK; admin /integrations/ page (later) becomes the registry editor;
-- companion installers register their integration row at install time.
--
-- This migration is phase 1 of the plan -- backend-safe alone, backward-
-- compatible until phase 6 (agent-edit Tools tab refactor).
--
-- Wrapped in single transaction; fails if any object already exists.

BEGIN;

-- ============================================================
-- sn_config.integrations: registry of installed sources
-- ============================================================

CREATE TABLE sn_config.integrations (
    id              BIGSERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    label           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'app'
                        CHECK (kind IN ('system', 'app', 'external', 'mcp-only')),
    status          TEXT NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available', 'not-installed', 'broken')),
    install_uri     TEXT NOT NULL DEFAULT '',
    companion_slug  TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    tenant_id       INT REFERENCES sn_config.tenants(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at     TIMESTAMPTZ
);

CREATE INDEX integrations_status_idx        ON sn_config.integrations(status);
CREATE INDEX integrations_companion_slug_idx ON sn_config.integrations(companion_slug);

COMMENT ON TABLE  sn_config.integrations IS
    'Registry of installed sources (system, planner, mesh, analytics, ...). One row per integration. Tools join via slug.';
COMMENT ON COLUMN sn_config.integrations.slug IS
    'Stable join key, human-readable. Used as FK target for sn_core.tools.integration_slug.';
COMMENT ON COLUMN sn_config.integrations.companion_slug IS
    'Soft link to companion installer (companions.d/*.json basename). NULL for external integrations with no companion.';

-- ============================================================
-- Bootstrap rows for the two integrations that exist today
-- ============================================================

INSERT INTO sn_config.integrations (slug, label, description, kind, status, install_uri, companion_slug)
VALUES
    ('system',  'System',  'Built-in primitives (bash, fs, fetch) and the AI Gateway.', 'system', 'available', '',                                   NULL),
    ('planner', 'Planner', 'Task and objective management.',               'app',    'available', 'http://localhost',       'tracking');

-- ============================================================
-- sn_core.tools.integration_slug: FK from live tools to registry
-- ============================================================

ALTER TABLE sn_core.tools
    ADD COLUMN integration_slug TEXT
        REFERENCES sn_config.integrations(slug)
        ON UPDATE CASCADE
        ON DELETE SET NULL;

CREATE INDEX tools_integration_slug_idx ON sn_core.tools(integration_slug);

COMMENT ON COLUMN sn_core.tools.integration_slug IS
    'FK -> sn_config.integrations.slug. Authoritative source of integration grouping (supersedes the source:component:X tag heuristic).';

-- ============================================================
-- Backfill: derive integration_slug from existing tags
-- ============================================================
--
-- Rule:
--   * tools with a 'source:component:X' tag  -> integration_slug = X
--   * tools without any 'source:*' tag       -> integration_slug = 'system'
--
-- After this UPDATE, the source: tag remains in tags[] for backward-compat,
-- but integration_slug becomes the source of truth. Phase 2 endpoint code
-- will derive the tag from the column on read; phase 7 (deferred) drops
-- the tag entirely.

-- 1) Tools with explicit source:component:X tag -> map to that integration.
UPDATE sn_core.tools t
SET    integration_slug = sub.src
FROM (
    SELECT id,
           substring(tag FROM 'source:component:(.+)') AS src
    FROM   sn_core.tools, unnest(tags) AS tag
    WHERE  tag LIKE 'source:component:%'
) sub
WHERE  t.id = sub.id
  AND  sub.src IS NOT NULL
  AND  EXISTS (SELECT 1 FROM sn_config.integrations i WHERE i.slug = sub.src);

-- 2) Remaining tools (no source: tag matched) -> system.
UPDATE sn_core.tools
SET    integration_slug = 'system'
WHERE  integration_slug IS NULL;

-- ============================================================
-- Sanity check: every tool must now belong to an integration
-- ============================================================

DO $$
DECLARE
    orphan_count INT;
BEGIN
    SELECT count(*) INTO orphan_count
    FROM   sn_core.tools
    WHERE  integration_slug IS NULL;

    IF orphan_count > 0 THEN
        RAISE EXCEPTION 'Migration 002 backfill incomplete: % tool(s) with NULL integration_slug', orphan_count;
    END IF;
END
$$;

COMMIT;
