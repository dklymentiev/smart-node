-- Planner: API key authentication
-- Migration 0002
--
-- Adds the api_keys table used by the auth middleware to validate the
-- X-API-Key header on every API request. Keys are stored hashed (never in
-- plaintext); the plaintext is only ever shown to the operator at the
-- moment of creation.

BEGIN;

CREATE TABLE api_keys (
    id            SERIAL PRIMARY KEY,
    key_hash      TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,
    actor_id      TEXT NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    CONSTRAINT api_keys_label_not_blank CHECK (length(trim(label)) > 0)
);

CREATE INDEX api_keys_actor_id_idx ON api_keys(actor_id);
CREATE INDEX api_keys_revoked_at_idx ON api_keys(revoked_at) WHERE revoked_at IS NULL;

COMMENT ON TABLE api_keys IS
    'API keys for authenticating clients. key_hash is sha256 of the plaintext token; plaintext is shown once at creation. Keys are bound to an actor so audit attribution flows through naturally.';
COMMENT ON COLUMN api_keys.last_used_at IS
    'Updated at most every few minutes (best-effort) so revocation can be informed by recent activity.';
COMMENT ON COLUMN api_keys.revoked_at IS
    'Soft-revocation: row stays for audit purposes. Auth middleware rejects keys whose revoked_at is not NULL.';

COMMIT;
