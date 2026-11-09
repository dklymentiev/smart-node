-- 027: a policy must say why it exists.
--
-- The column has been there since the catalogue was seeded, and all fourteen
-- seeded rules carry the dated incident that produced them. What was missing is
-- enforcement: POST /api/policies never wrote the column, so a rule authored the
-- supported way arrived with no history at all, silently, beside seeded rules
-- that had it. A catalogue of fifty rules is only reviewable if every rule can
-- be traced to what went wrong without it; a rule nobody can trace is a rule
-- nobody dares delete.
--
-- Safe as written: every existing row is already non-empty (14/14 on example-node
-- 2026-08-30). The backfill below exists so this does not fail on a node where
-- a rule was inserted directly before the API was fixed, and it marks such rows
-- rather than inventing a reason for them.

UPDATE sn_config.policies
   SET rationale = 'Recorded before rationale was required; the reason was not captured.'
 WHERE rationale IS NULL OR btrim(rationale) = '';

ALTER TABLE sn_config.policies
  ALTER COLUMN rationale SET NOT NULL;

-- NOT NULL alone accepts a single space, which satisfies the constraint and
-- defeats the point of it.
ALTER TABLE sn_config.policies
  ADD CONSTRAINT policies_rationale_not_blank
  CHECK (btrim(rationale) <> '');
