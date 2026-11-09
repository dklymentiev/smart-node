-- 032: cancel sessions stuck in status=active with no live process
--
-- Context: the session reaper sets status='reaped' when it kills an idle
-- claude-cli process. But if the gateway process itself restarts, or a
-- session was never cleanly closed, the DB row can be left as status='active'
-- while no process holds it. brin's durable session reuse logic treats 'active'
-- as directly sendable (no restart needed), so it tries to send to a dead pipe.
-- That either 502s (which marks the session 'failed' -- recoverable but wastes
-- one turn) or, in the stale-active + 409 variant, wedges permanently.
--
-- Fix: mark sessions that have been 'active' for more than 1 hour as 'cancelled'.
-- 1 hour safely exceeds the longest possible live turn (310s gateway timeout).
-- This is a one-time repair; the executor fix prevents recurrence.
--
-- Rows affected: 78 stale-active sessions as of 2026-09-02 (from task description).
--
-- 'cancelled' (not 'completed') because these sessions were abandoned, not
-- finished normally. The distinction matters for audit clarity.

UPDATE sn_core.ai_sessions
   SET status       = 'cancelled',
       completed_at = COALESCE(completed_at, now()),
       error        = 'closed by migration 032: status was active for >1h with no live process'
 WHERE status = 'active'
   AND last_activity_at < now() - INTERVAL '1 hour';
