-- Migration 004: add install_path column to sn_config.integrations
--
-- Companion installers know WHERE they put the install (e.g.
-- /opt/smart-node/tracking) -- the operator wants to see that path
-- alongside the install_uri in the panel.
--

BEGIN;

ALTER TABLE sn_config.integrations
    ADD COLUMN install_path TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN sn_config.integrations.install_path IS
    'Local install directory on the node (e.g. /opt/smart-node/tracking). Empty for external integrations and system primitives.';

-- Backfill known local companion-managed rows on the node.
UPDATE sn_config.integrations SET install_path = '/opt/smart-node/tracking'  WHERE slug = 'planner';
UPDATE sn_config.integrations SET install_path = '/opt/smart-node/documents' WHERE slug = 'doci';
UPDATE sn_config.integrations SET install_path = '/opt/smart-node/workflows' WHERE slug = 'rein';
-- Also normalize the placeholder install_uri values that came from
-- SMART_NODE_HOST being unset at install time -- the value resolved to
-- "localhost" so URIs read like http://doci.localhost.
UPDATE sn_config.integrations SET install_uri = 'http://doci.localhost'  WHERE slug = 'doci'  AND install_uri LIKE '%localhost%';
UPDATE sn_config.integrations SET install_uri = 'http://rein.localhost'  WHERE slug = 'rein'  AND install_uri LIKE '%localhost%';

COMMIT;
