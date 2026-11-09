-- 024: the per-agent wake flag
--
-- Until now the whole wake signal was "does this agent have an inbound message
-- still in state new". Tasks were not in it and could not be: the planner is a
-- separate database in its own container, so no query on this node can join it.
-- The per-agent cron existed only to paper over that, at the cost of waking
-- every agent every minute whether or not there was anything to do.
--
-- The flag closes it in the other direction: the planner already PUSHES on every
-- assignee set/change (planner/notify.py -> POST /api/notify), and core-api
-- landed that call nowhere for agent assignees. Now it lands here.
--
-- ONE mark per agent, not one row per event: ten assignments are one wake.
-- A timestamp, not a boolean, so the run that clears it can tell whether more
-- work arrived while it was running.

ALTER TABLE sn_core.agent_runtime ADD COLUMN IF NOT EXISTS wake_requested_at timestamp with time zone;
ALTER TABLE sn_core.agent_runtime ADD COLUMN IF NOT EXISTS wake_seen_at timestamp with time zone;

-- The Runner asks "who has work" every tick; this keeps the flag half of that
-- question off a sequential scan once a node carries many agents.
CREATE INDEX IF NOT EXISTS agent_runtime_wake_idx
    ON sn_core.agent_runtime USING btree (wake_requested_at)
    WHERE (wake_requested_at IS NOT NULL);
