-- 013: allow per-agent OpenRouter model slugs (studio configurator -> chat).
-- The studio chat maps the agent's stored model to a gateway model. Logical
-- names (opus/sonnet/haiku) still map via studio MODEL_MAP; whitelisted
-- OpenRouter slugs pass through verbatim. Widen the CHECK so the DB can store
-- either form. Keep 'none' for "no model".
ALTER TABLE sn_config.agents DROP CONSTRAINT IF EXISTS agents_model_check;
ALTER TABLE sn_config.agents ADD CONSTRAINT agents_model_check
  CHECK (model IN (
    'opus','sonnet','haiku','none',
    'anthropic/claude-3.5-haiku',
    'anthropic/claude-haiku-4.5',
    'anthropic/claude-sonnet-4.5',
    'anthropic/claude-sonnet-4.6',
    'anthropic/claude-opus-4.5',
    'google/gemini-2.5-flash',
    'google/gemini-2.5-pro',
    'openai/gpt-4o-mini',
    'meta-llama/llama-3.1-8b-instruct'
  ));
