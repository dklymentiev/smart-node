-- 022_channel_history_watermark.sql -- a connected mailbox is not an inbox of
-- everything it already contains.
--
-- WHAT WENT WRONG. The configurator bound a real business inbox -- months of
-- accumulated mail -- to an agent. The poller treated everything already
-- sitting in it as fresh inbound, and the agent did what it is supposed to do
-- with inbound mail: it answered. One reply carrying the node's internal status
-- reached an outside party; a second was queued behind it.
--
-- None of the existing guards could have stopped it, and each for its own
-- reason. The egress guard allowed it correctly (they had written to us). The
-- machine-mail filter classified it correctly (a marketing blast is human-sent).
-- Nothing anywhere asked "is this message from BEFORE this mailbox was
-- connected?" -- that is the missing idea, and it is orthogonal to permission:
-- a correspondent who wrote in May is legitimately allowed, and their May
-- question still gets answered as if it arrived today.
--
-- THE WATERMARK. `history_before` is the moment the channel instance was bound.
-- Everything that arrived at or before it is HISTORY: recorded (it is context
-- and evidence, and the agent can read it) and marked so nothing routes it for
-- a reply. Only what arrives after it is an inbox.
--
-- Backfill is `created_at`, not now(): for an existing binding the honest
-- watermark is the moment it was bound. Stamping now() would silently declare
-- every currently unanswered message to be history.
--
-- REFERENCE ONLY -- there is no migration runner on a node (docs/runbook-
-- update.md: "the database schema is baked, not migrated"). The statements that
-- actually reach a node are in bootstrap/B02-sn-core-schema.sql (fresh) and
-- bootstrap/B02-sn-core-seed.sql (installed, idempotent, applied by re-running
-- B02). This file is the written record of the change; keep the three in step.

ALTER TABLE sn_config.agent_channels
    ADD COLUMN IF NOT EXISTS history_before timestamp with time zone;

UPDATE sn_config.agent_channels
   SET history_before = created_at
 WHERE history_before IS NULL;

ALTER TABLE sn_config.agent_channels
    ALTER COLUMN history_before SET DEFAULT now();
ALTER TABLE sn_config.agent_channels
    ALTER COLUMN history_before SET NOT NULL;

COMMENT ON COLUMN sn_config.agent_channels.history_before IS
    'Binding watermark: messages that arrived at or before this are history -- recorded, never routed to an agent.';

-- The work-queue gains 'history' as a terminal state of its own. It is not
-- 'processed' (no agent dealt with it) and not 'blocked' (no policy refused
-- it); a queue that calls all three the same cannot answer "why did nobody
-- reply to this".
--
-- Drop-and-re-add rather than a one-off addition, because this set has already
-- drifted once: sn_core gained 'blocked' with the contact gate and public did
-- not, so a node installed before that change fails the constraint every time
-- dispatch writes 'blocked'. Re-asserting the whole set repairs that too.
DO $msgchk$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['public.message_state', 'sn_core.message_state'] LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format(
                'ALTER TABLE %s DROP CONSTRAINT IF EXISTS message_state_status_chk', t);
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT message_state_status_chk '
                'CHECK (status = ANY (ARRAY[''new'',''routed'',''processed'','
                '''failed'',''blocked'',''history'']))', t);
        END IF;
    END LOOP;
END
$msgchk$;
