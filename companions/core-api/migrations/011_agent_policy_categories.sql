-- Migration 011: agent.policy_categories
--
-- The runtime resolver needs category metadata to
-- know WHICH policy categories apply to each agent. Today agents have no
-- such field in the DB; this was previously implicit in agent.yaml.
--
-- Adds policy_categories TEXT[] -- a list of category slugs. The
-- resolver pulls enabled policies WHERE category = ANY(agent.policy_categories)
-- and assembles them into the agent's system_prompt.
--
-- Default: ['safety','workflow'] -- the two categories currently seeded
-- in sn_config.policies. New agents inherit it; existing 3 agents
-- (test-agent, ui-test, sysadmin) are backfilled to the same.

BEGIN;

ALTER TABLE sn_config.agents
  ADD COLUMN IF NOT EXISTS policy_categories TEXT[]
    NOT NULL DEFAULT ARRAY['safety','workflow'];

-- Backfill is automatic via DEFAULT; this UPDATE is a no-op for safety,
-- in case the column existed beforehand with NULLs.
UPDATE sn_config.agents
   SET policy_categories = ARRAY['safety','workflow']
 WHERE policy_categories IS NULL OR cardinality(policy_categories) = 0;

COMMIT;
