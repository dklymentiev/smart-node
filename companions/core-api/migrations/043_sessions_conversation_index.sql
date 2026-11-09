-- Reference migration. A node does not execute migrations/; the executable form is
-- the CREATE INDEX IF NOT EXISTS in bootstrap/B02-sn-core-schema.sql.
--
-- Added: sn_core.idx_sessions_conversation on sn_core.ai_sessions ((metadata ->> 'conversation')).
-- GET /api/desks looks the sessions of each desk up by that key; with no index a burst
-- of calls held all of core-api's connections.
CREATE INDEX IF NOT EXISTS idx_sessions_conversation
    ON sn_core.ai_sessions USING btree ((metadata ->> 'conversation'));
