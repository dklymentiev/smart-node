[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

# Mesh Memory

![Mesh Demo](app/demo.gif)

**A memory that understands what you mean, not just what you type.**

You save a note: *"changed the login page to fix the auth problem"*. A month later you search for *"security issues on the website"* -- and Mesh finds your note. Even though you never used the words "security", "issues", or "website". Because Mesh understands that these ideas are related.

### Why this matters

- You forget the exact words you used last time. Mesh doesn't care -- it searches by meaning.
- Your notes, decisions, and research are scattered. Mesh keeps them in one place and finds connections.
- AI agents need memory between sessions. Mesh gives them one -- searchable, organized, persistent.

### Who is Mesh for

- **Solo developers** -- save decisions, worklogs, research. Find them months later with a vague question.
- **Teams** -- each team gets its own workspace. Search across multiple workspaces with weighted priority.
- **AI agents** -- Claude, GPT, or custom agents use Mesh as long-term memory via API or MCP tools.
- **Anyone who takes notes** -- if you've ever lost a note because you couldn't remember the right keyword, Mesh solves that.

### How it's different from regular search

| Regular Search | Mesh Memory |
|----------------|-------------|
| "auth bug" only finds "auth bug" | "auth bug" finds "login problem" |
| Exact keyword match | Understands meaning and context |
| One language at a time | Works across 100+ languages |
| Forgets context | Remembers everything you saved |

### Agent roles via workspaces

Create a workspace for any role -- and your AI agent instantly becomes a specialist with full history. Workspaces are free-form, create as many as you need:

- **sysadmin** -- server configs, incident fixes, monitoring notes
- **marketer** -- campaigns, ad performance, content plans
- **developer** -- architecture decisions, bug investigations, code reviews
- **psychologist** -- session notes, client progress, therapy approaches
- **support** -- customer issues, FAQ, product knowledge base
- **researcher** -- papers, experiments, literature reviews
- *...any role you can think of*

Switch workspace -- and the agent sees only what's relevant to that role. No noise from other domains. Pin a role prompt to the top of each workspace, and the agent knows exactly who it is and what it should do.

Need cross-domain context? Use weighted multi-workspace search: *"find nginx configs"* with 70% weight on sysadmin, 20% on security, 10% on developer -- and get results from all three, ranked by relevance and priority.

### Privacy first

Runs on your own server. Single Docker container. No cloud APIs, no subscriptions, no data leaving your machine. The AI model runs locally.

## Quick Start

```bash
# Clone and configure
git clone https://github.com/dklymentiev/mesh-memory.git
cd mesh-memory
cp .env.example .env
```

Open `.env` and set a password for PostgreSQL:

```
POSTGRES_PASSWORD=pick_any_password_here
```

That's the only required change. Everything else has sensible defaults.

```bash
# Start
docker compose up -d

# Check it works (takes 2-3 min on first launch)
curl http://localhost:8000/health
# {"status": "healthy", "embeddings": "ready"}
```

First launch downloads PostgreSQL, builds the API image, and bakes in the AI model (~560 MB). Subsequent starts are instant.

```bash
# Save your first document
curl -X PUT http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"content": "Decided to use PostgreSQL for the project", "tags": ["type:decision"]}'

# Search by meaning
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "which database did we choose?"}'
```

It finds your document -- even though the words are completely different.

For a step-by-step walkthrough including Web UI, tagging, and MCP integration, see the **[Getting Started Guide](docs/configuration.md)**.

## How It Works

Mesh has three layers:

```
Layer 1: DOCUMENTS        What you save -- notes, decisions, worklogs, research
Layer 2: EMBEDDINGS       AI model turns text into 768-dim vectors (automatic)
Layer 3: SEARCH           Find by meaning using vector similarity
```

**You save a document** -- plain text with optional tags:

```bash
curl -X PUT localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"content": "Chose Redis for caching because...", "tags": ["type:decision"]}'
```

**Mesh automatically:**
1. Stores the document in PostgreSQL
2. Generates an embedding vector in the background
3. Auto-adds `date:2026-02-23` and `source:api` tags
4. After indexing, infers tags from similar documents (e.g. `type:decision`, `topic:redis`)

**You search by meaning:**

```bash
curl -X POST localhost:8000/search \
  -d '{"query": "what caching solution did we pick?"}'
# -> finds the Redis decision, even with zero word overlap
```

No configuration. No prompt engineering. No API keys for external services. The AI model runs locally inside the container.

## What Can You Do With It

### Personal knowledge base

Save everything you learn, decide, or think about. Find it later by describing what you're looking for in plain language.

### Team memory

Multiple people save worklogs and decisions. Anyone can search across everything by meaning. "How did we handle rate limiting?" finds the relevant design doc even if it never mentions those words.

### AI agent memory

Give your AI agents persistent memory. They save context, decisions, and progress. Later they (or other agents) can search for relevant information.

```bash
# Agent saves its work
mesh add "Implemented retry logic with exponential backoff for API calls" \
  "type:worklog,date:2026-02-23"

# Another agent finds it
mesh search "error handling patterns" 5
```

### MCP integration

Mesh includes an MCP server (12 tools) that works with Claude Desktop, Claude Code, and Cursor. Your AI assistant can search, save, and organize documents directly.

## Demo Mode

The repo ships with 1000 pre-generated documents across 15 categories -- software development, AI/ML, infrastructure, design, DevOps, career, and more. Great for trying out the UI and search.

```bash
# Start the demo instance (auto-seeds 1000 documents)
docker compose up -d api-demo

# Open the galaxy map
open http://localhost:8001/ui/map.html

# Or the search UI
open http://localhost:8001/ui/
```

The demo auto-seeds on first startup when the database is empty. You can also seed manually:

```bash
python scripts/seed_demo.py --api http://localhost:8001
```

## Features

### Semantic search

Search by meaning, not keywords. Ask "login problems" and find "authentication bug". Works across 100+ languages thanks to the [multilingual-e5-base](https://huggingface.co/intfloat/multilingual-e5-base) model.

### Smart auto-tagging

Three levels of automatic tagging (no AI API calls needed):

| Level | When | Example |
|-------|------|---------|
| **Defaults** | On save | `date:2026-02-23`, `source:api` |
| **Neighbor inference** | After embedding | 7 nearest neighbors share `type:worklog` -> auto-added |
| **Manual** | On save | Your tags always take priority |

Configured in `mesh.yaml`. Custom tags beyond the schema are always accepted.

### Version chains

Track how a document evolved over time. Mesh uses embedding similarity to find related versions automatically -- no manual linking needed.

```bash
curl "localhost:8000/versions/doc_a1b2c3d4?threshold=0.85"
```

### Pinned documents

Pin important documents to always appear first in workspace listings. Perfect for role prompts, onboarding docs, or project briefs.

```bash
curl -X PATCH localhost:8000/doc_abc123 \
  -H "Content-Type: application/json" \
  -d '{"pinned": true}'
```

Pinned documents sort before all others in `GET /` listings.

### Built-in Web UI

Served at `/ui/`:

- **Search page** (`/ui/`) -- semantic search with card grid and markdown rendering
- **Galaxy map** (`/ui/map.html`) -- Three.js visualization with clustering by category, timeline view, and real-time search highlighting

No build step. Native ES modules, baked into the Docker image.

### MCP Server (12 tools)

For Claude Desktop, Claude Code, and Cursor:

| Tool | What it does |
|------|-------------|
| `mesh_search` | Semantic search (single or multi-workspace with weights) |
| `mesh_add` | Save document (auto-tags applied) |
| `mesh_update` | Update content, tags, or pinned status |
| `mesh_delete` | Delete by GUID |
| `mesh_get` | Get by GUID |
| `mesh_bytag` | Filter by tags |
| `mesh_focus` | Switch active workspace (with optional prefetch) |
| `mesh_versions` | Document version chain |
| `mesh_projects` | List projects with stats |
| `mesh_stats` | Memory statistics |
| `mesh_recent` | Recent documents |
| `mesh_tags` | List tags with counts |
| `mesh_schema` | Show tag schema |

Setup:

```json
{
  "mcpServers": {
    "mesh": {
      "command": "python3",
      "args": ["/path/to/mcp_server.py"],
      "env": {
        "MESH_API_URL": "https://your-mesh-instance.com"
      }
    }
  }
}
```

### Workspaces (multi-tenancy)

Isolate documents by workspace. Each workspace has its own documents, categories, and search index. Perfect for teams or multi-project setups.

```bash
# Save to a specific workspace
curl -X PUT localhost:8000/ \
  -H "Content-Type: application/json" \
  -H "X-Workspace: marketing" \
  -d '{"content": "Q1 campaign results...", "tags": ["type:note"]}'

# Search only within that workspace
curl -X POST localhost:8000/search \
  -H "X-Workspace: marketing" \
  -d '{"query": "campaign performance"}'
```

Without `X-Workspace`, everything goes to the `default` workspace. Fully backward-compatible.

**Multi-workspace search** -- search across workspaces with weighted relevance:

```bash
curl -X POST localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "nginx config", "limit": 10, "workspaces": {"sysadmin": 0.7, "security": 0.2, "architecture": 0.1}}'
```

Each workspace gets proportional slots (min 1). Scores are adjusted by weight: a 0.85 match in a workspace with weight 0.3 becomes 0.255. Results are merged, deduped, and sorted by final score.

**Scoped API keys** restrict access to specific workspaces:

```bash
# Create a key that only sees "marketing" and "sales" workspaces
curl -X POST localhost:8000/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"label": "marketing-team", "workspaces": ["marketing", "sales"]}'
```

**Admin endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/keys` | Create scoped API key |
| `GET` | `/admin/keys` | List API keys |
| `DELETE` | `/admin/keys/{hash}` | Revoke key |
| `GET` | `/admin/workspaces` | List workspaces with doc counts |
| `DELETE` | `/admin/workspaces/{name}` | Delete workspace and all docs |
| `GET` | `/admin/config` | Server configuration |

### AI Categorizer (opt-in)

Automatic document classification using an LLM. Disabled by default -- works without any external API keys.

**How documents get organized:**

| Step | What happens | Requires |
|------|-------------|----------|
| 1. Save document | Tags you provide are stored as-is | Nothing |
| 2. Auto-tags | `date:2026-02-23`, `source:api` added automatically | Nothing |
| 3. Neighbor inference | After embedding, tags are inferred from 7 nearest neighbors | 5+ existing documents |
| 4. AI categorization | LLM assigns `category:*` and `subcategory:*` tags | LLM API key (opt-in) |

Steps 1-3 work out of the box. Step 4 requires an OpenAI-compatible API.

**Enable the AI categorizer:**

```env
# .env
CATEGORIZER_ENABLED=true
LLM_API_URL=https://openrouter.ai/api/v1   # or any OpenAI-compatible endpoint
LLM_API_KEY=sk-...
LLM_MODEL=google/gemini-2.0-flash-001      # or gpt-4o-mini, claude-haiku, etc.
```

**Generate a taxonomy from your documents:**

```bash
# LLM analyzes 50 diverse documents and designs 10-15 categories
curl -X POST localhost:8000/categorizer/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"mode": "designed"}'

# View the generated taxonomy
curl localhost:8000/categorizer/taxonomy
```

The bootstrap samples your documents, asks the LLM to propose meaningful categories (like "Infrastructure", "Product Decisions", "Research Notes"), then classifies every document. New documents are classified automatically as they arrive.

**Manage categories manually:**

Categories are workspace-scoped. Each workspace gets its own taxonomy.

```bash
# List categories for a workspace
curl localhost:8000/categorizer/categories -H "X-Workspace: marketing"

# Create a custom category
curl -X POST localhost:8000/categorizer/categories \
  -H "Content-Type: application/json" \
  -H "X-Workspace: marketing" \
  -d '{"id": "campaigns", "name": "Campaigns", "description": "Marketing campaigns"}'

# Rename a category
curl -X PUT localhost:8000/categorizer/categories/campaigns \
  -H "Content-Type: application/json" \
  -H "X-Workspace: marketing" \
  -d '{"name": "Ad Campaigns"}'

# Delete a category (also removes its tags from documents)
curl -X DELETE localhost:8000/categorizer/categories/campaigns \
  -H "X-Workspace: marketing"
```

**Classify existing documents in bulk:**

```bash
# Classify up to 500 untagged documents
curl -X POST localhost:8000/categorizer/batch \
  -H "Content-Type: application/json" \
  -d '{"limit": 500}'
```

### Bulk import

Import up to 100 documents per request:

```bash
# Import from JSONL
python scripts/seed_demo.py --api http://localhost:8000

# Or use the API directly
curl -X PUT localhost:8000/bulk \
  -H "Content-Type: application/json" \
  -d '[{"content": "doc 1", "tags": ["type:note"]}, {"content": "doc 2", "tags": ["type:note"]}]'
```

## API Reference

All endpoints accept `X-Workspace` header to select workspace (default: `default`).

**Documents:**

| Method | Path | Description |
|--------|------|-------------|
| `PUT` | `/` | Create document |
| `GET` | `/` | Help (no params) or list documents (with params) |
| `POST` | `/search` | Semantic search |
| `GET` | `/{guid}` | Get document |
| `PUT` | `/doc/{guid}` | Update document |
| `PATCH` | `/{guid}` | Update document (alias) |
| `DELETE` | `/{guid}` | Delete document |
| `PUT` | `/bulk` | Bulk create (up to 100) |
| `GET` | `/versions/{guid}` | Version chain |
| `GET` | `/short/{guid}` | Short document summary |
| `GET` | `/browse` | All documents (for client-side search) |
| `GET` | `/keyword` | Fast keyword search (no embedding) |
| `GET` | `/by-hash/{hash}` | Find document by content hash |
| `PATCH` | `/by-hash` | Update document by content hash |

**Search & Discovery:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tags` | All tags with counts |
| `GET` | `/stats` | Statistics |
| `GET` | `/activity` | Document activity feed |
| `GET` | `/schema` | Tag schema |
| `GET` | `/visualize` | Visualization data for galaxy map |

**Metadata:**

| Method | Path | Description |
|--------|------|-------------|
| `PUT` | `/meta/{guid}` | Set document metadata (JSONB) |
| `GET` | `/meta/{guid}` | Get document metadata |
| `DELETE` | `/meta/{guid}` | Delete document metadata |
| `GET` | `/meta?type=` | List metadata by document type |

**Embeddings:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/embed` | Generate embedding for text |
| `POST` | `/embed/batch` | Batch embed multiple texts |

**AI Categorizer** (requires `CATEGORIZER_ENABLED=true`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/categorizer/taxonomy` | Current taxonomy with stats |
| `POST` | `/categorizer/bootstrap` | Generate taxonomy from documents |
| `POST` | `/categorizer/batch` | Batch classify untagged documents |
| `POST` | `/categorizer/classify/{guid}` | Classify single document |
| `GET` | `/categorizer/categories` | List categories (workspace-scoped) |
| `POST` | `/categorizer/categories` | Create category (admin) |
| `PUT` | `/categorizer/categories/{id}` | Rename category (admin) |
| `DELETE` | `/categorizer/categories/{id}` | Delete category (admin) |

**AI & Utilities:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/summarize/{guid}` | AI-powered document summary |
| `GET` | `/health` | Health check |
| `GET` | `/help` | Full API documentation |

**Admin** (requires admin API key):

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/keys` | Create scoped API key |
| `GET` | `/admin/keys` | List API keys |
| `DELETE` | `/admin/keys/{hash}` | Revoke API key |
| `GET` | `/admin/workspaces` | List workspaces |
| `DELETE` | `/admin/workspaces/{name}` | Delete workspace |
| `GET` | `/admin/config` | Server configuration |

Full endpoint details: [docs/api-reference.md](docs/api-reference.md)

## Architecture

```
+--------------+  +--------------+  +--------------+
|  CLI / curl  |  |  MCP Server  |  |  Web UI /ui/ |
+------+-------+  +------+-------+  +------+-------+
       |                 |                 |
       +-------  X-Workspace header -------+
                         |
              +----------v----------+
              |  Mesh API (FastAPI) |
              |  localhost:8000     |
              |                     |
              |  +--- Auth + RLS --+|  API keys, workspace isolation
              |  +-- Tag Schema ---+|  mesh.yaml, auto-tagging
              |  +-- Categorizer --+|  per-workspace taxonomy
              +----------+----------+
                         |
              +----------v--------------+
              |  PostgreSQL + pgvector  |
              |  +- documents          |  workspace_id + RLS
              |  +- doc_embeddings     |  768-dim vectors
              |  +- doc_chunks         |  chunked embeddings
              |  +- category_centroids |  per-workspace categories
              |  +- document_metadata  |  JSONB
              |  +- api_keys           |  scoped access
              +-----------+------------+
```

## Deployment

### Docker Compose (recommended)

```bash
docker compose up -d
```

### Without Docker

```bash
# You need: Python 3.11+, PostgreSQL 16 with pgvector
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set DATABASE_URL
python -m mesh.main
```

### With Traefik

```bash
echo "TRAEFIK_HOST=mesh.example.com" >> .env
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```

### Environment Variables

```bash
# Core
DATABASE_URL=postgresql://postgres:password@postgres:5432/meshdb
DATABASE_URL_APP=postgresql://mesh_app:apppass@postgres:5432/meshdb  # RLS-enforced pool
MESH_APP_PASSWORD=apppass     # Password for mesh_app role (RLS)
EMBEDDING_MODEL=intfloat/multilingual-e5-base

# Security
AUTH_REQUIRED=false           # Require X-API-Key header
API_KEYS=key1,key2            # Valid admin API keys
IP_WHITELIST=                 # CIDR ranges (empty = allow all)
CORS_ORIGINS=                 # Allowed origins

# Rate limiting (requests/min, 0 = unlimited)
RATE_LIMIT_SEARCH=60
RATE_LIMIT_EMBED=30

# AI Categorizer (opt-in)
CATEGORIZER_ENABLED=false
LLM_API_URL=                  # OpenAI-compatible endpoint
LLM_API_KEY=
LLM_MODEL=
```

Full configuration: [docs/configuration.md](docs/configuration.md)

## Tag System

Mesh supports any tags. Recommended types:

| Tag | Description |
|-----|-------------|
| `type:worklog` | Completed work |
| `type:note` | Notes and ideas |
| `type:decision` | Architecture decisions |
| `type:task` | Action items |
| `type:research` | Analysis and findings |
| `type:rfc` | Proposals |
| `status:active` | In progress |
| `status:completed` | Done |
| `date:YYYY-MM-DD` | When created (auto-added) |
| `guid:project-id` | Project marker |

## How Mesh Compares

| | Mesh | Pinecone | Weaviate | ChromaDB |
|--|------|----------|----------|----------|
| Self-hosted | Yes | No (SaaS) | Yes | Yes |
| Database | PostgreSQL + pgvector | Proprietary | Custom | SQLite |
| Auto-tagging | Yes (neighbor inference) | No | No | No |
| MCP integration | Built-in | No | No | No |
| Web UI | Built-in | Dashboard | Console | No |
| Setup | `docker compose up` | Sign up | Helm chart | `pip install` |
| Best for | Knowledge management | Large-scale search | Knowledge graphs | Prototyping |

Mesh is designed for personal and team knowledge management -- not for billion-scale vector search. If you need a "memory" for your projects with smart search, tagging, and simple deployment, Mesh is for you.

## Project Structure

```
mesh/                     # Python package (FastAPI app)
  main.py                 # Application, auth, auto-tagging, version chains
  crud.py                 # Document CRUD + search
  database.py             # Schema migrations + RLS setup
  embeddings.py           # Embedding generation
  chunker.py              # Document chunking for full-content search
  config.py               # Environment configuration
  tag_schema.py           # Tag schema + auto-inference
  mesh.yaml               # Tag configuration
  categorizer/            # AI classification (opt-in, workspace-scoped)
    __init__.py            # Core: classify, bootstrap, batch_scan
    taxonomy.py            # TaxonomyStore (per-workspace)
    router.py              # API endpoints + CRUD
    models.py              # Pydantic models
    classifier_embedding.py
    classifier_llm.py
    config.py
ui/                       # Built-in web UI
  index.html              # Search page
  map.html                # Galaxy/timeline visualization
  settings.html           # Admin: workspaces, API keys, categorizer
  js/map/                 # Three.js modules (15 files)
scripts/                  # Dev utilities
  generate_demo.py        # Generate demo data
  seed_demo.py            # Load JSONL into Mesh via API
  reindex_chunks.py       # Reindex document chunks
  init-db.sh              # Database initialization
examples/                 # Usage examples + demo seed data
  demo-seed.jsonl         # 1000 pre-generated documents
mcp_server.py             # MCP server (12 tools)
tests/                    # Test suite
docs/                     # Documentation
```

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

For security issues, see [SECURITY.md](SECURITY.md).

## License

MIT

---

Created by [Dmytro Klymentiev](https://klymentiev.com)
