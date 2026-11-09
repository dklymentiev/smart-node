-- 014: integration capabilities (feature flags declared by an integration).
-- Adds sn_config.integrations.capabilities (text[]): the agent-facing features
-- an integration provides (known values: knowledge, memory, browser, orchestrator).
-- The capabilities-aware UPSERT in routes/integrations.py preserves existing values
-- when a caller sends an empty list (so old installers pre-capabilities will not
-- wipe an agent's features).
-- Idempotent: the column was added out-of-band on early nodes (e.g. reference-node) with no
-- prior migration; ADD COLUMN IF NOT EXISTS reconciles those nodes without error.
ALTER TABLE sn_config.integrations
  ADD COLUMN IF NOT EXISTS capabilities text[] NOT NULL DEFAULT '{}'::text[];
