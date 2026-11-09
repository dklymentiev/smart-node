-- 020_ledger_behavior.sql
-- Needed by the onboarding projector: the reverse reconcile/teardown pass
-- must dispatch teardown by the REMOVED param's behavior, but by then its
-- tenant_profile row is gone. Record behavior in the ledger so teardown is
-- self-contained.
ALTER TABLE sn_config.projection_ledger
    ADD COLUMN IF NOT EXISTS behavior text;
