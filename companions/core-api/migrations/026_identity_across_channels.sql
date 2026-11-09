-- 026_identity_across_channels.sql -- one identity for a person across
-- every channel. Reference copy; the live schema is baked in
-- bootstrap/B02-sn-core-schema.sql (this directory is documentation, not a runner
-- -- see core-api.sh). The schema records one identity across all channels.
--
-- THREE THINGS were conflated: the HANDLE a channel hands over, the PROOF it is
-- genuine, and the PERSON it belongs to. Only the person existed, as a lookup;
-- the handle was stored on the message; the proof was thrown away at the door.
-- This makes the person one record, the handle map one table that also records
-- how each binding was proved, and the proof a row written AT ARRIVAL.

-- 1. contact_channels becomes the ONE handle map and records how each binding
--    was established. user_channels is retired into it in a later step, once its
--    readers are repointed. verified_by: session | dkim | platform-link |
--    owner-confirmed | declared | none.
ALTER TABLE sn_config.contact_channels ADD COLUMN IF NOT EXISTS verified_by  text;
ALTER TABLE sn_config.contact_channels ADD COLUMN IF NOT EXISTS verified_at  timestamp with time zone;
ALTER TABLE sn_config.contact_channels ADD COLUMN IF NOT EXISTS last_seen_at timestamp with time zone;

-- 2. an account is a person who can log in: users points at its contact.
ALTER TABLE sn_config.users ADD COLUMN IF NOT EXISTS contact_id integer;
DO $users_contact_fk$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_contact_id_fkey') THEN
        ALTER TABLE sn_config.users
            ADD CONSTRAINT users_contact_id_fkey
            FOREIGN KEY (contact_id) REFERENCES sn_config.contacts(id);
    END IF;
END
$users_contact_fk$;

-- 3. the verdict recorded at arrival, one row per message, append-only beside
--    sn_core.messages. trust levels, high to low: session > proved > linked >
--    declared > unknown; a level may never authorise something above it.
CREATE TABLE IF NOT EXISTS sn_core.message_identity (
    message_id  bigint NOT NULL,
    contact_id  integer,
    handle      text NOT NULL,
    trust       text NOT NULL,
    evidence    jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT message_identity_pkey PRIMARY KEY (message_id),
    CONSTRAINT message_identity_trust_chk CHECK ((trust = ANY (ARRAY['session'::text, 'proved'::text, 'linked'::text, 'declared'::text, 'unknown'::text]))),
    CONSTRAINT message_identity_message_id_fkey FOREIGN KEY (message_id) REFERENCES sn_core.messages(id) ON DELETE CASCADE,
    CONSTRAINT message_identity_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES sn_config.contacts(id)
);
CREATE INDEX IF NOT EXISTS message_identity_contact_idx ON sn_core.message_identity USING btree (contact_id, resolved_at DESC);

-- GRANTS. This file, like the baked schema, runs as the owning role, so no
-- OWNER/GRANT for the app role is needed here. The communication companion grants
-- its own service user SELECT, INSERT on message_identity (companions/
-- communication.sh) so the telegram and email adapters can stamp identity at
-- arrival. Nothing UPDATEs the row -- it is written once and never revised.
