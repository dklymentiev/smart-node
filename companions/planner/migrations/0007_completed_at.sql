-- 0007_completed_at.sql
-- Two related fixes for the completed_at column.
--
-- PROBLEM (found 2026-09-05): every task closed since 2026-09-01 has
-- completed_at NULL (131 rows). The root cause: commit 20e6abe (2026-08-31)
-- routed PATCH /api/v1/tasks/{id} through Pipeline.status_change(), which is
-- correct for audit trail purposes, but status_change() did not inject
-- completed_at into the UPDATE. The only method that did -- Pipeline.verify()
-- -- was never called from any production path. The code fix (pipeline.py)
-- addresses all future writes. This migration addresses the past.
--
-- PART 1: DB trigger as belt-and-suspenders.
-- Catches any writer that bypasses the Python pipeline (raw psql, panel,
-- a future REST client that calls TasksRepo.update directly).
-- Fires BEFORE UPDATE; never overwrites a caller-supplied value.

CREATE OR REPLACE FUNCTION set_completed_at_on_done() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'done' AND (OLD.status IS DISTINCT FROM 'done') THEN
        -- Only set if the caller left it NULL; preserve explicit values
        -- (e.g. backfill migrations, or a caller correcting a timestamp).
        IF NEW.completed_at IS NULL THEN
            NEW.completed_at = now();
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_completed_at ON tasks;
CREATE TRIGGER trg_set_completed_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_completed_at_on_done();

-- PART 2: backfill.
-- For every done task with completed_at NULL, take the latest task_log row
-- whose action='status_changed' and new_value='done'. This recovers the
-- actual closing timestamp rather than guessing with now() or updated_at.
-- Tasks whose task_log row predates the earliest log entry (task_log.id < 10,
-- per the admin's note) are left NULL and reported at the bottom. A
-- subsequent manual correction for those rows is a separate operator action.

UPDATE tasks t
SET completed_at = tl.created_at
FROM (
    SELECT DISTINCT ON (task_id) task_id, created_at
    FROM task_log
    WHERE action = 'status_changed'
      AND new_value = 'done'
    ORDER BY task_id, created_at DESC
) tl
WHERE t.id = tl.task_id
  AND t.status = 'done'
  AND t.completed_at IS NULL;

-- Surface any rows that could not be recovered (no task_log entry exists).
-- This is informational; the migration still succeeds. The operator sees
-- the count in the migration runner output because DO blocks print via RAISE.
DO $$
DECLARE
    remaining INTEGER;
BEGIN
    SELECT COUNT(*) INTO remaining
    FROM tasks
    WHERE status = 'done' AND completed_at IS NULL;

    IF remaining > 0 THEN
        RAISE NOTICE
            'completed_at backfill: % done task(s) still have completed_at NULL '
            '(no task_log entry found for their done transition). '
            'Set completed_at manually for these task ids: %',
            remaining,
            (SELECT string_agg(id::text, ', ' ORDER BY id)
             FROM tasks
             WHERE status = 'done' AND completed_at IS NULL);
    ELSE
        RAISE NOTICE 'completed_at backfill: all done tasks recovered.';
    END IF;
END;
$$;
