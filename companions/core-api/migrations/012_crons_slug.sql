-- Migration 012: add a stable slug to sn_core.crons.
--
-- Until now cron jobs had no stable name; cron-runner.sh's JOB and the
-- by-job run mapping were derived/fuzzy-matched from the command. A slug
-- gives each job a stable identity used as the cron-runner JOB name and the
-- by-job run key.

BEGIN;

ALTER TABLE sn_core.crons ADD COLUMN IF NOT EXISTS slug TEXT;

-- Backfill existing rows with the previous derived name so nothing breaks.
UPDATE sn_core.crons SET slug = 'cron-' || id WHERE slug IS NULL;

-- Stable identity. Nulls are never written by the route (it always sets slug),
-- but the index tolerates them rather than forcing a NOT NULL rewrite.
CREATE UNIQUE INDEX IF NOT EXISTS crons_slug_uniq ON sn_core.crons(slug);

COMMIT;
