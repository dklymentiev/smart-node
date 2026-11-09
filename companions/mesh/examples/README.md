# Examples

## demo-seed.jsonl

Pre-generated demo dataset with 1000 documents across 15 categories. Ships with the repo so anyone can seed a demo instance without running the generator.

**Format:** One JSON object per line (JSONL). Each object has:
- `content` -- document text (Markdown)
- `tags` -- list of tags (type, date, category, topic, project, status)
- `source` -- always `"demo"`
- `created_at` -- ISO 8601 timestamp

**Categories:** Software Development, Infrastructure, AI & ML, Product Management, Personal Notes, Books & Reading, News & Articles, Research, Learning, Finance & Business, Health & Productivity, Travel & Life, Design & UX, DevOps & SRE, Career & Management.

**Usage:**

```bash
# Seed via API (manual)
python scripts/seed_demo.py --api http://localhost:8001

# Or generate fresh data
python scripts/generate_demo.py --count 1000 --output examples/demo-seed.jsonl --seed 42
```

The `api-demo` Docker service auto-loads this file on first startup when `ENVIRONMENT=demo` and the database is empty.

---

## basic_usage.py

Python script demonstrating core Mesh operations:
- Health check
- Create documents with tags
- Semantic search (by meaning)
- Filtered search (by tags)
- Tag listing
- Statistics

```bash
pip install httpx
python examples/basic_usage.py
```

## bulk_import.sh

Bash script to bulk import markdown files:

```bash
# Import all markdown files from a directory
./examples/bulk_import.sh ~/notes/*.md

# Custom Mesh URL
MESH_URL=http://mesh.example.com ./examples/bulk_import.sh docs/*.md
```

## curl Examples

### Create a document

```bash
curl -X PUT http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"content": "Meeting: decided to use Rust for the CLI", "tags": ["type:decision"]}'
```

### Semantic search

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "what programming language for CLI?", "limit": 5}'
```

### Browse by tag

```bash
curl "http://localhost:8000/?tag=type:decision&limit=10"
```

### Statistics

```bash
curl http://localhost:8000/stats
```
