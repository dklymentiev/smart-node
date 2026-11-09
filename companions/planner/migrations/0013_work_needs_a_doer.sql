-- 0013_work_needs_a_doer.sql
-- No task is in work without somebody to do it.
--
-- WHY. In two weeks on one node 42 tasks sat in progress with nobody assigned. A task
-- that waits in the queue before a doer is named is harmless; one that is being worked
-- on with no doer is not: nothing wakes, nobody is answerable, and the owner finds it
-- later. Telling agents so in a prompt does not hold; they forget, and the planner let
-- the move through. The rule belongs where the state changes.
--
-- THE RULE, as a trigger so that no door can bypass it (REST, the Studio form, the
-- planner's own MCP, an approval, an agent moving a status, a script):
--   a task may not ENTER the in_progress state, nor lose its doer while in it, with no
--   assignee. A task bound to a flow is exempt: the flow owns who works on it.
-- Only a change is refused. A task already in progress with no doer (history) can still
-- be edited and commented; it is not rewritten.
--
-- It is named so that it fires after the trigger that derives the state from the stage.
CREATE OR REPLACE FUNCTION tasks_require_doer() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'in_progress' AND NEW.assignee IS NULL AND NEW.workflow_id IS NULL THEN
        IF TG_OP = 'INSERT'
           OR OLD.status IS DISTINCT FROM 'in_progress'
           OR OLD.assignee IS NOT NULL THEN
            RAISE EXCEPTION 'a task needs a doer before work can start: name an assignee '
                            '(the agent who does it, or the owner if it is the owner''s to do)'
                USING ERRCODE = 'check_violation', HINT = 'task_needs_doer';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_zz_require_doer ON tasks;
CREATE TRIGGER trg_zz_require_doer
    BEFORE INSERT OR UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION tasks_require_doer();
