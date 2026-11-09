-- 015: agent runtime status. Per-agent live state for the centralized Runner
-- (smart-node-runner): free | busy | broken, with a lease (crash recovery) and last_error. The
-- Runner atomically claims an agent (free->busy) before launching, releases on completion, marks
-- broken on failure; GET /api/agents/state and /api/agents/with-pending-work read it. OWNER
-- smartnode so the app role can write. Idempotent: added out-of-band on early nodes (reference-node) with
-- raw SQL; CREATE TABLE IF NOT EXISTS reconciles those without error.
CREATE TABLE IF NOT EXISTS sn_core.agent_runtime (
  agent_slug  text PRIMARY KEY,
  status      text NOT NULL DEFAULT 'free',
  current_ref text,
  lease_until timestamptz,
  last_error  text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sn_core.agent_runtime OWNER TO smartnode;
