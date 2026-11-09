#!/usr/bin/env python3
"""
Database CRUD operations for documents and embeddings
"""
import asyncpg
import hashlib
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple
from datetime import datetime, timezone
import logging

from .models import DocumentCreateRequest, DocumentResponse, MetadataRequest, MetadataResponse, AuthContext

logger = logging.getLogger(__name__)


def compute_content_hash(content: str) -> str:
    """Compute MD5 hash of content"""
    return hashlib.md5(content.encode('utf-8')).hexdigest()


async def _apply_rls(conn, auth: Optional[AuthContext]):
    """Set RLS session vars on a connection if auth context is provided.

    Admin override (app.is_admin=true) is only set when workspace is '*'
    (all workspaces). Otherwise, even admin keys are scoped by workspace list.
    """
    if auth is None:
        return
    workspaces_csv = ",".join(auth.workspaces) if auth.workspaces else auth.workspace
    # Only bypass RLS when explicitly requesting all workspaces
    rls_admin = auth.is_admin and workspaces_csv == "*"
    await conn.execute("SELECT set_config('app.workspaces', $1, false)", workspaces_csv)
    await conn.execute("SELECT set_config('app.is_admin', $1, false)", str(rls_admin).lower())


class DocumentCRUD:
    """Database operations for documents"""

    def __init__(self, pool: asyncpg.Pool, auth: Optional[AuthContext] = None):
        self.pool = pool
        self._auth = auth

    def with_auth(self, auth: AuthContext) -> "DocumentCRUD":
        """Return a new CRUD instance bound to the given auth context."""
        return DocumentCRUD(self.pool, auth)

    @asynccontextmanager
    async def _conn(self):
        """Acquire a connection with RLS context set."""
        async with self.pool.acquire() as conn:
            await _apply_rls(conn, self._auth)
            yield conn
    
    async def create_document(self, guid: str, request: DocumentCreateRequest,
                              workspace_id: str = "default") -> DocumentResponse:
        """Create a new document in the database"""
        now = datetime.now(timezone.utc)
        created_at = request.created_at or now
        updated_at = request.updated_at or now
        tags = request.tags or []
        content_hash = compute_content_hash(request.content)
        filename = request.filename
        source = request.source or "api"

        async with self._conn() as conn:
            try:
                # Dedup: check if document with same content already exists
                existing = await conn.fetchrow(
                    "SELECT guid FROM documents WHERE content_hash = $1 LIMIT 1",
                    content_hash
                )
                if existing:
                    logger.info(f"Dedup: content_hash {content_hash} already exists as {existing['guid']}, merging tags")
                    # Merge new tags into existing document
                    row = await conn.fetchrow(
                        "SELECT * FROM documents WHERE guid = $1", existing['guid']
                    )
                    existing_tags = list(row['tags']) if row['tags'] else []
                    merged_tags = list(existing_tags)
                    for tag in tags:
                        if tag not in merged_tags:
                            merged_tags.append(tag)
                    if merged_tags != existing_tags:
                        await conn.execute(
                            "UPDATE documents SET tags = $1, updated_at = $2 WHERE guid = $3",
                            merged_tags, now, existing['guid']
                        )
                    return DocumentResponse(
                        guid=row['guid'],
                        content=row['content'],
                        content_hash=row['content_hash'],
                        filename=row.get('filename'),
                        source=row.get('source', 'api'),
                        tags=merged_tags,
                        created_at=row['created_at'],
                        updated_at=now,
                        directory="inbox",
                        workspace=row.get('workspace_id', 'default')
                    )

                # Insert document
                await conn.execute(
                    """
                    INSERT INTO documents (guid, content, content_hash, filename, source, tags,
                                           created_at, updated_at, workspace_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    guid, request.content, content_hash, filename, source, tags,
                    created_at, updated_at, workspace_id
                )

                return DocumentResponse(
                    guid=guid,
                    content=request.content,
                    content_hash=content_hash,
                    filename=filename,
                    source=source,
                    tags=tags,
                    created_at=created_at,
                    updated_at=updated_at,
                    directory="inbox",
                    workspace=workspace_id
                )

            except asyncpg.UniqueViolationError:
                logger.error(f"Document with GUID {guid} already exists")
                raise ValueError(f"Document with GUID {guid} already exists")
            except Exception as e:
                logger.error(f"Failed to create document {guid}: {e}")
                raise

    async def upsert_document(self, guid: str, request: DocumentCreateRequest,
                              workspace_id: str = "default") -> DocumentResponse:
        """Create or update a document by GUID"""
        now = datetime.now(timezone.utc)
        updated_at = request.updated_at or now
        tags = request.tags or []
        content_hash = compute_content_hash(request.content)
        filename = request.filename
        source = request.source or "api"

        created_at = request.created_at or now

        async with self._conn() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO documents (guid, content, content_hash, filename, source, tags,
                                           created_at, updated_at, workspace_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (guid) DO UPDATE SET
                        content = EXCLUDED.content,
                        content_hash = EXCLUDED.content_hash,
                        filename = EXCLUDED.filename,
                        source = EXCLUDED.source,
                        tags = EXCLUDED.tags,
                        updated_at = EXCLUDED.updated_at,
                        created_at = COALESCE($7, documents.created_at)
                    RETURNING created_at
                    """,
                    guid, request.content, content_hash, filename, source, tags,
                    created_at, updated_at, workspace_id
                )
                logger.info(f"Upserted document {guid}")

                return DocumentResponse(
                    guid=guid,
                    content=request.content,
                    content_hash=content_hash,
                    filename=filename,
                    source=source,
                    tags=tags,
                    created_at=row['created_at'],
                    updated_at=updated_at,
                    directory="inbox",
                    workspace=workspace_id
                )

            except Exception as e:
                logger.error(f"Failed to upsert document {guid}: {e}")
                raise

    async def get_document_by_guid(self, guid: str) -> Optional[DocumentResponse]:
        """Get a document by its GUID (RLS filters automatically)"""
        async with self._conn() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT guid, content, content_hash, filename, source, tags,
                           created_at, updated_at, workspace_id
                    FROM documents
                    WHERE guid = $1
                    """,
                    guid
                )

                if not row:
                    return None

                return DocumentResponse(
                    guid=row['guid'],
                    content=row['content'],
                    content_hash=row['content_hash'],
                    filename=row['filename'],
                    source=row['source'],
                    tags=row['tags'] or [],
                    created_at=row['created_at'],
                    updated_at=row['updated_at'],
                    directory="inbox",
                    workspace=row.get('workspace_id', 'default')
                )

            except Exception as e:
                logger.error(f"Failed to get document {guid}: {e}")
                raise
    
    async def keyword_search(
        self,
        query: str,
        limit: int = 10,
        tags: Optional[List[str]] = None
    ) -> List[DocumentResponse]:
        """Search documents by keyword (case-insensitive ILIKE).

        Used as a fallback/complement to semantic search for exact term matching.
        """
        async with self._conn() as conn:
            try:
                # Escape ILIKE special characters to prevent wildcard injection
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"

                if tags:
                    rows = await conn.fetch(
                        """
                        SELECT guid, content, content_hash, filename, source, tags,
                               created_at, updated_at, workspace_id
                        FROM documents
                        WHERE content ILIKE $1
                          AND tags @> $3::text[]
                        ORDER BY updated_at DESC
                        LIMIT $2
                        """,
                        pattern, limit, tags
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT guid, content, content_hash, filename, source, tags,
                               created_at, updated_at, workspace_id
                        FROM documents
                        WHERE content ILIKE $1
                        ORDER BY updated_at DESC
                        LIMIT $2
                        """,
                        pattern, limit
                    )

                return [
                    DocumentResponse(
                        guid=row['guid'],
                        content=row['content'],
                        content_hash=row['content_hash'],
                        filename=row['filename'],
                        source=row['source'],
                        tags=row['tags'] or [],
                        created_at=row['created_at'],
                        updated_at=row['updated_at'],
                        directory="inbox",
                        workspace=row.get('workspace_id', 'default')
                    )
                    for row in rows
                ]

            except Exception as e:
                logger.error(f"Failed to keyword search: {e}")
                raise


class EmbeddingCRUD:
    """Database operations for embeddings"""

    def __init__(self, pool: asyncpg.Pool, auth: Optional[AuthContext] = None):
        self.pool = pool
        self._auth = auth

    def with_auth(self, auth: AuthContext) -> "EmbeddingCRUD":
        """Return a new CRUD instance bound to the given auth context."""
        return EmbeddingCRUD(self.pool, auth)

    @asynccontextmanager
    async def _conn(self):
        async with self.pool.acquire() as conn:
            await _apply_rls(conn, self._auth)
            yield conn
    
    async def store_embedding(self, guid: str, embedding: List[float]) -> None:
        """Store an embedding for a document"""
        async with self._conn() as conn:
            try:
                # Convert list to vector format for PostgreSQL
                embedding_str = '[' + ','.join(map(str, embedding)) + ']'
                
                await conn.execute(
                    """
                    INSERT INTO doc_embeddings (guid, embedding)
                    VALUES ($1, $2::vector)
                    ON CONFLICT (guid) DO UPDATE SET embedding = EXCLUDED.embedding
                    """,
                    guid, embedding_str
                )
                
            except Exception as e:
                logger.error(f"Failed to store embedding for {guid}: {e}")
                raise
    
    async def search_similar_documents(
        self,
        query_embedding: List[float],
        limit: int = 10,
        similarity_threshold: float = 0.0,
        tags: Optional[List[str]] = None,
        date_from: Optional['datetime'] = None,
        date_to: Optional['datetime'] = None
    ) -> List[Tuple[str, float]]:
        """Search for similar documents using cosine similarity.

        Args:
            tags: If provided, only return documents containing ALL of these tags
                  (PostgreSQL array @> operator).
            date_from: If provided, only return documents created on or after this date.
            date_to: If provided, only return documents created on or before this date.
        """
        async with self._conn() as conn:
            try:
                query_vector = '[' + ','.join(map(str, query_embedding)) + ']'
                needs_join = tags or date_from or date_to

                # Build dynamic WHERE clauses and params
                conditions = ["1 - (e.embedding <=> $1::vector) >= $2"]
                params = [query_vector, similarity_threshold, limit]
                idx = 4

                if needs_join:
                    if tags:
                        conditions.append(f"d.tags @> ${idx}::text[]")
                        params.append(tags)
                        idx += 1
                    if date_from:
                        conditions.append(f"d.created_at >= ${idx}")
                        params.append(date_from)
                        idx += 1
                    if date_to:
                        conditions.append(f"d.created_at <= ${idx}")
                        params.append(date_to)
                        idx += 1

                    where_clause = " AND ".join(conditions)
                    query = f"""
                        SELECT e.guid,
                               1 - (e.embedding <=> $1::vector) AS similarity_score
                        FROM doc_embeddings e
                        JOIN documents d ON d.guid = e.guid
                        WHERE {where_clause}
                        ORDER BY similarity_score DESC
                        LIMIT $3
                    """
                else:
                    where_clause = " AND ".join(conditions)
                    query = f"""
                        SELECT e.guid,
                               1 - (e.embedding <=> $1::vector) AS similarity_score
                        FROM doc_embeddings e
                        WHERE {where_clause}
                        ORDER BY similarity_score DESC
                        LIMIT $3
                    """

                rows = await conn.fetch(query, *params)
                return [(row['guid'], float(row['similarity_score'])) for row in rows]

            except Exception as e:
                logger.error(f"Failed to search similar documents: {e}")
                raise

    async def search_similar_chunks(
        self,
        query_embedding: List[float],
        limit: int = 10,
        similarity_threshold: float = 0.0,
        tags: Optional[List[str]] = None,
        date_from: Optional['datetime'] = None,
        date_to: Optional['datetime'] = None
    ) -> List[Tuple[str, float]]:
        """Search for similar documents via chunk-level embeddings.

        Searches across all chunks, then deduplicates to document level
        by taking the maximum similarity score per document GUID.
        """
        async with self._conn() as conn:
            try:
                query_vector = '[' + ','.join(map(str, query_embedding)) + ']'
                chunk_limit = limit * 10
                needs_filters = tags or date_from or date_to

                if needs_filters:
                    # Build inner WHERE conditions
                    inner_conditions = []
                    params = [query_vector, similarity_threshold, chunk_limit]
                    idx = 4

                    if tags:
                        inner_conditions.append(f"d.tags @> ${idx}::text[]")
                        params.append(tags)
                        idx += 1
                    if date_from:
                        inner_conditions.append(f"d.created_at >= ${idx}")
                        params.append(date_from)
                        idx += 1
                    if date_to:
                        inner_conditions.append(f"d.created_at <= ${idx}")
                        params.append(date_to)
                        idx += 1

                    inner_where = " AND ".join(inner_conditions)

                    # Add workspace filter to avoid HNSW + RLS issue
                    ws = self._auth.workspace if self._auth else "default"
                    inner_where += f" AND c.workspace_id = ${idx}"
                    params.append(ws)
                    idx += 1

                    # Outer LIMIT must be appended AFTER workspace so that
                    # every ${N} in the built query maps to the right slot
                    # in *params. Previously `limit` was appended before the
                    # workspace filter, which shifted the binding and caused
                    # `expected str, got int` on the workspace_id slot
                    # whenever any filter (tags/date_from/date_to) was set.
                    outer_limit_ref = f"${idx}"
                    params.append(limit)
                    idx += 1

                    rows = await conn.fetch(
                        f"""
                        SELECT guid, MAX(sim) AS similarity_score
                        FROM (
                            SELECT c.guid,
                                   1 - (c.embedding <=> $1::vector) AS sim
                            FROM doc_chunks c
                            JOIN documents d ON d.guid = c.guid
                            WHERE {inner_where}
                            ORDER BY c.embedding <=> $1::vector
                            LIMIT $3
                        ) sub
                        WHERE sim >= $2
                        GROUP BY guid
                        ORDER BY similarity_score DESC
                        LIMIT {outer_limit_ref}
                        """,
                        *params
                    )
                else:
                    # Filter by workspace in inner query to avoid HNSW + RLS issue
                    ws = self._auth.workspace if self._auth else "default"
                    rows = await conn.fetch(
                        """
                        SELECT guid, MAX(sim) AS similarity_score
                        FROM (
                            SELECT c.guid,
                                   1 - (c.embedding <=> $1::vector) AS sim
                            FROM doc_chunks c
                            WHERE c.workspace_id = $5
                            ORDER BY c.embedding <=> $1::vector
                            LIMIT $3
                        ) sub
                        WHERE sim >= $2
                        GROUP BY guid
                        ORDER BY similarity_score DESC
                        LIMIT $4
                        """,
                        query_vector, similarity_threshold, chunk_limit, limit, ws
                    )

                return [(row['guid'], float(row['similarity_score'])) for row in rows]

            except Exception as e:
                logger.error(f"Failed to search similar chunks: {e}")
                raise

    async def has_chunks(self) -> bool:
        """Check if doc_chunks table has any data."""
        async with self._conn() as conn:
            try:
                count = await conn.fetchval("SELECT COUNT(*) FROM doc_chunks")
                return count > 0
            except Exception:
                return False


class MetadataCRUD:
    """Database operations for document metadata"""

    def __init__(self, pool: asyncpg.Pool, auth: Optional[AuthContext] = None):
        self.pool = pool
        self._auth = auth

    def with_auth(self, auth: AuthContext) -> "MetadataCRUD":
        return MetadataCRUD(self.pool, auth)

    @asynccontextmanager
    async def _conn(self):
        async with self.pool.acquire() as conn:
            await _apply_rls(conn, self._auth)
            yield conn

    async def get_metadata(self, guid: str) -> Optional[MetadataResponse]:
        """Get metadata for a document"""
        import json
        async with self._conn() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT guid, doc_type, metadata, extracted_at, extractor_version
                    FROM document_metadata
                    WHERE guid = $1
                    """,
                    guid
                )

                if not row:
                    return None

                # Parse JSONB if it's a string
                metadata = row['metadata']
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)

                return MetadataResponse(
                    guid=row['guid'],
                    doc_type=row['doc_type'],
                    metadata=metadata or {},
                    extracted_at=row['extracted_at'],
                    extractor_version=row['extractor_version']
                )

            except Exception as e:
                logger.error(f"Failed to get metadata for {guid}: {e}")
                raise

    async def upsert_metadata(self, guid: str, request: MetadataRequest) -> MetadataResponse:
        """Create or update metadata for a document"""
        now = datetime.now(timezone.utc)

        async with self._conn() as conn:
            try:
                # Check if document exists
                doc_exists = await conn.fetchval(
                    "SELECT 1 FROM documents WHERE guid = $1", guid
                )
                if not doc_exists:
                    raise ValueError(f"Document {guid} not found")

                # Upsert metadata
                import json
                metadata_json = json.dumps(request.metadata)

                await conn.execute(
                    """
                    INSERT INTO document_metadata (guid, doc_type, metadata, extracted_at, extractor_version)
                    VALUES ($1, $2, $3::jsonb, $4, $5)
                    ON CONFLICT (guid) DO UPDATE SET
                        doc_type = EXCLUDED.doc_type,
                        metadata = EXCLUDED.metadata,
                        extracted_at = EXCLUDED.extracted_at,
                        extractor_version = EXCLUDED.extractor_version
                    """,
                    guid, request.doc_type, metadata_json, now, request.extractor_version
                )

                return MetadataResponse(
                    guid=guid,
                    doc_type=request.doc_type,
                    metadata=request.metadata,
                    extracted_at=now,
                    extractor_version=request.extractor_version
                )

            except ValueError:
                raise
            except Exception as e:
                logger.error(f"Failed to upsert metadata for {guid}: {e}")
                raise

    async def delete_metadata(self, guid: str) -> bool:
        """Delete metadata for a document"""
        async with self._conn() as conn:
            try:
                result = await conn.execute(
                    "DELETE FROM document_metadata WHERE guid = $1",
                    guid
                )
                return result == "DELETE 1"

            except Exception as e:
                logger.error(f"Failed to delete metadata for {guid}: {e}")
                raise

    async def list_by_type(self, doc_type: str, limit: int = 100) -> List[MetadataResponse]:
        """List all metadata of a specific type"""
        import json
        async with self._conn() as conn:
            try:
                rows = await conn.fetch(
                    """
                    SELECT guid, doc_type, metadata, extracted_at, extractor_version
                    FROM document_metadata
                    WHERE doc_type = $1
                    ORDER BY extracted_at DESC
                    LIMIT $2
                    """,
                    doc_type, limit
                )

                results = []
                for row in rows:
                    metadata = row['metadata']
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata)
                    results.append(MetadataResponse(
                        guid=row['guid'],
                        doc_type=row['doc_type'],
                        metadata=metadata or {},
                        extracted_at=row['extracted_at'],
                        extractor_version=row['extractor_version']
                    ))
                return results

            except Exception as e:
                logger.error(f"Failed to list metadata by type {doc_type}: {e}")
                raise

