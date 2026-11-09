-- 029_message_attachments.sql
-- Create sn_core.message_attachments: per-message file metadata for inbound
-- attachments received on any channel. Part of.
--
-- Bytes live on disk at /srv/data/attachments/{storage_path}; no agent reads
-- the filesystem directly. The retrieval API (core-api /api/attachments/)
-- enforces message-level access before serving bytes.
--
-- Access rule: can the caller read sn_core.messages where id = message_id?
-- There is no separate ACL for this table.

CREATE TABLE IF NOT EXISTS sn_core.message_attachments (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id       bigint      NOT NULL REFERENCES sn_core.messages(id) ON DELETE CASCADE,
    tenant_id        integer     NOT NULL DEFAULT 1,
    channel_type     text        NOT NULL,
    filename         text,
    content_type     text,
    size_bytes       bigint,
    content_hash     text,       -- SHA-256 hex of the stored bytes
    storage_path     text,       -- relative path within /srv/data/attachments/
    status           text        NOT NULL DEFAULT 'received'
                                 CHECK (status IN ('received', 'quarantined', 'processed', 'expired')),
    -- received_at mirrors the parent message
    received_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE sn_core.message_attachments IS
    'File metadata for inbound channel attachments. Bytes on disk; FK to messages for access control.';

-- Primary lookup: all attachments for a message.
CREATE INDEX IF NOT EXISTS message_attachments_message_id_idx
    ON sn_core.message_attachments (message_id);

-- Expiration sweep: find rows due for byte deletion.
CREATE INDEX IF NOT EXISTS message_attachments_status_received_at_idx
    ON sn_core.message_attachments (status, received_at)
    WHERE status = 'received';

-- Idempotent re-ingest: same bytes + same filename on the same message are
-- one attachment, not two. Protects against crash-then-restart.
CREATE UNIQUE INDEX IF NOT EXISTS message_attachments_dedup_idx
    ON sn_core.message_attachments (message_id, content_hash, filename)
    WHERE content_hash IS NOT NULL AND filename IS NOT NULL;
