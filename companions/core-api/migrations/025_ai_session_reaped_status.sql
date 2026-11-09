-- 025: 'reaped' becomes a legal ai_sessions.status
--
-- The session reaper has been writing status='reaped' since June and the CHECK
-- constraint has never allowed it. The failure was invisible for two compounding
-- reasons, and the combination is the actual bug:
--
--   1. The reaper issues the audit INSERT and the status UPDATE as ONE psql -c
--      string, which psql runs as a SINGLE transaction. The UPDATE violates the
--      constraint, so the whole thing rolls back -- taking the event with it.
--   2. Its psql helper captures stderr and returns only stdout, so nothing is
--      printed, the timer reports success, and the journal is clean.
--
-- The result is exactly inverted from what you want: a session that was open
-- when it was reaped records NOTHING, while a session that was already closed
-- records an event, because then the UPDATE matches zero rows, raises nothing,
-- and lets the INSERT commit. Measured on reference-node, which has run this since June:
-- 2 session.reaped events, 0 sessions in status reaped.
--
-- A reaped session is genuinely its own outcome and should not be flattened into
-- 'cancelled' (a person or caller cancelled it) or 'completed' (it finished).
-- Something killed it from outside while it was idle, and the operator asking
-- "what happened to this session" deserves to be told that.

ALTER TABLE sn_core.ai_sessions DROP CONSTRAINT IF EXISTS ai_sessions_status_check;

ALTER TABLE sn_core.ai_sessions ADD CONSTRAINT ai_sessions_status_check
    CHECK (status = ANY (ARRAY[
        'active'::text,
        'streaming'::text,
        'idle_released'::text,
        'completed'::text,
        'failed'::text,
        'cancelled'::text,
        'reaped'::text
    ]));
