-- 029_outbound_dry_run.sql -- mechanical outbound suppression per agent or
-- channel, independent of the agent body.
--
-- WHAT WENT WRONG. The only guard on a first live run was a line in the agent's
-- body asking it to honour DRY_RUN. An agent whose body ignored instructions
-- (that is the reason the body was being rewritten) could not be safely tested
-- because nothing in the infrastructure stopped it from sending. The send loop
-- needs a flag it controls, not one the agent's own body can override.
--
-- THE FIX. DRY_RUN_AGENTS (env var, comma-separated agent slugs) and
-- DRY_RUN_CHANNELS (env var, comma-separated channel types) are checked by
-- send_outbound.py before dialling any transport. When a match is found the
-- row is marked dry_run: the composed content is logged, the message row is
-- preserved, and nothing is transmitted. The email transport is covered by
-- the same path.
--
-- THIS MIGRATION ONLY ADDS 'dry_run' to the message_state status constraint.
-- send_outbound.py is changed in this same commit; no other schema changes are
-- needed.
--
-- REFERENCE ONLY -- there is no migration runner on a node (docs/runbook-
-- update.md: "the database schema is baked, not migrated"). The statements that
-- actually reach a node are in bootstrap/B02-sn-core-schema.sql (fresh) and
-- bootstrap/B02-sn-core-seed.sql (installed, idempotent, applied by re-running
-- B02). This file is the written record of the change; keep the three in step.

DO $drychk$
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
                '''failed'',''blocked'',''history'',''dry_run'']))', t);
        END IF;
    END LOOP;
END
$drychk$;
