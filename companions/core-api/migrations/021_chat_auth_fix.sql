-- 021_chat_auth_fix.sql -- make the panel chat work on a node.
--
-- Two seeding defects that killed every chat while the cron agent kept working
-- (it runs `claude` directly and never touches these rows):
--
-- 1. cli_providers.claude-cli pointed at `secret:gateway/anthropic-key` -- a secret ref
--    nothing ever writes. A node's only LLM credential is an OAuth token
--    (CLAUDE_CODE_OAUTH_TOKEN; the CLI rejects it as ANTHROPIC_API_KEY), and the gateway
--    now RAISES when a referenced secret is absent. The token reaches the CLI through the
--    gateway's process env instead (EnvironmentFile=/etc/smart-node/agent-runtime.env),
--    so the provider needs no env of its own.
--
-- 2. agents.tenant_id was left NULL by the onboarding seed. The gateway copies the agent's
--    tenant onto the chat session row and ai_sessions.tenant_id is NOT NULL -> every
--    agent-mode chat died with a 500 (NotNullViolation).

UPDATE sn_config.cli_providers
   SET env_vars = '{}'::jsonb
 WHERE slug = 'claude-cli'
   AND env_vars ? 'ANTHROPIC_API_KEY';

UPDATE sn_config.agents
   SET tenant_id = 1
 WHERE tenant_id IS NULL;

-- 3. sn_config.models was never seeded at all. With an empty model list the panel falls
--    back to a hard-coded openrouter model, which needs a cloud key a fresh node does not
--    have -> the chat page 502s and sits in "reconnecting" forever. Default to the node's
--    own agent (claude-cli): persona + tools, no cloud key, no extra cost.
INSERT INTO sn_config.models (provider, model, label, modality, enabled, is_default, sort_order)
SELECT * FROM (VALUES
  ('claude-cli', 'sonnet', 'Claude Sonnet (agent)', 'chat', true, true,  10),
  ('claude-cli', 'haiku',  'Claude Haiku (agent)',  'chat', true, false, 20),
  ('claude-cli', 'opus',   'Claude Opus (agent)',   'chat', true, false, 30)
) AS v
WHERE NOT EXISTS (SELECT 1 FROM sn_config.models);
