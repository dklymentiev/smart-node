-- Reference migration. A node does not execute migrations/; the executable form is
-- the CREATE INDEX IF NOT EXISTS in bootstrap/B02-sn-core-schema.sql (and the pg_trgm
-- extension in bootstrap/B02-persistence.sh).
--
-- Added: idx_sessions_conversation_trgm, idx_sessions_user_key on sn_core.ai_sessions and
-- idx_messages_recipient on sn_core.messages. GET /api/desks and /api/desks/unread match
-- a desk's messages by conversation suffix, session user and message recipient; with no
-- index the unread call took 40 s on a node with thousands of sessions.
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA smartnode_ext;
CREATE INDEX IF NOT EXISTS idx_sessions_conversation_trgm
    ON sn_core.ai_sessions USING gin ((metadata ->> 'conversation') smartnode_ext.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sessions_user_key
    ON sn_core.ai_sessions USING btree ((metadata ->> 'user'));
CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON sn_core.messages USING btree (recipient);
