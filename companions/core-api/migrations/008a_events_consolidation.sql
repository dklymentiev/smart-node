-- Migration 008a -- events table consolidation.
--
-- SUPERSEDED 2026-09-09. THIS FILE RECORDS WHAT WAS DECIDED THEN, NOT WHAT
-- THE NODE DOES NOW. Do not apply it and do not reason from it.
--
-- It consolidated onto public.events and dropped sn_core.events. The
-- decision has since been reversed, deliberately and in this order:
--
--   * core-api 0.16.0 moved the writers and readers to sn_core --
--     events_emit.py, routes/events.py, routes/system.py, lib/projector.py --
--     and B02-sn-core-schema.sql started creating sn_core.events again.
--   * sn_core.v_actor_events reads sn_core.events.
--   * bootstrap/lib.sh nevertheless kept emitting `INSERT INTO events`
--     unqualified, so install-time events went to public.events and the
--     node carried TWO journals with no row in common.
--   * 2026-09-09 the owner settled it: sn_core.events is the journal, and
--     public.events is dropped by B02-sn-core-seed.sql after its rows are
--     carried across. There is no second table and no second name.
--
-- 008a's own reason for the public direction was that events must be
-- attributable from the earliest bootstrap step. That reason still holds and
-- is still met: sn_core is created by B02, in bootstrap, before any companion
-- runs. It never required the table to live in `public`.
--
-- Kept, not deleted, because it is the record of a decision that was made on
-- purpose and reversed on purpose. Nothing applies this directory --
-- companions/core-api.sh says so at line 26 -- so it is history, not code.
-- ORIGINAL HEADER FOLLOWS.
--
-- Fixes a duplication mistake from Migration 007: sn_core.events was
-- created without realizing public.events already exists in B02 schema
-- (created by bootstrap/B02-schema.sql and the mesh
-- the event read model). B02-schema explicitly designs events to live
-- in public/kernel schema because "every action (stage runs, companion
-- installs, agent activity) must be attributable to an actor from the
-- earliest bootstrap step. If events were a companion, all of Phase 1
-- + the first companion installs would run unjournaled."
--
-- This migration:
--   1. Adds parent_event_id column to public.events (was missing; it carries the
--      causality chain).
--   2. Inserts the actors that emit() calls produced during Foundation
--      Milestone. public.events.actor is FK to public.actors,
--      and these actors did not yet exist there.
--   3. Migrates all rows from sn_core.events into public.events,
--      preserving order via ts. parent_event_id is always NULL on the
--      source rows so no id-mapping is needed.
--   4. Drops sn_core.events as duplicate.
--
-- Code changes ride along with this SQL:
--   - events_emit.py: INSERT INTO sn_core.events -> public.events
--   - routes/events.py: FROM sn_core.events -> public.events
--   - routes/system.py: SELECT ts FROM sn_core.events -> public.events,
--                       and COUNT(*) AS n FROM sn_core.events -> public.events
--   - routes/crons.py: docstring updated for consistency
--
-- After this migration sn_core retains crons, cron_runs, tools.
-- Those are net-new tables (no B02 duplicate, verified).
--
-- Smoke on the node: 25 (existing) + 885 (migrated) = 910 rows in
-- public.events; service restart + heartbeat tick produces id=911 with
-- actor=component:core-api.

\set ON_ERROR_STOP on
SET ROLE smartnode;

BEGIN;

-- 1. Add parent_event_id (column for the causality chain)
ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS parent_event_id BIGINT REFERENCES public.events(id);

-- 2. Insert missing actors used by core-api emit() helper
INSERT INTO public.actors (id, display_name, type, layer) VALUES
  ('component:core-api', 'Core API Controller', 'system', 'orchestrator'),
  ('system:cron',        'Cron Runner Wrapper', 'system', 'orchestrator'),
  ('system:heartbeat',   'Heartbeat Companion', 'system', 'guard'),
  ('test:smoke',         'Smoke Test Actor',    'system', 'system'),
  ('untrusted',          'Untrusted Caller',    'system', 'system')
ON CONFLICT (id) DO NOTHING;

-- 3. Migrate rows from sn_core.events -> public.events.
--    parent_event_id is always NULL on source so no id-mapping needed.
--    DO block makes the migration idempotent: if sn_core.events is
--    already dropped on a re-apply, the INSERT is skipped.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='sn_core' AND table_name='events'
    ) THEN
        INSERT INTO public.events (actor, event_type, entity_type, entity_id, payload, ts)
        SELECT actor, event_type, entity_type, entity_id, payload, ts
          FROM sn_core.events
         ORDER BY id;

        DROP TABLE sn_core.events;
    END IF;
END $$;

COMMIT;
