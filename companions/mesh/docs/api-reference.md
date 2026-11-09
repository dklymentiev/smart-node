# API Reference

Base URL: `http://localhost:8000`

## Documents

### Create document

```
PUT /
```

Auto-generates a GUID.

```bash
curl -X PUT http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"content": "Meeting notes from Monday standup", "tags": ["type:note", "topic:meetings"]}'
```

Response:

```json
{
  "guid": "doc_a1b2c3d4e5f6g7h8",
  "content": "Meeting notes from Monday standup",
  "tags": ["type:note", "topic:meetings", "date:2026-02-21", "source:api"],
  "created_at": "2026-02-21T12:00:00Z",
  "updated_at": "2026-02-21T12:00:00Z"
}
```

### Create with specific GUID

```
PUT /doc/{guid}
```

Upserts: creates if new, updates if exists.

```bash
curl -X PUT http://localhost:8000/doc/my-custom-id \
  -H "Content-Type: application/json" \
  -d '{"content": "Project README content", "tags": ["type:artifact"]}'
```

### Get document

```
GET /{guid}
```

```bash
curl http://localhost:8000/doc_a1b2c3d4e5f6g7h8
```

### Update document

```
PATCH /{guid}
```

Update content, replace tags, or add/remove individual tags.

```bash
# Update content
curl -X PATCH http://localhost:8000/doc_a1b2c3d4e5f6g7h8 \
  -H "Content-Type: application/json" \
  -d '{"content": "Updated meeting notes"}'

# Add tags without removing existing ones
curl -X PATCH http://localhost:8000/doc_a1b2c3d4e5f6g7h8 \
  -H "Content-Type: application/json" \
  -d '{"add_tags": ["status:reviewed"]}'

# Remove specific tags
curl -X PATCH http://localhost:8000/doc_a1b2c3d4e5f6g7h8 \
  -H "Content-Type: application/json" \
  -d '{"remove_tags": ["status:draft"]}'
```

### Delete document

```
DELETE /{guid}
```

```bash
curl -X DELETE http://localhost:8000/doc_a1b2c3d4e5f6g7h8
```

### List documents

```
GET /
```

Query parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 10 | Max documents (1-100) |
| `offset` | int | 0 | Skip N documents |
| `tag` | string | - | Filter by tag (repeatable, AND logic) |
| `type` | string | - | Filter by type tag |
| `guid` | string | - | Filter by project GUID tag |
| `source` | string | - | Filter by source |

```bash
# List latest 5 worklogs
curl "http://localhost:8000/?type=worklog&limit=5"

# Filter by multiple tags
curl "http://localhost:8000/?tag=topic:docker&tag=type:decision"
```

### Bulk create

```
PUT /bulk
```

Create up to 100 documents in one request.

```bash
curl -X PUT http://localhost:8000/bulk \
  -H "Content-Type: application/json" \
  -d '[
    {"content": "First document", "tags": ["type:note"]},
    {"content": "Second document", "tags": ["type:note"]}
  ]'
```

---

## Search

### Semantic search

```
POST /search
```

Finds documents by meaning, not just keywords.

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "which database did we choose?", "limit": 5}'
```

Response:

```json
{
  "results": [
    {
      "guid": "doc_a1b2c3d4e5f6g7h8",
      "content": "Decided to use PostgreSQL for the project",
      "tags": ["type:decision"],
      "similarity_score": 0.858,
      "created_at": "2026-02-21T12:00:00Z"
    }
  ],
  "total_count": 1,
  "query": "which database did we choose?"
}
```

Optional: filter search by tags:

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "deployment", "tags": ["type:decision"], "limit": 10}'
```

### Find by content hash

```
GET /by-hash/{md5_hash}
```

Find a document by its MD5 content hash (deduplication).

---

## Tags

### List all tags

```
GET /tags
```

```bash
# All tags
curl http://localhost:8000/tags

# Only type: tags
curl "http://localhost:8000/tags?prefix=type:"
```

Response:

```json
{
  "tags": [
    {"tag": "type:worklog", "count": 42},
    {"tag": "type:note", "count": 18}
  ],
  "total": 60
}
```

### Tag schema

```
GET /schema
```

Returns the tag configuration: prefixes, auto-inference rules, defaults.

---

## Embeddings

### Generate embedding

```
POST /embed
```

```bash
curl -X POST http://localhost:8000/embed \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello world"}'
```

Returns a 768-dimensional vector.

### Batch embeddings

```
POST /embed/batch
```

Up to 100 texts per request.

```bash
curl -X POST http://localhost:8000/embed/batch \
  -H "Content-Type: application/json" \
  -d '{"texts": ["First text", "Second text", "Third text"]}'
```

---

## Metadata

Document metadata is stored separately from content. Useful for extracted entities, classifications, or structured data.

```
GET    /meta/{guid}     # Get metadata
PUT    /meta/{guid}     # Create/update metadata
DELETE /meta/{guid}     # Delete metadata
GET    /meta            # List metadata records (?type=report&limit=10)
```

---

## Version chains

```
GET /versions/{guid}
```

Find documents that are revisions of each other using embedding similarity.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `threshold` | 0.85 | Similarity threshold (0.5-0.99) |
| `limit` | 20 | Max versions |
| `same_project` | true | Only same project |

---

## Other endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check (database + embeddings) |
| `GET /stats` | Statistics (document count, queue size, tags) |
| `GET /help` | Full API documentation |
| `GET /activity` | Tag activity timeline |
| `GET /short` | Find documents with very short content |
| `GET /visualize` | 2D UMAP visualization of document clusters |

---

## Authentication

When `AUTH_REQUIRED=true` in `.env`, all requests (except `/health`) require:

```
X-API-Key: your-api-key-here
```

```bash
curl -H "X-API-Key: my-secret-key" http://localhost:8000/stats
```

## Rate limits

| Endpoint | Default limit |
|----------|--------------|
| `/search` | 60 req/min |
| `/embed`, `/embed/batch` | 30 req/min |

Exceeded: returns `429 Too Many Requests`.
