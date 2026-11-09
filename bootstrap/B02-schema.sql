-- B02-schema.sql -- Smart Node kernel schema: the actor registry, the
-- messages registry and its work queue. The event journal is NOT here --
-- it is sn_core.events; the events section below says why.
--
-- Applied by B02-persistence.sh into the `smartnode` core database.
--
-- The immutable event log + actor registry are CORE kernel schema, NOT a
-- Phase-2 companion: every action (stage runs, companion installs, agent
-- activity) must be attributable to an actor from the earliest bootstrap
-- step. If events were a companion, all of Phase 1 + the first companion
-- installs would run unjournaled -- history unrecoverable. (an earlier
-- precedent: the actors table was added retroactively and task_log had to
-- be cleaned of invalid actors.)
--
--  and implementation plan.
--
-- Idempotent: safe to re-apply -- B02-persistence.sh is idempotent and
-- applies this file on every run.

\set ON_ERROR_STOP on

-- Objects are owned by the core application role, not the postgres
-- superuser that runs the apply. The `smartnode` role owns the
-- `smartnode` database, so under PG 15 it may create in `public`.
SET ROLE smartnode;

-- ----------------------------------------------------------------------
-- actors -- reference table: every entity that can produce an event.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS actors (
    id            text        PRIMARY KEY,
    display_name  text        NOT NULL,
    type          text        NOT NULL,
    layer         text        NOT NULL,
    active        boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT actors_layer_chk
        CHECK (layer IN ('specialist','orchestrator','guard','human','system'))
);

COMMENT ON TABLE  actors             IS 'Smart Node actor registry -- every entity that can produce events.';
COMMENT ON COLUMN actors.id          IS 'Stable slug, convention "<layer>:<name>", e.g. system:installer.';
COMMENT ON COLUMN actors.type        IS 'Finer-grained role classification, free text.';
COMMENT ON COLUMN actors.layer       IS 'Architectural layer: specialist|orchestrator|guard|human|system.';

-- ----------------------------------------------------------------------
-- events -- NOT created here any more. There is one journal on the node and
-- it is sn_core.events, laid down by B02-sn-core-schema.sql in this same
-- stage and written by everything: core-api's events_emit.py, and now
-- bootstrap itself.
--
-- A second table in `public` gave the node two journals. `search_path` is
-- "$user", public, so lib.sh's unqualified INSERT landed in this one, which
-- no reader on the node ever opens -- sn_core.v_actor_events reads
-- sn_core.events, so the entire install and validation history sat outside
-- the identity model.
--
-- The reason the journal belongs in the kernel and not in a companion still
-- holds: events must be attributable from the earliest bootstrap step.
-- sn_core is created by this same B02 stage, before any companion runs, so
-- that requirement is met without a duplicate.
--
-- An existing node's rows are carried into sn_core.events and this table is
-- renamed aside by B02-sn-core-seed.sql. An installer does not drop a journal.
-- ----------------------------------------------------------------------

-- events_immutable() stays here: sn_core.events' own trigger executes this
-- function (B02-sn-core-schema.sql), and messages_immutable() below is its
-- sibling for the messages registry.
CREATE OR REPLACE FUNCTION events_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'events is append-only: % rejected', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

-- ----------------------------------------------------------------------
-- messages -- strictly immutable registry of all external-world
-- communication, every channel, inbound + outbound. Provenance store;
-- UPDATE and DELETE are both rejected. Queue /
-- lifecycle state lives in the separate message_state table. The messages
-- registry is the cross-component contract of the runtime model -- see
-- communication-runtime-model.md.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_type     text        NOT NULL,   -- email|telegram|web|voice|sms|...
    channel_id       text,                   -- per-channel instance (mailbox, bot)
    external_id      text        NOT NULL,   -- canonical source ID (Message-ID...)
    thread_id        text,                   -- thread handle, set at ingestion
    direction        text        NOT NULL,   -- inbound|outbound
    sender           text,
    recipient        text,
    content          text,
    content_type     text,                   -- text/plain|text/html|audio|...
    provenance_chain jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- parent/task/flow
    metadata         jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- channel-specific
    received_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messages_direction_chk CHECK (direction IN ('inbound','outbound'))
);

COMMENT ON TABLE  messages              IS 'Smart Node messages registry -- immutable provenance store for all channel comms.';
COMMENT ON COLUMN messages.channel_type IS 'Channel family: email|telegram|web|voice|sms|...';
COMMENT ON COLUMN messages.external_id  IS 'Canonical ID from the source (e.g. RFC822 Message-ID).';
COMMENT ON COLUMN messages.direction    IS 'inbound (received) | outbound (sent).';

CREATE UNIQUE INDEX IF NOT EXISTS messages_external_uq
    ON messages (channel_type, external_id);
CREATE INDEX IF NOT EXISTS messages_channel_idx  ON messages (channel_type, channel_id);
CREATE INDEX IF NOT EXISTS messages_thread_idx   ON messages (thread_id);
CREATE INDEX IF NOT EXISTS messages_received_idx ON messages (received_at);

-- messages is append-only even for its owner -- identical contract to events.
CREATE OR REPLACE FUNCTION messages_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'messages is append-only: % rejected', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

DROP TRIGGER IF EXISTS messages_no_update_delete ON messages;
CREATE TRIGGER messages_no_update_delete
    BEFORE UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_immutable();

-- ----------------------------------------------------------------------
-- message_state -- the mutable work queue, 1:1 with messages via FK.
-- status advances new -> routed -> processed (or failed). This is the
-- table brin (realtime) and secretary (scheduled) scan for unhandled work.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_state (
    message_id   bigint      PRIMARY KEY REFERENCES messages(id),
    status       text        NOT NULL DEFAULT 'new',
    processed_at timestamptz,
    error        text,                   -- set when status='failed'
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT message_state_status_chk
        CHECK (status IN ('new','routed','processed','failed'))
);

COMMENT ON TABLE  message_state        IS 'Mutable lifecycle/work-queue for messages -- new->routed->processed; scanned by brin and secretary.';
COMMENT ON COLUMN message_state.status IS 'Work-queue state: new|routed|processed|failed.';

CREATE INDEX IF NOT EXISTS message_state_status_idx ON message_state (status, updated_at);

-- Every inserted message gets a state row, status 'new'.
CREATE OR REPLACE FUNCTION messages_seed_state() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO message_state (message_id, status) VALUES (NEW.id, 'new');
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS messages_seed_state_trg ON messages;
CREATE TRIGGER messages_seed_state_trg
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_seed_state();

-- message_state rows advance but never disappear: DELETE rejected,
-- updated_at bumped on every UPDATE.
CREATE OR REPLACE FUNCTION message_state_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'message_state is append-only: DELETE rejected'
            USING ERRCODE = 'restrict_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS message_state_guard_trg ON message_state;
CREATE TRIGGER message_state_guard_trg
    BEFORE UPDATE OR DELETE ON message_state
    FOR EACH ROW EXECUTE FUNCTION message_state_guard();

-- ----------------------------------------------------------------------
-- Seed -- the installer itself, the first actor. Every event emitted by
-- the bootstrap stages is attributed to it.
-- ----------------------------------------------------------------------
INSERT INTO actors (id, display_name, type, layer)
VALUES ('system:installer', 'Smart Node Installer', 'installer', 'system')
ON CONFLICT (id) DO NOTHING;

RESET ROLE;
