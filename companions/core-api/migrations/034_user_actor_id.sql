-- 034: actor_id on sn_config.users
--
-- Adds actor_id (text, FK -> sn_core.actors(id)) to sn_config.users so
-- that every user account resolves to a canonical actor identity. This
-- is the prerequisite for binding principal parameters in the runtime
-- without exposing actor ids to the model.
--
-- Column is nullable in this migration. NOT NULL is applied only once a
-- zero-null verification confirms every row is populated (see step 5).
--
-- Additive: no existing read or write path changes behaviour. The column
-- is ignored until the runtime code is updated in step 4.
--
-- Release: 1.12.33.

-- -----------------------------------------------------------------------
-- 1. Schema column
-- -----------------------------------------------------------------------

ALTER TABLE sn_config.users
    ADD COLUMN IF NOT EXISTS actor_id text;

-- -----------------------------------------------------------------------
-- 2. FK: actor_id -> sn_core.actors(id)
-- -----------------------------------------------------------------------
-- ON DELETE SET NULL: removing an actor row from sn_core.actors does not
-- cascade-delete the user account; the account simply loses its actor
-- reference and is treated as unresolved until a new actor is assigned.

DO $users_actor_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname    = 'users_actor_id_fkey'
           AND conrelid   = 'sn_config.users'::regclass
    ) THEN
        ALTER TABLE sn_config.users
            ADD CONSTRAINT users_actor_id_fkey
            FOREIGN KEY (actor_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL;
    END IF;
END
$users_actor_fk$;

-- -----------------------------------------------------------------------
-- 3. Provision actor rows for agents not yet present in sn_core.actors
-- -----------------------------------------------------------------------
-- Agents accumulate actor rows via events_emit._ensure_actor as they run,
-- but an agent that has never triggered an event on this node will be
-- absent. Provisioning here ensures the FK reference below succeeds for
-- every agent in the registry, including freshly added ones.
--
-- type = 'agent', layer = 'specialist' matches _LAYER_BY_PREFIX in
-- events_emit.py (prefix 'agent' -> layer 'specialist').

INSERT INTO sn_core.actors (id, display_name, type, layer)
SELECT 'agent:' || g.slug,
       COALESCE(g.title, initcap(g.slug)),
       'agent',
       'specialist'
  FROM sn_config.agents g
 WHERE NOT EXISTS (
     SELECT 1 FROM sn_core.actors a WHERE a.id = 'agent:' || g.slug
 );

-- -----------------------------------------------------------------------
-- 4. Populate actor_id
-- -----------------------------------------------------------------------

-- 4a. Users with a non-empty handle: their sn_core.actors entry is
--     'user:<handle>'. COALESCE takes the canonical if an alias was set
--     (e.g. user:owner -> canonical user:admin by migration 033).
--
-- OWNER ROW (id=1): when the owner's handle is an alias of user:admin
-- (canonical_id = 'user:admin', migration 033), After this step the owner's
-- users row carries actor_id = 'user:admin'. Step 4 must populate
-- contacts id=1 with the same actor_id so the cross-channel actor join is exact.
UPDATE sn_config.users u
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'user:' || u.handle
   AND u.handle IS NOT NULL AND u.handle <> ''
   AND u.actor_id IS NULL;

-- 4b. Agent rows (role = 'agent'): the agent's email column holds its
--     slug (see routes/agents.py upsert_user_account). Canonical actor
--     is agent:<slug>; migration 033 set canonical_id on any user:<slug>
--     alias so COALESCE resolves correctly in either direction.
UPDATE sn_config.users u
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'agent:' || u.email
   AND u.role = 'agent'
   AND u.actor_id IS NULL;

-- 4c. Non-agent rows without a handle: fall back to 'user:<email>'.
--     This catches the initial super-admin seed row (email='admin')
--     when its handle has not yet been set, and any operator account
--     whose handle was never stored.
UPDATE sn_config.users u
   SET actor_id = COALESCE(a.canonical_id, a.id)
  FROM sn_core.actors a
 WHERE a.id = 'user:' || u.email
   AND (u.handle IS NULL OR u.handle = '')
   AND u.role <> 'agent'
   AND u.actor_id IS NULL;

-- -----------------------------------------------------------------------
-- 5. NOT NULL -- applied only once every row carries an actor_id
-- -----------------------------------------------------------------------
-- If any row is still NULL after steps 3-4 (actor row could not be found
-- and was not provisioned), the ALTER TABLE is skipped and a NOTICE is
-- raised. The migration still succeeds; investigate the missing rows and
-- fix them before the NOT NULL step runs in a later migration.

DO $users_actor_not_null$
DECLARE
    null_count int;
BEGIN
    SELECT COUNT(*) INTO null_count FROM sn_config.users WHERE actor_id IS NULL;
    IF null_count = 0 THEN
        ALTER TABLE sn_config.users ALTER COLUMN actor_id SET NOT NULL;
    ELSE
        RAISE NOTICE
            'users.actor_id: % row(s) still NULL -- NOT NULL constraint not applied; '
            'investigate before step 5 migration', null_count;
    END IF;
END
$users_actor_not_null$;
