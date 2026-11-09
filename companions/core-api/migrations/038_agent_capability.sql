-- 038: capability column on sn_config.agents
--
-- Adds an immutable product-role marker to every agent row. Seeds, the
-- installer, and update scripts look up agents by capability rather than by
-- literal role slug. The column is set at creation time from the product
-- template; it is never updated.
--
-- NOTE: this file is reference-only. The column, its update-path ALTER TABLE,
-- and the backfill UPDATE are all in B02-sn-core-schema.sql, which is the file
-- B02-persistence.sh actually runs on both fresh installs and updates.
-- companions/core-api/migrations/ is not executed by any bootstrap or companion
-- code (core-api.sh states this outright). Keep this file as documentation.
--
-- Release: first release after 1.12.38 with agent capability migration.

-- 1. Add the column (idempotent via IF NOT EXISTS)
ALTER TABLE sn_config.agents ADD COLUMN IF NOT EXISTS capability text NOT NULL DEFAULT '';

-- 2. Backfill from the role field using the product-role mapping.
--    Agents with an unrecognised role get their slug as capability (best
--    available approximation for nodes that predate this ADR).
UPDATE sn_config.agents
   SET capability = CASE role
       WHEN 'Foreman'           THEN 'configurator'
       WHEN 'Developer'         THEN 'developer'
       WHEN 'Sysadmin'          THEN 'sysadmin'
       WHEN 'Alex'              THEN 'secretary'
       WHEN 'Secretary'         THEN 'secretary'
       WHEN 'Architect'         THEN 'architect'
       WHEN 'PM'                THEN 'pm'
       WHEN 'Project Manager'   THEN 'pm'
       WHEN 'Finance'           THEN 'finance'
       WHEN 'Marketing'         THEN 'marketing'
       WHEN 'Content'           THEN 'content'
       WHEN 'Social Media'      THEN 'social-media'
       ELSE slug
   END
 WHERE capability = '';
