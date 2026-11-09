-- 0010_status_model.sql
-- Three statuses and two adjustable dictionaries.
--
-- WHY. The task row carried eleven statuses and a fixed transition matrix,
-- duplicated in pipeline.py and in status_transitions. Three different things
-- were folded into one column: whether the task moves (open / in_progress /
-- done), what it is waiting for (awaiting_approval / pending_review / blocked /
-- escalated) and how it ended (done / rejected). Every new need became a new
-- status, and a new status was a release of the planner, the Studio and every
-- flow that writes one. The owner of a node in the field asked for the
-- opposite: few statuses enforced by the planner, the reasons as data the node
-- can extend itself, and no transition matrix.
--
-- THE MODEL.
--   tasks.status        queued | in_progress | closed      (CHECK, forever)
--   tasks.wait_reason   why a queued task is not being worked on; a row of
--                       wait_reasons, NULL = simply waiting for someone to
--                       pick it up. Only meaningful while status = queued.
--   tasks.close_outcome how a closed task ended; a row of close_outcomes,
--                       required when status = closed, forbidden otherwise.
--   tasks.stage         the ONE-WORD LEGACY NAME of the task's situation
--                       (open, awaiting_approval, pending_review, blocked,
--                       escalated, in_progress, done, rejected). Derived by
--                       trigger from the three columns above through the
--                       dictionaries (each dictionary row names the legacy
--                       stage it shows as). Readers that only know the old
--                       names read this column; writers that only know the old
--                       names write it and the trigger derives the real state.
--                       A compatibility projection, not a second source of
--                       truth: it is never written by hand.
--
-- Adding a reason or an outcome is an INSERT into a dictionary table, done
-- from the panel. No release, no migration, no code.
--
-- Legacy stages that are not a state of their own collapse: assessing and
-- incident become in_progress (an agent assessing a task is working on it; an
-- incident reopens a task into work and the fact lives in the task relations
-- and comments the incident endpoint writes).
--
-- The transition matrix is gone. The pipeline keeps its guards (a close needs a
-- report, only the reviewer closes a reviewed task, a decision answers only a
-- task that is waiting for one); which status may follow which is no longer a
-- rule. From in_progress a task may close with any outcome in one move, which
-- is what the flows needed and used to fake with a hop through escalated.
--
-- The migration runner wraps each file in one transaction; SET LOCAL below
-- lives inside it.

-- ---------------------------------------------------------------------------
-- 1. Dictionaries
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wait_reasons (
    code          text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{0,31}$'),
    label         text NOT NULL,
    description   text NOT NULL DEFAULT '',
    -- The legacy one-word stage a queued task with this reason shows as.
    legacy_stage  text NOT NULL DEFAULT 'blocked'
                  CHECK (legacy_stage IN ('open', 'awaiting_approval', 'pending_review', 'blocked', 'escalated')),
    -- When several rows share a legacy stage, the default one is what a
    -- legacy writer gets ("status: blocked" becomes this reason).
    legacy_default boolean NOT NULL DEFAULT false,
    -- Side effects, declared and visible. Known keys:
    --   notify: "assignee" | "reviewer" | "owner" | "none"  (who is woken when
    --           a task enters the queue with this reason; default assignee)
    --   comment_required: true | false  (a stop must say why; default false)
    effects       jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled       boolean NOT NULL DEFAULT true,
    sort_order    integer NOT NULL DEFAULT 100,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text
);

CREATE TABLE IF NOT EXISTS close_outcomes (
    code          text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{0,31}$'),
    label         text NOT NULL,
    description   text NOT NULL DEFAULT '',
    -- The legacy one-word stage a closed task with this outcome shows as.
    legacy_stage  text NOT NULL DEFAULT 'rejected'
                  CHECK (legacy_stage IN ('done', 'rejected')),
    legacy_default boolean NOT NULL DEFAULT false,
    -- Known keys:
    --   success: true | false   (counts as delivered work in progress numbers)
    --   comment_required: true | false  (default true: a close carries a report)
    --   agent_may_set: true | false (an agent may close with this outcome on
    --           its own; default true for completed, false for the rest: a
    --           task that could not be done is a person's call)
    effects       jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled       boolean NOT NULL DEFAULT true,
    sort_order    integer NOT NULL DEFAULT 100,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text
);

-- One default per legacy stage, so the legacy-to-state direction is a function.
CREATE UNIQUE INDEX IF NOT EXISTS wait_reasons_legacy_default_uq
    ON wait_reasons (legacy_stage) WHERE legacy_default;
CREATE UNIQUE INDEX IF NOT EXISTS close_outcomes_legacy_default_uq
    ON close_outcomes (legacy_stage) WHERE legacy_default;

INSERT INTO wait_reasons (code, label, description, legacy_stage, legacy_default, effects, sort_order) VALUES
  ('approval',   'Awaiting approval', 'The owner has not yet approved the task or its plan.',
                 'awaiting_approval', true,  '{"notify": "assignee"}', 10),
  ('review',     'In review',         'Work is done and waits for the reviewer to accept or bounce it.',
                 'pending_review',    true,  '{"notify": "reviewer"}', 20),
  ('owner',      'Owner decision',    'Stopped until the owner decides what to do.',
                 'escalated',         true,  '{"notify": "assignee", "comment_required": true}', 30),
  ('person',     'Waiting on a person', 'Someone has to supply or correct something before work can go on.',
                 'blocked',           true,  '{"notify": "assignee", "comment_required": true}', 40),
  ('dependency', 'Waiting on a task', 'Blocked by another task (blocked_by).',
                 'blocked',           false, '{"notify": "assignee", "comment_required": true}', 50),
  ('schedule',   'Scheduled',         'Not before a date or time (external_detail says when).',
                 'blocked',           false, '{"notify": "assignee", "comment_required": true}', 60),
  ('error',      'Error',             'A technical failure stopped the work; needs a person to look.',
                 'blocked',           false, '{"notify": "assignee", "comment_required": true}', 70)
ON CONFLICT (code) DO NOTHING;

INSERT INTO close_outcomes (code, label, description, legacy_stage, legacy_default, effects, sort_order) VALUES
  ('completed',    'Completed',    'Done as asked; the acceptance items are met.',
                   'done',     true,  '{"success": true, "agent_may_set": true}', 10),
  ('cancelled',    'Cancelled',    'Decided against while the work was under way.',
                   'rejected', false, '{"success": false}', 20),
  ('rejected',     'Rejected',     'Refused on intake or before execution began.',
                   'rejected', true,  '{"success": false}', 30),
  ('not_possible', 'Not possible', 'Could not be done as asked; a person confirmed it.',
                   'rejected', false, '{"success": false}', 40),
  ('duplicate',    'Duplicate',    'Already covered by another task.',
                   'rejected', false, '{"success": false, "agent_may_set": true}', 50)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. The task columns
-- ---------------------------------------------------------------------------

ALTER TABLE tasks RENAME COLUMN status TO stage;
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
ALTER INDEX IF EXISTS idx_tasks_status RENAME TO idx_tasks_stage;

ALTER TABLE tasks
    ADD COLUMN status        text NOT NULL DEFAULT 'queued',
    ADD COLUMN wait_reason   text REFERENCES wait_reasons(code) ON UPDATE CASCADE,
    ADD COLUMN close_outcome text REFERENCES close_outcomes(code) ON UPDATE CASCADE;

-- ---------------------------------------------------------------------------
-- 3. The two directions, as SQL functions reading the dictionaries
-- ---------------------------------------------------------------------------

-- stage_for: the legacy one-word name of a state.
CREATE OR REPLACE FUNCTION stage_for(p_status text, p_wait text, p_outcome text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    s text;
BEGIN
    IF p_status = 'in_progress' THEN
        RETURN 'in_progress';
    ELSIF p_status = 'closed' THEN
        SELECT legacy_stage INTO s FROM close_outcomes WHERE code = p_outcome;
        RETURN coalesce(s, 'done');
    ELSE
        IF p_wait IS NULL THEN
            RETURN 'open';
        END IF;
        SELECT legacy_stage INTO s FROM wait_reasons WHERE code = p_wait;
        RETURN coalesce(s, 'blocked');
    END IF;
END;
$$;

-- state_for: the state a legacy one-word name means (the default row of the
-- dictionary that shows as that stage).
CREATE OR REPLACE FUNCTION state_for(p_stage text,
                                     OUT o_status text, OUT o_wait text, OUT o_outcome text)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    o_wait := NULL; o_outcome := NULL;
    IF p_stage IN ('in_progress', 'assessing', 'incident') THEN
        o_status := 'in_progress';
    ELSIF p_stage IN ('done', 'rejected') THEN
        o_status := 'closed';
        SELECT code INTO o_outcome FROM close_outcomes
         WHERE legacy_stage = p_stage AND legacy_default LIMIT 1;
        IF o_outcome IS NULL THEN
            SELECT code INTO o_outcome FROM close_outcomes
             WHERE legacy_stage = p_stage ORDER BY sort_order, code LIMIT 1;
        END IF;
        IF o_outcome IS NULL THEN
            RAISE EXCEPTION 'no close_outcome shows as legacy stage %', p_stage
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF p_stage = 'open' THEN
        o_status := 'queued';
    ELSIF p_stage IN ('awaiting_approval', 'pending_review', 'blocked', 'escalated') THEN
        o_status := 'queued';
        SELECT code INTO o_wait FROM wait_reasons
         WHERE legacy_stage = p_stage AND legacy_default LIMIT 1;
        IF o_wait IS NULL THEN
            SELECT code INTO o_wait FROM wait_reasons
             WHERE legacy_stage = p_stage ORDER BY sort_order, code LIMIT 1;
        END IF;
        IF o_wait IS NULL THEN
            RAISE EXCEPTION 'no wait_reason shows as legacy stage %', p_stage
                USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        RAISE EXCEPTION 'unknown task stage %', p_stage USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 4. Data: every existing row gets its state from its old status
-- ---------------------------------------------------------------------------

SET LOCAL planner.bypass_transitions = 'on';

UPDATE tasks SET status = 'in_progress', wait_reason = NULL, close_outcome = NULL
 WHERE stage IN ('in_progress', 'assessing', 'incident');

UPDATE tasks SET status = 'queued', wait_reason = NULL, close_outcome = NULL
 WHERE stage = 'open';

UPDATE tasks SET status = 'queued', wait_reason = 'approval', close_outcome = NULL
 WHERE stage = 'awaiting_approval';

UPDATE tasks SET status = 'queued', wait_reason = 'review', close_outcome = NULL
 WHERE stage = 'pending_review';

UPDATE tasks SET status = 'queued', wait_reason = 'owner', close_outcome = NULL
 WHERE stage = 'escalated';

-- blocked: the finer reason comes from the columns and tags the old model used.
UPDATE tasks t SET status = 'queued', close_outcome = NULL,
       wait_reason = CASE
           WHEN t.blocked_by IS NOT NULL OR t.blocked_reason = 'task_dependency' THEN 'dependency'
           WHEN t.blocked_reason = 'scheduled_time' THEN 'schedule'
           WHEN EXISTS (SELECT 1 FROM task_tags g WHERE g.task_id = t.id
                          AND g.tag IN ('blocked:error', 'error:crash')) THEN 'error'
           ELSE 'person'
       END
 WHERE stage = 'blocked';

UPDATE tasks SET status = 'closed', wait_reason = NULL, close_outcome = 'completed'
 WHERE stage = 'done';

UPDATE tasks SET status = 'closed', wait_reason = NULL, close_outcome = 'rejected'
 WHERE stage = 'rejected';

-- The projection column now follows from the state (assessing/incident rows
-- read in_progress from here on).
UPDATE tasks SET stage = stage_for(status, wait_reason, close_outcome)
 WHERE stage IS DISTINCT FROM stage_for(status, wait_reason, close_outcome);

-- previous_status (the stage a blocked task returns to) collapses the same way.
UPDATE tasks SET previous_status = 'in_progress'
 WHERE previous_status IN ('assessing', 'incident');

-- ---------------------------------------------------------------------------
-- 5. Constraints
-- ---------------------------------------------------------------------------

ALTER TABLE tasks
    ADD CONSTRAINT tasks_status_check
        CHECK (status IN ('queued', 'in_progress', 'closed')),
    ADD CONSTRAINT tasks_close_outcome_check
        CHECK ((status = 'closed') = (close_outcome IS NOT NULL)),
    ADD CONSTRAINT tasks_wait_reason_check
        CHECK (wait_reason IS NULL OR status = 'queued');

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_wait_reason ON tasks (wait_reason) WHERE wait_reason IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. Triggers: the projection, completed_at, and no matrix
-- ---------------------------------------------------------------------------

-- Keep stage and (status, wait_reason, close_outcome) in step, whichever side
-- the writer touched. A writer that changes the state gets its stage derived;
-- a legacy writer that only changes the stage gets its state derived. A write
-- that changes both must agree, otherwise it is refused: two truths is the
-- bug this migration removes.
CREATE OR REPLACE FUNCTION tasks_sync_stage() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    st record;
    state_changed boolean;
    stage_changed boolean;
BEGIN
    -- A legacy writer that puts a stage name into ``status`` (an old script,
    -- a raw UPDATE tasks SET status = 'done') is read as writing the stage.
    -- BEFORE triggers run before the CHECK constraints, so the row that is
    -- checked is the corrected one.
    IF NEW.status IS NOT NULL AND NEW.status NOT IN ('queued', 'in_progress', 'closed') THEN
        NEW.stage := NEW.status;
        IF TG_OP = 'UPDATE' THEN
            NEW.status := OLD.status;
            NEW.wait_reason := OLD.wait_reason;
            NEW.close_outcome := OLD.close_outcome;
        ELSE
            NEW.status := 'queued';
            NEW.wait_reason := NULL;
            NEW.close_outcome := NULL;
        END IF;
    END IF;

    IF TG_OP = 'INSERT' THEN
        -- Both given: the state wins and must agree with the stage.
        IF NEW.stage IS NOT NULL AND NEW.stage <> 'open'
           AND (NEW.status <> 'queued' OR NEW.wait_reason IS NOT NULL OR NEW.close_outcome IS NOT NULL) THEN
            IF stage_for(NEW.status, NEW.wait_reason, NEW.close_outcome) <> NEW.stage THEN
                RAISE EXCEPTION 'stage % does not match state (%, %, %)',
                    NEW.stage, NEW.status, NEW.wait_reason, NEW.close_outcome
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF NEW.stage IS NOT NULL AND NEW.stage <> 'open' THEN
            -- Legacy insert: only the stage says anything.
            SELECT * INTO st FROM state_for(NEW.stage);
            NEW.status := st.o_status; NEW.wait_reason := st.o_wait; NEW.close_outcome := st.o_outcome;
        END IF;
        NEW.stage := stage_for(NEW.status, NEW.wait_reason, NEW.close_outcome);
        RETURN NEW;
    END IF;

    state_changed := NEW.status IS DISTINCT FROM OLD.status
                  OR NEW.wait_reason IS DISTINCT FROM OLD.wait_reason
                  OR NEW.close_outcome IS DISTINCT FROM OLD.close_outcome;
    stage_changed := NEW.stage IS DISTINCT FROM OLD.stage;

    IF state_changed THEN
        IF stage_changed AND stage_for(NEW.status, NEW.wait_reason, NEW.close_outcome) <> NEW.stage THEN
            RAISE EXCEPTION 'stage % does not match state (%, %, %)',
                NEW.stage, NEW.status, NEW.wait_reason, NEW.close_outcome
                USING ERRCODE = 'check_violation';
        END IF;
        -- Leaving the queue clears the wait; leaving closed clears the outcome.
        IF NEW.status <> 'queued' THEN NEW.wait_reason := NULL; END IF;
        IF NEW.status <> 'closed' THEN NEW.close_outcome := NULL; END IF;
        NEW.stage := stage_for(NEW.status, NEW.wait_reason, NEW.close_outcome);
    ELSIF stage_changed THEN
        SELECT * INTO st FROM state_for(NEW.stage);
        NEW.status := st.o_status; NEW.wait_reason := st.o_wait; NEW.close_outcome := st.o_outcome;
        NEW.stage := stage_for(NEW.status, NEW.wait_reason, NEW.close_outcome);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tasks_sync_stage ON tasks;
CREATE TRIGGER trg_tasks_sync_stage
    BEFORE INSERT OR UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION tasks_sync_stage();

-- completed_at: every close stamps it (0007 did this for done only).
CREATE OR REPLACE FUNCTION set_completed_at_on_done() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'closed' AND (OLD.status IS DISTINCT FROM 'closed') THEN
        IF NEW.completed_at IS NULL THEN
            NEW.completed_at = now();
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The trigger order is alphabetical in Postgres: trg_set_completed_at runs
-- before trg_tasks_sync_stage, so a legacy "stage = 'done'" write would not
-- yet have status = 'closed' when completed_at is decided. Rename it so the
-- projection runs first.
DROP TRIGGER IF EXISTS trg_set_completed_at ON tasks;
DROP TRIGGER IF EXISTS trg_z_set_completed_at ON tasks;
CREATE TRIGGER trg_z_set_completed_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_completed_at_on_done();

-- No transition matrix. The table and its trigger go; the pipeline's guards
-- stay in the application where they can say why they refuse.
DROP TRIGGER IF EXISTS trg_enforce_status_transition ON tasks;
DROP FUNCTION IF EXISTS enforce_status_transition();
DROP TABLE IF EXISTS status_transitions;

COMMENT ON COLUMN tasks.status IS
    'queued | in_progress | closed. The only three. What the task is waiting for is wait_reason; how it ended is close_outcome.';
COMMENT ON COLUMN tasks.wait_reason IS
    'Row of wait_reasons while status = queued (NULL = waiting for someone to pick it up). Cleared by trigger when the task leaves the queue.';
COMMENT ON COLUMN tasks.close_outcome IS
    'Row of close_outcomes; required when status = closed, cleared otherwise.';
COMMENT ON COLUMN tasks.stage IS
    'Legacy one-word projection of (status, wait_reason, close_outcome) through the dictionaries, kept by trigger. Read it if you only know the old names; never write it by hand.';
COMMENT ON TABLE wait_reasons IS
    'Why a queued task is not being worked on. Node-editable; adding a row needs no release.';
COMMENT ON TABLE close_outcomes IS
    'How a closed task ended. Node-editable; adding a row needs no release.';

COMMIT;
