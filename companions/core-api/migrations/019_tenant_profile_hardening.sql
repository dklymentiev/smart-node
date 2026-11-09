-- 019_tenant_profile_hardening.sql
-- Hardening constraints on the onboarding profile
-- store from migration 018, from the earlier change review. Tables are empty at this
-- point, so the added constraints validate trivially.

-- 1) seed-once / policy-versioned rows MUST carry a natural_key -- it is the
--    create-decision key. Without it the projector cannot match the live target
--    and would duplicate or fail.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenant_profile_natural_key_required') THEN
    ALTER TABLE sn_config.tenant_profile
      ADD CONSTRAINT tenant_profile_natural_key_required
      CHECK (behavior NOT IN ('seed-once','policy-versioned') OR natural_key IS NOT NULL);
  END IF;
END $$;

-- 2) Ledger uniqueness must be NULL-safe: a not-yet-known target_pk (NULL) must
--    not let the same (param_key,target_table) be inserted twice. NULLS NOT
--    DISTINCT (PG15+) treats NULLs as equal for the constraint.
ALTER TABLE sn_config.projection_ledger DROP CONSTRAINT IF EXISTS projection_ledger_uniq;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projection_ledger_uniq') THEN
    ALTER TABLE sn_config.projection_ledger
      ADD CONSTRAINT projection_ledger_uniq
      UNIQUE NULLS NOT DISTINCT (param_key, target_table, target_pk);
  END IF;
END $$;

-- 3) result is a known set (NULL = not yet resolved), mirroring the discipline
--    on tenant_profile.status.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projection_ledger_result_chk') THEN
    ALTER TABLE sn_config.projection_ledger
      ADD CONSTRAINT projection_ledger_result_chk
      CHECK (result IS NULL OR result IN ('projected','failed','skipped','retired'));
  END IF;
END $$;
