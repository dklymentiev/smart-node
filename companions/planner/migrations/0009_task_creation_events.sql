-- Agent task-creation refusals are durable planner events, not tasks.
ALTER TABLE task_notifications ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE task_notifications ALTER COLUMN notify_target DROP NOT NULL;

