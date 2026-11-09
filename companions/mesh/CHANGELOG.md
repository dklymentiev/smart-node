# Changelog

All notable changes to Mesh are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed
- Cold embedding-model initialization now runs outside uvicorn's event-loop
  thread. The first document write after restart can return as soon as the
  durable database insert finishes while indexing continues in the background;
  concurrent queue workers still construct the shared model only once.

---

## [1.4.0] - 2026-03-17

### Added
- **Weighted multi-workspace search** (#641)
  - `POST /search` accepts `workspaces: {"sysadmin": 0.7, "security": 0.2}`
  - Limit distributed proportionally (min 1 slot per workspace)
  - Parallel search via asyncio.gather, scores adjusted by weight
  - MCP: `mesh_search(workspaces={"ws": weight})` parameter
  - Without `workspaces` param, behavior unchanged

---

## [1.3.2] - 2026-03-17

### Fixed
- **Critical: chunk vector search broken with workspace RLS** (#655)
  - HNSW index scanned all workspaces, RLS filtered post-scan → 0 results
  - Added `workspace_id` column to `doc_chunks` table
  - Chunk search now filters by workspace before HNSW scan
  - Backfill migration for existing chunks

### Removed
- Keyword fallback in search endpoint — was masking the broken vector search

---

## [1.3.1] - 2026-03-17

### Added
- **Pinned documents**: pin documents to top of workspace listings (#611)
  - `pinned` boolean column on documents table (default false)
  - `PATCH /{guid}` accepts `{"pinned": true/false}` to pin/unpin
  - `GET /` sorts pinned documents first (`ORDER BY pinned DESC, created_at DESC`)
  - `mesh_update` MCP tool accepts `pinned` parameter
  - Use case: role prompts for agent workspaces -- pinned doc always returned first

### Fixed
- Removed Cyrillic strings from `hooks/pre-search.sh` for i18n cleanliness

---

## [1.3.0] - 2026-02-25

### Added
- **Workspace multi-tenancy**: full document isolation per workspace via RLS
  - `X-Workspace` header selects active workspace on every request
  - Scoped API keys with per-workspace access (`POST /admin/keys`)
  - `api_keys` table: key hash, label, workspace list, admin flag
  - `GET /admin/workspaces` -- list workspaces with doc counts
  - `DELETE /admin/workspaces/{name}` -- delete workspace and all its docs
  - `GET /admin/config` -- server configuration overview
  - Default workspace `default` -- fully backward-compatible
- **AI Categorizer** (opt-in): automatic document classification
  - Embedding-based + LLM-based classifiers with configurable thresholds
  - Category taxonomy with subcategories, workspace-scoped (each workspace gets its own)
  - Bootstrap modes: `cluster` (HDBSCAN bottom-up) and `designed` (LLM top-down)
  - API endpoints: `/categorizer/taxonomy`, `/categorizer/bootstrap`, `/categorizer/batch`, `/categorizer/classify/{guid}`
  - **Category CRUD**: `GET/POST/PUT/DELETE /categorizer/categories` -- manage categories per workspace
  - 7 new environment variables (`CATEGORIZER_ENABLED`, `LLM_API_URL`, etc.)
  - Gated by `CATEGORIZER_ENABLED=false` (default off)
- **Document chunking**: full-content semantic search via `doc_chunks` table
  - Documents split into overlapping chunks, each with its own embedding
  - Improves search recall for long documents
- **Built-in Web UI** served at `/ui/`
  - Search page (`/ui/`): semantic search, card grid, document panel with markdown
  - Map page (`/ui/map.html`): Three.js galaxy/timeline visualization with particle clustering
  - Settings page (`/ui/settings.html`): system status, workspace management, API keys, categorizer config with CRUD
  - Modular ES modules architecture (`js/map/*.js`, 15 modules, no build step)
  - Text search (TX) and semantic search (AI) with live timer and client-side cache
- `/browse` endpoint: all documents with short previews for client-side search
- `/keyword` endpoint: fast ILIKE search without embedding
- `/summarize/{guid}` endpoint: on-demand LLM document summarization with DB caching
- `/short/{guid}` endpoint: document summary in short form
- `PUT /doc/{guid}` endpoint: update document content and tags
- Server-side query embedding cache (1h TTL, 500 entries) to avoid repeated 20s CPU inference
- **Demo seed data**: `examples/demo-seed.jsonl` with 1000 pre-generated documents across 15 categories
  - Auto-seeds on first startup when `ENVIRONMENT=demo` and database is empty
- `api-demo` service in docker-compose.yml for public demo instance
- `scripts/seed_demo.py`, `scripts/generate_demo.py`, `scripts/enrich_demo_tags.py` dev utilities
- `scripts/init-db.sh` for database initialization

### Changed
- `category_centroids` table: composite PK `(category_id, workspace_id)` -- categories are per-workspace
- docker-compose.yml: domain defaults changed to `example.com` placeholders (set real domains in `.env`)
- `.dockerignore`: added `docker-compose*.yml`, `mcp_server.py`, `scripts/` to reduce build context

### Fixed
- `.gitignore`: added `*.bak` pattern to prevent accidental backup commits
- Chunk search limit and reindex resilience improvements

---

## [1.2.1] - 2026-02-21

### Added
- CONTRIBUTING.md with development setup, test instructions, code style guidelines, and PR process
- `pyproject.toml` with project metadata, ruff/pytest/coverage config
- `.editorconfig` for consistent editor settings
- GitHub issue templates (bug report, feature request) and PR template
- `examples/` directory with `basic_usage.py` and `bulk_import.sh`

### Changed
- Reorganized project into `mesh/` Python package (moved from flat files in root)
- Gunicorn entry point: `mesh.main:app` (was `main:app`)
- CI uses `pgvector/pgvector:pg16` image (was `postgres:15`)
- Docker workflow uses GitHub Container Registry (ghcr.io) with native `GITHUB_TOKEN` auth
- `config.py` SIMILARITY_THRESHOLD default aligned to 0.1 (matches docker-compose)
- `.env.example` defaults aligned with docker-compose.yml
- README.md: badges, clone URL, comparison table, complete API endpoints table, examples section
- CONTRIBUTING.md: ruff replaces black, correct paths, updated project structure

### Fixed
- README clone URL and CI badges now point to GitHub (canonical host)
- `mesh/__init__.py` version corrected to 1.2.1 (was 1.0.0)
- `mcp` and `httpx` added to requirements.txt (were missing)
- Broken test imports: `import main` -> `from mesh import main` in test_api.py
- Internal domain removed from CHANGELOG example
- `secrets/` added to .dockerignore to prevent credential leaks in Docker builds

### Removed
- Dead code: `calculate_similarity` from embeddings.py, `check_connection` from database.py
- Dead code: unused config stubs and utility functions

---

## [1.2.0] - 2026-01-10

### Added
- **Metadata Layer**: Separate structured metadata storage per document
  - New `document_metadata` table with JSONB column for flexible schemas
  - `GET /meta/{guid}` - retrieve metadata for a document
  - `PUT /meta/{guid}` - create or replace metadata
  - `DELETE /meta/{guid}` - remove metadata
  - `GET /meta?type={doc_type}` - list metadata filtered by document type
- **Metadata layer**: Configurable origin restrictions for CORS via existing `CORS_ORIGINS` env var (frontend access for metadata endpoints)
- `MetadataRequest` Pydantic model: `doc_type`, `metadata` (dict), `extractor_version`
- `MetadataResponse` Pydantic model: `guid`, `doc_type`, `metadata`, `extracted_at`, `extractor_version`

### Use Cases Enabled
- Store Twitter/X post metadata: author, likes, retweets, source URL
- Store YouTube video metadata: channel, view count, duration, transcript
- Store webpage metadata: title, description, Open Graph image

---

## [1.1.0] - 2026-01-03

### Added
- Content hash (MD5) stored on every document for deduplication
- `GET /by-hash/{hash}` - find a document by its content hash
- `PATCH /by-hash` - update document content addressed by hash
- `PUT /bulk` - create multiple documents in a single request (up to 100)
- `GET /activity` - tag statistics with last-updated timestamps, filterable by tag prefix
- `GET /versions/{guid}` - document version chain (chronological evolution via embedding similarity)
- `GET /schema` - tag schema configuration endpoint
- `GET /visualize` - 2D document map (UMAP + HDBSCAN clustering)
- Tag schema system (`mesh.yaml`) with auto-inference from nearest neighbors
- MCP server (`mcp_server.py`) with 12 tools for Claude Desktop/Code integration
- API key authentication (`AUTH_REQUIRED`, `API_KEYS` env vars)
- Per-IP sliding window rate limiting (search, embed, heavy tiers)
- CORS support via `CORS_ORIGINS` env var
- `TRUST_PROXY_HEADERS` for Cloudflare/Traefik proxy deployments
- Model sentinel file to detect EMBEDDING_MODEL mismatch at startup

---

## [1.0.0] - 2025-12-29

### Added
- Initial release of Mesh document management and semantic search API
- Document CRUD with auto-generated GUIDs (`doc_xxxxxxxx` format)
- Semantic search powered by `intfloat/multilingual-e5-base` sentence-transformers model
- Background embedding worker: documents saved immediately, embeddings generated asynchronously
- Tag-based document filtering (`GET /?tag=type:worklog`)
- Smart root endpoint: returns API help without parameters, document list with parameters
- `PUT /` - create document
- `GET /` - help or document list
- `POST /search` - semantic similarity search with configurable threshold
- `GET /{guid}` - retrieve document by GUID
- `GET /health` - health check for database and embedding service
- `GET /stats` - document count, embedding queue depth, and service statistics
- `GET /help` - full inline API documentation
- IP whitelist middleware with CIDR range support, reads real IP from Cloudflare headers
- PostgreSQL 16 + pgvector backend with connection pooling via asyncpg
- Docker Compose deployment with Traefik reverse proxy integration
- Environment-based configuration via `.env` / `python-dotenv`
