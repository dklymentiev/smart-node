-- Migration 003: add purpose column to sn_config.integrations
--
-- Splits the conflated "label" field into two: label (brand: Mesh, Doci,
-- Rein, Screenbox, Planner) and purpose (role: Memory, Documents,
-- Orchestrator, Browser, Tasks). The panel UI shows both columns so an
-- operator can see "Doci = Documents" at a glance.
--
-- See the implementation plan.

BEGIN;

ALTER TABLE sn_config.integrations
    ADD COLUMN purpose TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN sn_config.integrations.purpose IS
    'Role / functional category (Memory, Documents, Orchestrator, Browser, Tasks). Distinct from label (the brand-y short name shown next to it).';

-- Backfill existing six integrations registered up to this point.
UPDATE sn_config.integrations SET purpose = 'System'       WHERE slug = 'system';
UPDATE sn_config.integrations SET purpose = 'Tasks'        WHERE slug = 'planner';
UPDATE sn_config.integrations SET purpose = 'Memory'       WHERE slug = 'mesh';
UPDATE sn_config.integrations SET purpose = 'Documents'    WHERE slug = 'doci';
UPDATE sn_config.integrations SET purpose = 'Orchestrator' WHERE slug = 'rein';
UPDATE sn_config.integrations SET purpose = 'Browser'      WHERE slug = 'screenbox';

-- Sanity: every row must have purpose set (manual rows can stay '' but
-- the seven we know about should be populated).
DO $$
DECLARE empty_count int;
BEGIN
    SELECT count(*) INTO empty_count
      FROM sn_config.integrations
      WHERE purpose = ''
        AND slug IN ('system','planner','mesh','doci','rein','screenbox');
    IF empty_count > 0 THEN
        RAISE EXCEPTION 'backfill missed % known integration(s)', empty_count;
    END IF;
END $$;

COMMIT;
