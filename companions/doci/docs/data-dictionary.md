# DOCI — Data Dictionary

**Database:** `doci`
**App user:** `doci_app`
**Migrations (applied idempotently on every container start):**
- `migrations/001_initial_schema.sql` — `documents` table + indexes
- `migrations/002_key_documents.sql` — `key_documents` table
- `migrations/003_hierarchy_full_chain.sql` — rewrite
  `get_document_hierarchy()` to walk thread + version links

## Tables

### `documents`

| Column | Type | Notes |
|---|---|---|
| `guid` | `UUID PRIMARY KEY DEFAULT gen_random_uuid()` | Stable across renames |
| `path` | `TEXT` | Relative to `files/`, ends in `.md` |
| `parent_guid` | `UUID` | Thread -> parent document |
| `original_guid` | `UUID` | Version -> original document |
| `doc_type` | `TEXT DEFAULT 'document'` | `document` \| `inbox` \| `thread` \| `version` |
| `title` | `TEXT` | |
| `summary` | `TEXT` | One-line summary |
| `tags` | `TEXT[]` | GIN-indexed |
| `quote` | `TEXT` | (Threads) selected passage on parent |
| `created_by` | `TEXT` | Identity at create time |
| `created_at` | `TIMESTAMPTZ DEFAULT NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NULL until first update |
| `updated_by` | `TEXT` | Identity at last update |
| `access` | `TEXT` | Reserved for per-doc ACL (not wired in v0.1) |
| `deleted_at` | `TIMESTAMPTZ` | Soft-delete tombstone |

### `key_documents`

Curated "first reach for these" registry: a many-to-many mapping from
`documents.guid` to a domain string (`marketing`, `support`,
`incidents`, …). A document can be canonical in several domains at
once.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | Surrogate; identifies one mapping row |
| `guid` | `UUID NOT NULL` | References `documents.guid` (no FK — see below) |
| `domain` | `TEXT NOT NULL` | Free-form domain label |
| `label` | `TEXT` | Optional display label override |
| `description` | `TEXT` | Optional one-line description |
| `update_trigger` | `TEXT` | Free-form "when should this be refreshed" hint |
| `created_at` | `TIMESTAMPTZ DEFAULT NOW()` | |

No FK to `documents(guid)` by design: `documents` is soft-deleted (the
row stays), so referential integrity at the SQL level would prevent the
tombstone from doing its job. Removing a `key_documents` row is a
separate operation.

## Indexes

```sql
CREATE UNIQUE INDEX documents_path_unique ON documents (path);
CREATE INDEX idx_documents_parent   ON documents (parent_guid);
CREATE INDEX idx_documents_original ON documents (original_guid);
CREATE INDEX idx_documents_path     ON documents (path);
CREATE INDEX idx_documents_tags     ON documents USING gin (tags);
CREATE INDEX idx_documents_type     ON documents (doc_type);

CREATE INDEX idx_key_documents_guid   ON key_documents (guid);
CREATE INDEX idx_key_documents_domain ON key_documents (domain);
```

## Functions

### `get_document_hierarchy(doc_guid UUID)`

Recursive CTE walking the ancestry chain from `doc_guid` upward.
Returns rows ordered root-first, each carrying `guid`, `path`,
`doc_type`, `title`, `quote`, `depth`. Used by the UI breadcrumb,
`documents.php::getHierarchy`, and the AI thread-reply prompt builder.

Walk rule (set by migration `003`):
`step = COALESCE(parent_guid, original_guid)` — a `thread` row follows
its `parent_guid` (the version it is anchored to); a `version` row
follows its `original_guid` (the source it snapshotted); a `document`
row terminates with NULL. Depth is capped at 20 to defend against
accidental cycles. Earlier versions of the function walked only
`parent_guid` and lost the root document context whenever a `version`
sat in the middle of the chain.

## Filesystem mapping

```
files/
|-- inbox/                                       # doc_type = 'inbox'
|   `-- 20260124-143022.md
|-- .data/
|   |-- versions/<original-guid>/<version-guid>/index.md
|   |                                            # doc_type = 'version'
|   `-- threads/<parent-guid>/<thread-guid>/index.md
|                                                # doc_type = 'thread'
`-- <any folders...>                             # doc_type = 'document'
```

The path on disk encodes `doc_type` for threads/versions/inbox; the
metadata column is redundant (and authoritative).

## Soft delete

`deleted_at IS NOT NULL` -> the row is gone from every default query.
The file on disk stays; a `Delete <path>` git commit is recorded so
the absence is auditable. To restore: `UPDATE documents SET deleted_at
= NULL WHERE guid = '...'`.

## Connection

```bash
# Dev (docker-compose.dev.yml exposes Postgres on 5433)
psql -h 127.0.0.1 -p 5433 -U doci_app -d doci

# List undeleted documents
SELECT guid, doc_type, path, title
FROM documents
WHERE deleted_at IS NULL
ORDER BY updated_at DESC NULLS LAST;
```

## Rebuilding the index

The `documents` table is derived data. To rebuild from `files/`:

```bash
docker compose exec doci php scripts/index-documents.php
```

This walks the tree, inserts a row for every `.md` file not already
indexed (matched by path), and leaves existing rows alone. Lost
metadata (tags, summaries) is not recoverable from files alone — they
were authored through the API, not embedded in the markdown.
