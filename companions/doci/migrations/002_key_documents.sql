-- Key (canonical) documents -- the curated set that callers should
-- reach for first. A document can be "key" in several domains at once
-- (a brand-voice guide may matter for both marketing and support), so
-- the mapping is many-to-many with no UNIQUE on (guid, domain).
--
-- No FK to documents.guid by design: documents are soft-deleted (the
-- row stays), and a key_documents entry should follow the same soft
-- lifecycle (deletion is a separate operation).

CREATE TABLE IF NOT EXISTS key_documents (
    id              BIGSERIAL PRIMARY KEY,
    guid            UUID        NOT NULL,
    domain          TEXT        NOT NULL,
    label           TEXT,
    description     TEXT,
    update_trigger  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_key_documents_guid   ON key_documents (guid);
CREATE INDEX IF NOT EXISTS idx_key_documents_domain ON key_documents (domain);
