-- DOCI Initial Schema
-- Applied automatically on first run via docker-entrypoint-initdb.d

-- Documents table (stores all document types)
CREATE TABLE IF NOT EXISTS documents (
    guid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path TEXT,
    parent_guid UUID,
    original_guid UUID,
    doc_type TEXT DEFAULT 'document',
    title TEXT,
    summary TEXT,
    tags TEXT[],
    quote TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ,
    updated_by TEXT,
    access TEXT,
    deleted_at TIMESTAMPTZ
);

-- Indexes
CREATE UNIQUE INDEX IF NOT EXISTS documents_path_unique ON documents (path);
CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents (parent_guid);
CREATE INDEX IF NOT EXISTS idx_documents_original ON documents (original_guid);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents (path);
CREATE INDEX IF NOT EXISTS idx_documents_tags ON documents USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents (doc_type);

-- Broken links tracking
CREATE TABLE IF NOT EXISTS broken_links (
    id SERIAL PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_guid UUID,
    link_text TEXT,
    link_target TEXT NOT NULL,
    link_type TEXT DEFAULT 'internal',
    error_type TEXT,
    found_at TIMESTAMPTZ DEFAULT NOW(),
    fixed_at TIMESTAMPTZ,
    UNIQUE (source_path, link_target)
);

CREATE INDEX IF NOT EXISTS idx_broken_links_source ON broken_links (source_path);
CREATE INDEX IF NOT EXISTS idx_broken_links_found ON broken_links (found_at);

-- Recursive hierarchy function (walks parent chain)
CREATE OR REPLACE FUNCTION get_document_hierarchy(doc_guid UUID)
RETURNS TABLE(guid UUID, path TEXT, doc_type TEXT, title TEXT, depth INTEGER)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE hierarchy AS (
        SELECT
            d.guid,
            d.path,
            d.doc_type,
            d.title,
            d.parent_guid,
            0 AS depth
        FROM documents d
        WHERE d.guid = doc_guid AND d.deleted_at IS NULL

        UNION ALL

        SELECT
            p.guid,
            p.path,
            p.doc_type,
            p.title,
            p.parent_guid,
            h.depth + 1
        FROM documents p
        JOIN hierarchy h ON p.guid = h.parent_guid
        WHERE p.deleted_at IS NULL
    )
    SELECT
        hierarchy.guid,
        hierarchy.path,
        hierarchy.doc_type,
        hierarchy.title,
        hierarchy.depth
    FROM hierarchy
    ORDER BY hierarchy.depth DESC;
END;
$$;
