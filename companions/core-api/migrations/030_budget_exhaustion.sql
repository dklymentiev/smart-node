-- 030: budget exhaustion behavior.
-- Adds owner notification tracking and pre-warning configuration to sn_config.budgets,
-- and a budget suspension field to sn_core.agent_runtime so the runner can pause
-- wake attempts until a budget period resets without marking agents broken.

-- Budget notification and pre-warning tracking
ALTER TABLE sn_config.budgets
    ADD COLUMN IF NOT EXISTS notified_at  timestamptz,
    ADD COLUMN IF NOT EXISTS warn_at_pct  integer     NOT NULL DEFAULT 90,
    ADD COLUMN IF NOT EXISTS warned_at    timestamptz;

COMMENT ON COLUMN sn_config.budgets.notified_at IS
    'Set when the owner has been notified of exhaustion for this period; cleared on period reset.';
COMMENT ON COLUMN sn_config.budgets.warn_at_pct IS
    'Percentage of period limit at which a pre-warning is sent to the owner.';
COMMENT ON COLUMN sn_config.budgets.warned_at IS
    'Set when the pre-warning has been sent for this period; cleared on period reset.';

-- Runner suspension: avoids marking agents broken during a budget exhaustion period
ALTER TABLE sn_core.agent_runtime
    ADD COLUMN IF NOT EXISTS budget_suspended_until timestamptz;

COMMENT ON COLUMN sn_core.agent_runtime.budget_suspended_until IS
    'If set and > now(), the runner does not wake this agent.';
