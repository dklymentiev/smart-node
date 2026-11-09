-- 037: kind and relation on sn_core.actors; actor_id on sn_config.contacts
--
-- Step 4 of the identity migration.
--
-- TWO CHANGES:
-- (a) Adds kind and relation columns to sn_core.actors.
--     Classifies every actor by canonical identity: prefix for canonical rows,
--     inheritance from canonical for alias rows. The external partner actor is the
--     first row with relation='external' on this node.
-- (b) Populates actor_id on sn_config.contacts for both rows.
--     contact id=1 (the owner) -> user:admin (same as users id=1; no second actor).
--     contact id=2 (external partner Claude) -> agent:partner-claude (inserted in step 4).
--
-- Runs after 033 (canonical_id/aliased_at), 034 (user actor_id), 036 (agent actor_id).
-- Has never run anywhere at time of writing (2026-09-08).
-- Release: 1.12.33.
--
-- CLASSIFICATION RULE:
--   user:%      -> kind='human',     relation='internal'
--   agent:%     -> kind='agent',     relation='internal'
--   component:% -> kind='component', relation='internal'
--   installer:% -> kind='component', relation='internal'  (integration installers)
--   cron:%      -> kind='component', relation='internal'  (scheduler jobs)
-- Alias rows (canonical_id IS NOT NULL) inherit kind and relation from their
-- canonical row. Any actor that remains NULL after both steps triggers RAISE
-- EXCEPTION in step 7 -- the migration aborts loudly rather than silently
-- leaving unclassified rows.
--
-- Identity rule: kind is immutable. Both columns must be set correctly at insert
-- time; there is no update path for kind.

-- -----------------------------------------------------------------------
-- 1. Schema columns on sn_core.actors (nullable for now)
-- -----------------------------------------------------------------------

ALTER TABLE sn_core.actors
    ADD COLUMN IF NOT EXISTS kind     text,
    ADD COLUMN IF NOT EXISTS relation text;

-- -----------------------------------------------------------------------
-- 2. Classify canonical actors by prefix
--    Canonical row = canonical_id IS NULL (the row IS the canonical identity).
--    All existing actors are relation='internal'; the only relation='external'
--    actor is provisioned in step 4 below.
-- -----------------------------------------------------------------------

UPDATE sn_core.actors
   SET kind = CASE
           WHEN id LIKE 'user:%'      THEN 'human'
           WHEN id LIKE 'agent:%'     THEN 'agent'
           WHEN id LIKE 'component:%' THEN 'component'
           WHEN id LIKE 'installer:%' THEN 'component'
           WHEN id LIKE 'cron:%'      THEN 'component'
       END,
       relation = 'internal'
 WHERE canonical_id IS NULL
   AND kind IS NULL;

-- -----------------------------------------------------------------------
-- 3. Alias rows inherit kind and relation from their canonical
--    Runs after step 2 so every canonical row has kind/relation set.
--    Migration 033 creates no chains (alias -> canonical -> canonical),
--    so a single pass is sufficient.
-- -----------------------------------------------------------------------

UPDATE sn_core.actors a
   SET kind     = c.kind,
       relation = c.relation
  FROM sn_core.actors c
 WHERE a.canonical_id = c.id
   AND a.kind IS NULL;

-- -----------------------------------------------------------------------
-- 4. Provision the external partner actor (contact id=2)
--    First actor with relation='external' on this node.
--    contact id=2 is the AI secretary on a partner node (kind='partner' in contacts).
--    It must NOT be merged with the operator's actor row.
--    kind=agent: a different entity on a different node; not a human.
--    relation=external: not managed by this node.
-- -----------------------------------------------------------------------

INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation)
VALUES (
    'agent:partner-claude',
    'Claude (partner)',
    'agent',
    'specialist',
    'agent',
    'external'
)
ON CONFLICT (id) DO NOTHING;

-- -----------------------------------------------------------------------
-- 5. FK: contacts.actor_id -> sn_core.actors(id)
--    The baked schema includes this FK; include it idempotently here so
--    nodes bootstrapped before the baked schema was updated are covered.
-- -----------------------------------------------------------------------

DO $contacts_actor_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname    = 'contacts_actor_id_fkey'
           AND conrelid   = 'sn_config.contacts'::regclass
    ) THEN
        ALTER TABLE sn_config.contacts
            ADD CONSTRAINT contacts_actor_id_fkey
            FOREIGN KEY (actor_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL;
    END IF;
END
$contacts_actor_fk$;

-- -----------------------------------------------------------------------
-- 6. Populate actor_id on sn_config.contacts
--    id=1: the owner -> user:admin (same actor as users id=1, no second actor).
--          This is the join whose absence caused the 2026-09-05 failure.
--    id=2: external partner Claude -> agent:partner-claude (distinct external agent, inserted above).
-- -----------------------------------------------------------------------

UPDATE sn_config.contacts
   SET actor_id = 'user:admin'
 WHERE id = 1
   AND actor_id IS NULL;

UPDATE sn_config.contacts
   SET actor_id = 'agent:partner-claude'
 WHERE id = 2
   AND actor_id IS NULL;

-- -----------------------------------------------------------------------
-- 7. Assert zero-null kind/relation; fail loudly if any actor is unclassified.
--    Quote the output in the task comment after the migration runs.
-- -----------------------------------------------------------------------

DO $assert_classified$
DECLARE
    null_count integer;
    sample_rows text;
BEGIN
    SELECT count(*) INTO null_count
      FROM sn_core.actors
     WHERE kind IS NULL OR relation IS NULL;

    IF null_count > 0 THEN
        SELECT string_agg(id, ', ' ORDER BY id) INTO sample_rows
          FROM (
              SELECT id FROM sn_core.actors
               WHERE kind IS NULL OR relation IS NULL
               LIMIT 10
          ) s;
        RAISE EXCEPTION
            'Migration 037 aborted: % actor(s) still have NULL kind or relation. '
            'First 10 IDs: %. Extend the classification rule in step 2 to cover '
            'any new prefix, then re-run.',
            null_count, sample_rows;
    END IF;
END
$assert_classified$;

-- -----------------------------------------------------------------------
-- 8. Apply NOT NULL constraints on kind and relation
-- -----------------------------------------------------------------------

ALTER TABLE sn_core.actors
    ALTER COLUMN kind     SET NOT NULL,
    ALTER COLUMN relation SET NOT NULL;
