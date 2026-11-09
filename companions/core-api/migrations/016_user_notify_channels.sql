-- 016_user_notify_channels.sql
-- Per-user notification channel registry. Maps a human user (sn_config.users)
-- to one or more delivery addresses (email mailbox, telegram chat id, ...).
-- The /api/notify endpoint resolves a planner assignee (= users.email) to that
-- user's ENABLED channels and writes one outbound sn_core.messages row each.
-- Agent assignees have no rows here, so notify is a no-op for them.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS sn_config.user_channels (
    id           serial PRIMARY KEY,
    tenant_id    integer,
    user_id      integer REFERENCES sn_config.users(id),
    channel_slug text,
    address      text,
    enabled      boolean DEFAULT true,
    created_at   timestamptz DEFAULT now(),
    UNIQUE (user_id, channel_slug)
);

-- Seed: the human owner (user id 1, email 'admin', super-admin) gets an
-- email channel seeded with a placeholder address; the owner sets their
-- real address in the panel (Notifications) after install.
INSERT INTO sn_config.user_channels (tenant_id, user_id, channel_slug, address, enabled)
SELECT u.tenant_id, u.id, 'email', 'owner@example.com', true
FROM sn_config.users u
WHERE u.id = 1
ON CONFLICT (user_id, channel_slug) DO NOTHING;
