-- 0011_recurring_tasks.sql
-- Recurring tasks: a task that comes back on a schedule.
--
-- WHY. Work that repeats (a daily check, a weekly report, a monthly import)
-- had no place in the planner. A node kept it alive with a cron of its own
-- that woke an agent, and the task, if there was one, was a standing log
-- nobody could close. The planner already knows who does a task, how it is
-- approved and how it is closed with a report; it only lacked "again, every
-- N". Two columns give it that.
--
-- THE MODEL.
--   tasks.recur_interval  how often the task comes back ('1 day', '2 weeks',
--                         '1 month'); NULL = a one-off task.
--   tasks.next_run        when the next cycle opens. Set together with the
--                         interval and cleared together with it.
--
-- One task row carries the whole series. When next_run passes, the
-- recurrence sweep (Pipeline.recurrence_sweep, run by cron through
-- `planner maintenance recur`) reopens a task closed as completed to
-- in_progress, keeps approved_at (the owner approves the series once), wakes
-- the assignee and moves next_run forward by whole intervals until it is in
-- the future, so a node that was down for a week opens one cycle, not seven.
-- Each cycle is closed the ordinary way, with a report; those reports are
-- the record of the series.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS recur_interval interval,
    ADD COLUMN IF NOT EXISTS next_run timestamptz;

ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_recur_interval_positive;
ALTER TABLE tasks ADD CONSTRAINT tasks_recur_interval_positive
    CHECK (recur_interval IS NULL OR recur_interval > interval '0');

ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_recur_next_run_paired;
ALTER TABLE tasks ADD CONSTRAINT tasks_recur_next_run_paired
    CHECK ((recur_interval IS NULL) = (next_run IS NULL));

CREATE INDEX IF NOT EXISTS idx_tasks_next_run
    ON tasks (next_run) WHERE recur_interval IS NOT NULL;

COMMENT ON COLUMN tasks.recur_interval IS
    'How often a recurring task comes back; NULL for a one-off task.';
COMMENT ON COLUMN tasks.next_run IS
    'When the next cycle of a recurring task opens; set and cleared with recur_interval.';
