-- 036: actor_id on sn_config.agents
--
-- Adds actor_id (text, FK -> sn_core.actors(id)) to sn_config.agents so
-- that every active agent resolves to a canonical actor identity.
--
-- WATERMARK ROWS: nine e2e-watermark-* rows were hard-deleted by the
-- operator at 20:52 CT on 2026-09-07 before
-- this migration was merged. Before counts: agents=16, agent_channels=28,
-- users=18. After counts: agents=7, agent_channels=19, users=9. Zero
-- e2e-watermark actors existed in sn_core.actors (migration 034 had not
-- run). Step 5 therefore asserts zero nulls and applies NOT NULL
-- unconditionally; it raises EXCEPTION if any nulls remain.
--
-- ORDERING NOTE: migration 034 (user actor_id) provisions actors for all
-- sn_config.agents rows without filtering archived_at. This migration
-- provisions actors for ACTIVE agents only (WHERE archived_at IS NULL), so
-- archived residue does not receive actor identities here. If 034 has already
-- run, step 3 below is a no-op for active agents.
--
-- Column is nullable until step 5, which applies NOT NULL unconditionally.
-- Release: 1.12.33. Prerequisite: the actor-alias resolution migration.

-- -----------------------------------------------------------------------
-- 1. Schema column
-- -----------------------------------------------------------------------

ALTER TABLE sn_config.agents
    ADD COLUMN IF NOT EXISTS actor_id text;

-- -----------------------------------------------------------------------
-- 2. FK: actor_id -> sn_core.actors(id)
-- -----------------------------------------------------------------------
-- ON DELETE SET NULL: removing an actor row from sn_core.actors does not
-- cascade-delete the agent; the agent loses its actor reference and is
-- treated as unresolved until a new actor is assigned.

DO $agents_actor_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname    = 'agents_actor_id_fkey'
           AND conrelid   = 'sn_config.agents'::regclass
    ) THEN
        ALTER TABLE sn_config.agents
            ADD CONSTRAINT agents_actor_id_fkey
            FOREIGN KEY (actor_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL;
    END IF;
END
$agents_actor_fk$;

-- -----------------------------------------------------------------------
-- 3. Provision actor rows for active agents not yet in sn_core.actors
-- -----------------------------------------------------------------------
-- Filtered to archived_at IS NULL: archived residue does not receive actor
-- identities here. If migration 034 has already run, this INSERT is a no-op
-- for all active agents (their actors already exist via 034 step 3).
--
-- type = 'agent', layer = 'specialist' matches _LAYER_BY_PREFIX in
-- events_emit.py (prefix 'agent' -> layer 'specialist').

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

-- -----------------------------------------------------------------------
-- 4. Populate actor_id for active agents
-- -----------------------------------------------------------------------
-- Archived rows are skipped intentionally. Every agent:<slug> row IS the
-- canonical row (canonical_id is NULL on it); COALESCE(NULL, a.id) = a.id,
-- which is 'agent:<slug>'. Correct for all real agents.

UPDATE sn_config.agents g
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'agent:' || g.slug
   AND g.archived_at IS NULL
   AND g.actor_id IS NULL;

-- -----------------------------------------------------------------------
-- 5. NOT NULL -- applied after asserting zero nulls
-- -----------------------------------------------------------------------
-- Precondition (confirmed 2026-09-07 20:52 CT by operator): the nine
-- e2e-watermark-* archived agent rows were
-- hard-deleted before this migration was merged. After step 4 above
-- populates actor_id for every remaining active agent, no NULL actor_id
-- rows can exist.
--
-- The DO block asserts this with RAISE EXCEPTION so the migration fails
-- loudly if the precondition is somehow unmet, rather than silently
-- skipping the constraint.

DO $agents_actor_not_null$
DECLARE
    null_count int;
BEGIN
    SELECT COUNT(*) INTO null_count FROM sn_config.agents WHERE actor_id IS NULL;
    IF null_count <> 0 THEN
        RAISE EXCEPTION
            'agents.actor_id: % row(s) still NULL after population step -- '
            'NOT NULL not applied. Hard-delete any remaining archived agent '
            'residue and re-run the migration.',
            null_count;
    END IF;
    ALTER TABLE sn_config.agents ALTER COLUMN actor_id SET NOT NULL;
END
$agents_actor_not_null$;
