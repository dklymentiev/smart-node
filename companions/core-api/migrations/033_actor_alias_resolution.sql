-- 033: actor alias resolution
--
-- Adds canonical_id and aliased_at to sn_core.actors. Populates the alias
-- mapping  Decision 2 and 7. Creates the v_actor_events view.
--
-- Additive: nothing that works today stops working. Readers that GROUP BY actor
-- should move to canonical_actor from v_actor_events; agent-state-grid.py is
-- updated in the same commit. The write paths (_ensure_actor callers) need no
-- change: they INSERT with ON CONFLICT DO NOTHING and never read back.
--
-- NOT IN THIS MIGRATION: dropping canonical_id, aliased_at, or the view.
-- That is step 9, one release cycle later, after events are rewritten.
--
-- Release: 1.12.33. Blocks steps 2, 3, 4, 9.

-- -----------------------------------------------------------------------
-- 1. Schema columns
-- -----------------------------------------------------------------------

ALTER TABLE sn_core.actors
    ADD COLUMN IF NOT EXISTS canonical_id text,
    ADD COLUMN IF NOT EXISTS aliased_at   timestamp with time zone;

-- FK: canonical_id -> sn_core.actors(id). Self-referential; ON DELETE SET NULL
-- so that removing a canonical row leaves its aliases uncorrupted.
DO $add_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname    = 'actors_canonical_id_fkey'
           AND conrelid   = 'sn_core.actors'::regclass
    ) THEN
        ALTER TABLE sn_core.actors
            ADD CONSTRAINT actors_canonical_id_fkey
            FOREIGN KEY (canonical_id) REFERENCES sn_core.actors(id) ON DELETE SET NULL;
    END IF;
END
$add_fk$;

-- CHECK: a row cannot be its own canonical. The FK alone does not prevent this
-- because canonical_id = id satisfies the foreign key (the target exists) and
-- produces a circular COALESCE in v_actor_events. NULL passes this check (NULL
-- means the row IS canonical, not that it points to itself).
DO $add_chk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname    = 'actors_canonical_id_self_chk'
           AND conrelid   = 'sn_core.actors'::regclass
    ) THEN
        ALTER TABLE sn_core.actors
            ADD CONSTRAINT actors_canonical_id_self_chk CHECK (canonical_id <> id);
    END IF;
END
$add_chk$;

-- -----------------------------------------------------------------------
-- 2. Provision canonical rows that may be absent
-- -----------------------------------------------------------------------

-- component:installer: canonical for the user:system:installer prefix-bug row.
-- _ensure_actor received 'system:installer', split on ':', produced type=user
-- and id='user:system:installer'. The correct canonical is component:installer.
INSERT INTO sn_core.actors (id, display_name, type, layer)
VALUES ('component:installer', 'Component: installer', 'component', 'system')
ON CONFLICT (id) DO NOTHING;

-- agent: counterparts for bare-name slug rows (architect, foreman, sysadmin).
-- These bare names exist because _ensure_actor was called with a name that had
-- no colon, so the whole string was used as both id and type.
INSERT INTO sn_core.actors (id, display_name, type, layer)
SELECT 'agent:' || a.id,
       initcap(a.id),
       'agent',
       'specialist'
  FROM sn_core.actors a
 WHERE a.id NOT LIKE '%:%'
   AND a.id IN ('architect', 'foreman', 'sysadmin')
ON CONFLICT (id) DO NOTHING;

-- component:cron: canonical for the bare 'cron' row.
-- 'cron' is the node scheduler, not an agent with a queue. kind=component,
-- relation=internal (owner confirmed). Same bucket as cron:* rows.
INSERT INTO sn_core.actors (id, display_name, type, layer)
SELECT 'component:' || a.id,
       initcap(a.id),
       'component',
       'system'
  FROM sn_core.actors a
 WHERE a.id NOT LIKE '%:%'
   AND a.id = 'cron'
ON CONFLICT (id) DO NOTHING;

-- component: counterparts for non-agent bare-name rows (anonymous, unknown-peer).
INSERT INTO sn_core.actors (id, display_name, type, layer)
SELECT 'component:' || a.id,
       initcap(replace(a.id, '-', ' ')),
       'component',
       'system'
  FROM sn_core.actors a
 WHERE a.id NOT LIKE '%:%'
   AND a.id IN ('anonymous', 'unknown-peer')
ON CONFLICT (id) DO NOTHING;

-- component:claude: canonical for the operator. the actor-alias rule is explicit --
-- the operator runs as a system service, has no agents row, no folder, no run.sh
-- and no schedule, so it is a component and not a participant. Without this row
-- user:claude stays canonical and the operator is classified as a person (73
-- events on example-node, found by running these migrations against a copy).
INSERT INTO sn_core.actors (id, display_name, type, layer)
VALUES ('component:claude', 'Component: claude', 'component', 'system')
ON CONFLICT (id) DO NOTHING;

-- component:panel: canonical for user:panel, the admin panel's own actor. It is
-- a surface of this node, not a person; core-api's own tests already describe it
-- as a seeded FK target. 118 events on example-node.
INSERT INTO sn_core.actors (id, display_name, type, layer)
VALUES ('component:panel', 'Component: panel', 'component', 'system')
ON CONFLICT (id) DO NOTHING;

-- -----------------------------------------------------------------------
-- 3. Populate canonical_id on alias rows
-- -----------------------------------------------------------------------

-- Owner group: user:admin is canonical. Bare 'admin' and user:owner are aliases
-- of the same person, written by paths that named the owner differently.
UPDATE sn_core.actors
   SET canonical_id = 'user:admin',
       aliased_at   = now()
 WHERE id IN ('admin', 'user:owner')
   AND canonical_id IS NULL
   AND EXISTS (SELECT 1 FROM sn_core.actors WHERE id = 'user:admin');

-- Installer prefix-bug: user:system:installer -> component:installer.
UPDATE sn_core.actors
   SET canonical_id = 'component:installer',
       aliased_at   = now()
 WHERE id = 'user:system:installer'
   AND canonical_id IS NULL;

-- Agent group: user:<slug> -> agent:<slug>.
-- Joins against sn_config.agents so every current agent slug is covered without
-- hard-coding the list. Only aliases whose canonical row actually exists are set;
-- if the agent:<slug> row is absent (unexpected), the alias is left unresolved
-- rather than pointing at a non-existent canonical.
UPDATE sn_core.actors a
   SET canonical_id = 'agent:' || g.slug,
       aliased_at   = now()
  FROM sn_config.agents g
 WHERE a.id = 'user:' || g.slug
   AND EXISTS (SELECT 1 FROM sn_core.actors c WHERE c.id = 'agent:' || g.slug)
   AND a.canonical_id IS NULL;

-- Non-person user: rows -> their component canonical.
-- _ensure_actor built these with a 'user:' prefix because that is what the caller
-- passed, not because anyone decided they were people. Left alone they stay
-- canonical and the prefix rule in step 4 of the migration classifies them as
-- humans: on example-node that produced eight 'humans' where there is one person,
-- carrying 484 events between them. The component counterparts for
-- anonymous and unknown-peer are already provisioned above for the bare-name
-- rows; claude and panel are provisioned above for this group.
UPDATE sn_core.actors a
   SET canonical_id = 'component:' || substring(a.id from 6),
       aliased_at   = now()
 WHERE a.id IN ('user:anonymous', 'user:unknown-peer', 'user:panel', 'user:claude')
   AND a.canonical_id IS NULL
   AND EXISTS (SELECT 1 FROM sn_core.actors c
                WHERE c.id = 'component:' || substring(a.id from 6));

-- Bare-name group: slug aliases -> agent:* or component:* canonical.
UPDATE sn_core.actors
   SET canonical_id = CASE
           WHEN id IN ('architect', 'foreman', 'sysadmin')
               THEN 'agent:' || id
           WHEN id IN ('anonymous', 'unknown-peer', 'cron')
               THEN 'component:' || id
       END,
       aliased_at = now()
 WHERE id NOT LIKE '%:%'
   AND id IN ('architect', 'foreman', 'sysadmin', 'cron', 'anonymous', 'unknown-peer')
   AND canonical_id IS NULL;

-- -----------------------------------------------------------------------
-- 4. View
-- -----------------------------------------------------------------------

-- v_actor_events adds canonical_actor to every event row. Use canonical_actor
-- instead of actor whenever grouping or filtering by who acted, so that alias
-- events count under the canonical identity rather than the alias.
CREATE OR REPLACE VIEW sn_core.v_actor_events AS
SELECT e.*,
       COALESCE(a.canonical_id, e.actor) AS canonical_actor
  FROM sn_core.events e
  LEFT JOIN sn_core.actors a ON a.id = e.actor;
