-- Reference migration for the unattended wake cap.
--
-- Production schema is applied by bootstrap/B02-sn-core-schema.sql, which
-- carries the same column both in the CREATE for a fresh node and as an
-- additive ALTER for an existing one. companions/core-api/migrations is the
-- component-level history; core-api.sh deliberately does not run it.

-- The second wall an agent can hit. Deliberately NOT the budget column: the
-- reason and the reset differ, and an operator looking at a silent agent has to
-- be able to tell which of the two is holding it. Money says the work was too
-- expensive; this says the agent was starting work too often with nobody
-- asking. It lifts by itself once the counting window drains, so no human
-- action is needed to bring an agent back.
ALTER TABLE sn_core.agent_runtime
    ADD COLUMN IF NOT EXISTS wake_suspended_until timestamp with time zone;
