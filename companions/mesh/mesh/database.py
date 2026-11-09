#!/usr/bin/env python3
"""
Database connection and table management
"""
import asyncpg
import logging

logger = logging.getLogger(__name__)

async def create_tables(pool: asyncpg.Pool) -> None:
    """Create database tables if they don't exist.

    Runs under the superuser (migration) pool -- RLS does not apply.
    """
    async with pool.acquire() as conn:
        # Enable pgvector extension
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

        # Create documents table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                guid VARCHAR(32) PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash VARCHAR(32),
                filename VARCHAR(500),
                source VARCHAR(100) DEFAULT 'api',
                tags TEXT[] DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Add new columns if they don't exist (migration for existing deployments)
        await conn.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32)
        """)
        await conn.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS filename VARCHAR(500)
        """)
        await conn.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS source VARCHAR(100) DEFAULT 'api'
        """)
        await conn.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()
        """)
        await conn.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT NULL
        """)

        # Pinned documents feature
        await conn.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE
        """)

        # Create index on content_hash for fast lookup
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS documents_content_hash_idx
            ON documents(content_hash)
        """)
        
        # Create doc_embeddings table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_embeddings (
                guid VARCHAR(32) PRIMARY KEY REFERENCES documents(guid) ON DELETE CASCADE,
                embedding vector(768)
            )
        """)
        
        # Create HNSW index for vector similarity search.
        # HNSW provides better recall than IVFFlat with no tuning required.
        # Drop legacy IVFFlat index if it exists from older deployments.
        await conn.execute("""
            DROP INDEX IF EXISTS doc_embeddings_embedding_idx
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS doc_embeddings_hnsw_idx
            ON doc_embeddings USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)

        # Create document_metadata table for structured metadata
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS document_metadata (
                id SERIAL PRIMARY KEY,
                guid VARCHAR(32) REFERENCES documents(guid) ON DELETE CASCADE,
                doc_type VARCHAR(50),
                metadata JSONB DEFAULT '{}',
                extracted_at TIMESTAMPTZ DEFAULT NOW(),
                extractor_version VARCHAR(20),
                UNIQUE(guid)
            )
        """)

        # Create indexes for metadata queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS document_metadata_guid_idx
            ON document_metadata(guid)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS document_metadata_doc_type_idx
            ON document_metadata(doc_type)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS document_metadata_metadata_idx
            ON document_metadata USING GIN (metadata)
        """)

        # Create category_centroids table for AI categorizer
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS category_centroids (
                category_id VARCHAR(64) PRIMARY KEY,
                parent_id VARCHAR(64),
                name VARCHAR(200) NOT NULL,
                description TEXT,
                centroid vector(768),
                doc_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS category_centroids_hnsw_idx
            ON category_centroids USING hnsw (centroid vector_cosine_ops)
        """)

        # Add workspace_id to category_centroids (multi-tenant)
        await conn.execute("""
            ALTER TABLE category_centroids
                ADD COLUMN IF NOT EXISTS workspace_id VARCHAR(64) NOT NULL DEFAULT 'default'
        """)

        # Migrate PK from (category_id) to (category_id, workspace_id)
        await conn.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name
                    WHERE tc.table_name = 'category_centroids'
                      AND tc.constraint_type = 'PRIMARY KEY'
                      AND NOT EXISTS (
                          SELECT 1 FROM information_schema.key_column_usage kcu2
                          WHERE kcu2.constraint_name = tc.constraint_name
                            AND kcu2.column_name = 'workspace_id'
                      )
                ) THEN
                    ALTER TABLE category_centroids
                        DROP CONSTRAINT category_centroids_pkey;
                    ALTER TABLE category_centroids
                        ADD PRIMARY KEY (category_id, workspace_id);
                END IF;
            END $$
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS category_centroids_workspace_idx
            ON category_centroids(workspace_id)
        """)

        # Create doc_chunks table for full-content search via chunked embeddings
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_chunks (
                id SERIAL PRIMARY KEY,
                guid VARCHAR(32) NOT NULL REFERENCES documents(guid) ON DELETE CASCADE,
                chunk_index SMALLINT NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding vector(768),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(guid, chunk_index)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS doc_chunks_guid_idx ON doc_chunks(guid)
        """)

        # Add workspace_id to doc_chunks for workspace-scoped vector search
        await conn.execute("""
            ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS workspace_id VARCHAR(64)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS doc_chunks_workspace_idx ON doc_chunks(workspace_id)
        """)
        # Backfill done via: UPDATE doc_chunks c SET workspace_id = d.workspace_id FROM documents d WHERE c.guid = d.guid AND c.workspace_id IS NULL
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS doc_chunks_hnsw_idx
            ON doc_chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)

        # ---- Multi-tenant workspace support ----

        # Add workspace_id column to documents
        await conn.execute("""
            ALTER TABLE documents
                ADD COLUMN IF NOT EXISTS workspace_id VARCHAR(64) NOT NULL DEFAULT 'default'
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS documents_workspace_idx ON documents(workspace_id)
        """)

        # API keys table (scoped keys with workspace access)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key_hash    VARCHAR(64) PRIMARY KEY,
                label       VARCHAR(200),
                workspaces  TEXT[] NOT NULL DEFAULT '{}',
                is_admin    BOOLEAN NOT NULL DEFAULT FALSE,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                last_used   TIMESTAMPTZ
            )
        """)

        # ---- Row-Level Security ----
        await _setup_rls(conn)

        logger.info("Database tables created successfully")


async def _setup_rls(conn: asyncpg.Connection) -> None:
    """Enable RLS on data tables and create workspace isolation policies.

    Policies use session variables set via SET LOCAL before each transaction:
      app.workspaces  -- comma-separated list of allowed workspace IDs
      app.is_admin    -- 'true' bypasses workspace filter
    """
    # Enable RLS (idempotent)
    for table in ("documents", "doc_embeddings", "doc_chunks", "document_metadata"):
        await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    # Documents: direct workspace_id check
    await conn.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename = 'documents' AND policyname = 'workspace_isolation'
            ) THEN
                CREATE POLICY workspace_isolation ON documents
                    USING (
                        workspace_id = ANY(string_to_array(
                            current_setting('app.workspaces', true), ','))
                        OR current_setting('app.is_admin', true) = 'true'
                    );
            END IF;
        END $$
    """)

    # Related tables: filter via JOIN to documents (RLS on documents cascades)
    for table in ("doc_embeddings", "doc_chunks", "document_metadata"):
        await conn.execute(f"""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = '{table}' AND policyname = 'workspace_isolation'
                ) THEN
                    CREATE POLICY workspace_isolation ON {table}
                        USING (
                            guid IN (SELECT guid FROM documents)
                        );
                END IF;
            END $$
        """)


async def create_app_role(pool: asyncpg.Pool, password: str) -> None:
    """Create the mesh_app role used by the application (RLS enforced).

    Must be run under superuser pool.  Idempotent.
    """
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'mesh_app'"
        )
        if not exists:
            # Cannot use parameterised query for CREATE ROLE
            await conn.execute(
                f"CREATE ROLE mesh_app LOGIN PASSWORD '{password}'"
            )
            logger.info("Created database role: mesh_app")

        # Grant privileges (idempotent)
        await conn.execute(
            "GRANT ALL ON ALL TABLES IN SCHEMA public TO mesh_app"
        )
        await conn.execute(
            "GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mesh_app"
        )
        await conn.execute(
            "GRANT ALL ON ALL TABLES IN SCHEMA public TO mesh_app"
        )
        await conn.execute("""
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT ALL ON TABLES TO mesh_app
        """)
        await conn.execute("""
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT USAGE ON SEQUENCES TO mesh_app
        """)
        logger.info("Granted privileges to mesh_app")
