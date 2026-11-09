-- DOCI -- recursive hierarchy that walks BOTH thread/parent links AND
-- version/original links.
--
-- v1 walked only parent_guid. For nested threads that broke the chain
-- at every version-row (which has parent_guid=NULL, original_guid set).
-- A sub-thread on a thread on a doc only saw 2 ancestors instead of
-- the full 5-row chain, so AI lost the root document context entirely.
--
-- New behaviour:
--   chain step = COALESCE(parent_guid, original_guid)
--   thread row -> follow parent_guid (the version it was anchored to)
--   version row -> follow original_guid (the doc/thread it snapshotted)
--   document row -> NULL -> stop
-- Plus: return `quote` so AI prompt can show what each ancestor thread
-- was anchored to. Depth capped at 20 to defend against accidental loops.

DROP FUNCTION IF EXISTS get_document_hierarchy(UUID);

CREATE OR REPLACE FUNCTION get_document_hierarchy(doc_guid UUID)
RETURNS TABLE(
    guid          UUID,
    path          TEXT,
    doc_type      TEXT,
    title         TEXT,
    quote         TEXT,
    depth         INTEGER
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE hierarchy AS (
        SELECT
            d.guid, d.path, d.doc_type, d.title, d.quote,
            d.parent_guid, d.original_guid, 0 AS depth
        FROM documents d
        WHERE d.guid = doc_guid AND d.deleted_at IS NULL

        UNION ALL

        SELECT
            p.guid, p.path, p.doc_type, p.title, p.quote,
            p.parent_guid, p.original_guid, h.depth + 1
        FROM documents p
        JOIN hierarchy h ON p.guid = COALESCE(h.parent_guid, h.original_guid)
        WHERE p.deleted_at IS NULL
          AND h.depth < 20
    )
    SELECT
        hierarchy.guid,
        hierarchy.path,
        hierarchy.doc_type,
        hierarchy.title,
        hierarchy.quote,
        hierarchy.depth
    FROM hierarchy
    ORDER BY hierarchy.depth DESC;
END;
$$;
