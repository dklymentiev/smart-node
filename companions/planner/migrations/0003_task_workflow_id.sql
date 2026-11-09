-- 0003_task_workflow_id.sql
-- Soft link from a task to the workflow/process that owns it.
-- Workflows live in core-api (GET /api/workflows -> integer id), a separate DB
-- from planner, so this is an INTEGER reference with NO foreign key constraint.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS workflow_id INTEGER;
COMMENT ON COLUMN tasks.workflow_id IS 'Soft reference to core-api workflows.id (cross-service; no FK). Nullable.';
CREATE INDEX IF NOT EXISTS tasks_workflow_id_idx ON tasks (workflow_id) WHERE workflow_id IS NOT NULL;
