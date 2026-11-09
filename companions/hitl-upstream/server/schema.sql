-- hitl storage, schema version 3 (PRAGMA user_version). Applied on every connection; all statements are idempotent.

CREATE TABLE IF NOT EXISTS callers (
    id             TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    display_domain TEXT NOT NULL,
    theme          TEXT NOT NULL DEFAULT '{}', -- operator-approved outer shell colors and optional logo
    pubkey         TEXT NOT NULL,          -- Ed25519, base64url
    verified_since TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    id           TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    enc_pubkey   TEXT,                     -- X25519, base64url; set by the first enrolment
    epoch        INTEGER NOT NULL DEFAULT 1,  -- +1 at every reset; a key statement chain lives in one epoch
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id           TEXT PRIMARY KEY,         -- WebAuthn credential id, base64url
    recipient_id TEXT NOT NULL REFERENCES recipients(id),
    spki         TEXT NOT NULL,            -- credential public key, SubjectPublicKeyInfo DER, base64url
    alg          INTEGER NOT NULL,         -- COSE algorithm
    prf_salt     TEXT NOT NULL,
    wrapped_key  TEXT NOT NULL,            -- encryption private key and the device's anchor, sealed with the PRF-derived key
    wrap_nonce   TEXT NOT NULL,
    label        TEXT NOT NULL,
    enrolled_via TEXT NOT NULL,            -- which identity check vouched for this enrolment
    created_at   TEXT NOT NULL,
    retired_at   TEXT
);

CREATE TABLE IF NOT EXISTS key_statements (
    recipient_id         TEXT NOT NULL REFERENCES recipients(id),
    epoch                INTEGER NOT NULL,
    seq                  INTEGER NOT NULL,
    statement            TEXT NOT NULL,          -- JCS of the statement: complete key set after the change
    signer_credential_id TEXT NOT NULL,
    assertion            TEXT NOT NULL,          -- WebAuthn assertion over SHA-256 of the statement
    created_at           TEXT NOT NULL,
    PRIMARY KEY (recipient_id, epoch, seq)
);

CREATE TABLE IF NOT EXISTS enrol_tokens (
    token_hash   TEXT PRIMARY KEY,         -- SHA-256 of the token, hex: a copy of the database yields no usable link
    recipient_id TEXT NOT NULL REFERENCES recipients(id),
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    via          TEXT NOT NULL DEFAULT 'operator-link'  -- which identity check issued it
);

CREATE TABLE IF NOT EXISTS requests (
    id                 TEXT PRIMARY KEY,   -- minted by the caller, inside the signed creation body
    short              TEXT NOT NULL UNIQUE,
    caller_id          TEXT NOT NULL REFERENCES callers(id),
    recipient_id       TEXT NOT NULL REFERENCES recipients(id),
    creation_body      TEXT NOT NULL,      -- JCS of the signed creation body, exactly as the caller signed it
    caller_signature   TEXT NOT NULL,
    config_ct          TEXT NOT NULL,
    payload_ct         TEXT NOT NULL,
    created_at         TEXT NOT NULL,      -- the caller's clock, from the creation body
    expires_at         TEXT NOT NULL,      -- created_at + ttl
    opened_at          TEXT,
    response_record    TEXT,               -- JCS of the signed response record
    response_assertion TEXT,               -- JSON: the device assertion, or the session proof
    signed_at          TEXT
);

CREATE TABLE IF NOT EXISTS blobs (
    id         TEXT PRIMARY KEY,
    caller_id  TEXT NOT NULL REFERENCES callers(id),
    sha256     TEXT NOT NULL,                  -- of the ciphertext, hex
    size       INTEGER NOT NULL,
    request_id TEXT REFERENCES requests(id),  -- set when a request claims it; only then downloadable
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    type       TEXT NOT NULL,
    request_id TEXT,
    caller_id  TEXT,
    data       TEXT NOT NULL
);
