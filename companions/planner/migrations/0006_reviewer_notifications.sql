-- 0006_reviewer_notifications.sql
-- Adds a reviewer column to tasks and makes task_notifications.notify_target
-- nullable so the pipeline can record events even when no target is resolved.
--
-- reviewer: the agent or human who reviews a task in pending_review. FK to
--   actors(id) ON DELETE SET NULL. NULL means no reviewer assigned; the
--   pipeline logs this as a fault but still allows the pending_review
--   transition so the status change is never blocked by notification gaps.
--
-- notify_target nullable: the pipeline must write a task_notifications row
--   on every status transition, including transitions where neither a reviewer
--   nor an assignee is set. A null notify_target records the event for audit
--   without implying a consumer was reached. Existing rows with a non-null
--   value are unaffected; the constraint is dropped, no default is added.

ALTER TABLE tasks
    ADD COLUMN reviewer TEXT REFERENCES actors(id) ON DELETE SET NULL;

CREATE INDEX idx_tasks_reviewer ON tasks(reviewer);

COMMENT ON COLUMN tasks.reviewer IS
    'Agent or human who reviews this task when it reaches pending_review. '
    'FK to actors.id ON DELETE SET NULL. NULL means no reviewer is assigned; '
    'the pipeline logs a fault and proceeds rather than blocking the transition.';

ALTER TABLE task_notifications
    ALTER COLUMN notify_target DROP NOT NULL;

COMMENT ON COLUMN task_notifications.notify_target IS
    'Wake or delivery target. NULL when no reviewer or assignee was available '
    'at notification time; the row is still written for audit, but no wake is '
    'fired and fired_at remains NULL.';
