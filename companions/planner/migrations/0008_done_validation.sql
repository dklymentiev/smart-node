-- 0008_done_validation.sql
-- Adds the done -> pending_review edge to the DB-level transition table.
--
-- CONTEXT: the pipeline now enforces two rules on the done transition:
--   1. A comment is required (the closer must quote evidence for each
--      acceptance item; "the report" in the plan-progress-report policy).
--   2. When a reviewer is set, only that reviewer may close the task
--      (the confirmer is never the finder).
--
-- The pipeline also provides a way OUT of a wrongly-closed done state that
-- is less severe than `incident`: done -> pending_review. This says the close
-- was premature; the task goes back for another review pass. This edge must
-- be reflected in both the Python TRANSITIONS map (pipeline.py) AND this DB
-- table, because the BEFORE UPDATE trigger on tasks validates against this
-- table for every writer, including raw psql.

INSERT INTO status_transitions (from_status, to_status)
VALUES ('done', 'pending_review')
ON CONFLICT DO NOTHING;

COMMENT ON TABLE status_transitions IS
    'Allowed status transitions, mirroring pipeline.py TRANSITIONS. '
    'Enforced by trg_enforce_status_transition on BEFORE UPDATE. '
    'Emergency bypass: SET planner.bypass_transitions=''on'' in a session.';
