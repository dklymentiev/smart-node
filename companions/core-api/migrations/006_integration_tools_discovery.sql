-- Migration 006: integration tools discovery -- columns for MCP/HTTP dispatch + auth refs
--
-- Adds the minimum columns the Integration Tools Registry needs:
--
-- sn_core.tools:
--   input_schema  -- function-calling JSON schema, populated by MCP tools/list
--                    or supplied manually for kind='api' (HTTP) tools
--   http_method   -- for kind='api': GET/POST/PUT/DELETE
--   http_path     -- for kind='api': path appended to integration.endpoint
--   last_seen     -- last time discovery confirmed this tool exists upstream
--
-- sn_config.integrations:
--   endpoint            -- MCP URL or HTTP base URL used by dispatcher
--                          (distinct from install_uri which is the human-facing link)
--   auth_kind           -- none/bearer/api_key/basic/oauth2/custom
--   auth_secret_ref     -- key path inside /etc/smart-node/integrations.sops.yaml
--                          (e.g. 'replicate.api_key')
--   auth_header_name    -- for api_key/bearer custom headers
--   last_discovered_at  -- last successful tools/list call
--
-- Existing `kind` column on sn_core.tools (cli|mcp|api|builtin) is reused
-- as the transport indicator -- no new `transport` column.
--
-- Wrapped in a single transaction; safe to rerun? -- no, ALTER ADD fails on
-- duplicate. Run once.

BEGIN;

-- ============================================================
-- sn_core.tools  -- dispatch + discovery columns
-- ============================================================
ALTER TABLE sn_core.tools
    ADD COLUMN input_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN http_method  TEXT NULL,
    ADD COLUMN http_path    TEXT NULL,
    ADD COLUMN last_seen    TIMESTAMPTZ NULL;

COMMENT ON COLUMN sn_core.tools.input_schema IS
    'OpenAI/MCP function-calling JSON schema. Populated by MCP discovery (tools/list) or by the operator for kind=api manual tools.';
COMMENT ON COLUMN sn_core.tools.http_method IS
    'kind=api only: HTTP verb (GET/POST/PUT/DELETE/PATCH). NULL for kind=mcp/cli/builtin.';
COMMENT ON COLUMN sn_core.tools.http_path IS
    'kind=api only: path appended to integrations.endpoint. May contain {arg} placeholders bound from input_schema.';
COMMENT ON COLUMN sn_core.tools.last_seen IS
    'Last time discovery confirmed this tool is present upstream. NULL for manual tools that are not auto-discovered.';

ALTER TABLE sn_core.tools
    ADD CONSTRAINT tools_http_method_check
    CHECK (http_method IS NULL OR http_method IN ('GET','POST','PUT','DELETE','PATCH'));

-- ============================================================
-- sn_config.integrations  -- endpoint + auth ref columns
-- ============================================================
ALTER TABLE sn_config.integrations
    ADD COLUMN endpoint            TEXT NOT NULL DEFAULT '',
    ADD COLUMN auth_kind           TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN auth_secret_ref     TEXT NULL,
    ADD COLUMN auth_header_name    TEXT NULL,
    ADD COLUMN last_discovered_at  TIMESTAMPTZ NULL;

ALTER TABLE sn_config.integrations
    ADD CONSTRAINT integrations_auth_kind_check
    CHECK (auth_kind IN ('none','bearer','api_key','basic','oauth2','custom'));

COMMENT ON COLUMN sn_config.integrations.endpoint IS
    'Dispatcher endpoint: MCP URL for kind=mcp-only / HTTP base URL for kind=external. Distinct from install_uri (human-facing link). Auto-backfilled from install_uri on this migration.';
COMMENT ON COLUMN sn_config.integrations.auth_kind IS
    'Authentication scheme: none/bearer/api_key/basic/oauth2/custom. oauth2 currently delegated to MCP wrapper servers (panel does not run OAuth flow itself).';
COMMENT ON COLUMN sn_config.integrations.auth_secret_ref IS
    'Path inside /etc/smart-node/integrations.sops.yaml (e.g. "replicate.api_key"). Resolved at dispatch-time via core-api /api/secrets/resolve. NULL for auth_kind=none.';
COMMENT ON COLUMN sn_config.integrations.auth_header_name IS
    'HTTP header carrying the secret for auth_kind=api_key (e.g. "X-API-Key") or bearer (default "Authorization"). NULL for none/basic/oauth2.';
COMMENT ON COLUMN sn_config.integrations.last_discovered_at IS
    'Last successful MCP tools/list call. NULL for kind=app companion-managed integrations that do not need discovery.';

-- Backfill endpoint from install_uri for existing rows.
UPDATE sn_config.integrations
SET endpoint = install_uri
WHERE install_uri <> '' AND endpoint = '';

COMMIT;
