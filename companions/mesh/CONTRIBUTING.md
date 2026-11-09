# Working with the Mesh source

Mesh is shipped here as open source, but the upstream is maintainer-published
and does not accept outside pull or merge requests. Forks are welcome. This
document explains how to set up a development environment and test changes in
your fork.

---

## Table of Contents

- [Development Environment Setup](#development-environment-setup)
- [Running Tests](#running-tests)
- [Code Style Guidelines](#code-style-guidelines)
- [Fork Workflow](#fork-workflow)
- [Documenting Issues](#documenting-issues)

---

## Development Environment Setup

### Prerequisites

- Python 3.11 or higher
- PostgreSQL 16 with the [pgvector](https://github.com/pgvector/pgvector) extension
- Docker and Docker Compose (recommended for local database)
- Git

### 1. Clone the Repository

```bash
git clone https://github.com/dklymentiev/mesh-memory.git
cd mesh-memory
```

### 2. Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux / macOS
# .venv\Scripts\activate   # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 4. Start the Database

The easiest way is with Docker Compose:

```bash
cp .env.example .env       # copy and edit as needed
docker compose up postgres -d
```

Or use an existing PostgreSQL 16+ instance with the [pgvector](https://github.com/pgvector/pgvector) extension installed:

```sql
CREATE DATABASE meshdb;
\c meshdb
CREATE EXTENSION IF NOT EXISTS vector;
```

### 5. Configure Environment Variables

Create a `.env` file in the project root:

```env
DATABASE_URL=postgresql://postgres:password@localhost:5433/meshdb
API_HOST=0.0.0.0
API_PORT=8000
ENVIRONMENT=development
LOG_LEVEL=DEBUG
DEBUG=true

# Optional
IP_WHITELIST=
CORS_ORIGINS=http://localhost:3000
SIMILARITY_THRESHOLD=0.1
EMBEDDING_MODEL=intfloat/multilingual-e5-base
```

### 6. Run the API Locally

```bash
uvicorn mesh.main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at `http://localhost:8000`.

### 7. Verify the Setup

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status": "healthy", "embeddings": "ready"}
```

---

## Running Tests

### Unit and Integration Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_api.py

# Run a specific test
pytest tests/test_api.py::TestHealthEndpoint
```

### Test Configuration

Tests use `pytest-asyncio` for async test support. The test database is separate from the development database. Set `DATABASE_URL` to a test database in your environment before running tests, or use the defaults in `tests/conftest.py`.

```bash
DATABASE_URL=postgresql://postgres:password@localhost:5433/meshdb_test pytest
```

### Coverage

`pytest-cov` is already included in `requirements-dev.txt`:

```bash
pytest --cov=mesh --cov-report=term-missing
```

---

## Code Style Guidelines

### General Rules

- Follow [PEP 8](https://peps.python.org/pep-0008/) for Python code.
- Maximum line length: **88 characters**.
- Use **type hints** for all function arguments and return values.
- Write **docstrings** for all public functions, classes, and modules.

### Formatter & Linter

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting:

```bash
ruff format .         # auto-format
ruff check .          # lint
ruff check . --fix    # auto-fix safe issues
```

### Type Checking

mypy is not included in `requirements-dev.txt`. Install it separately:

```bash
pip install mypy
mypy . --ignore-missing-imports
```

### Naming Conventions

| Element | Convention | Example |
|---------|------------|---------|
| Variables | `snake_case` | `document_guid` |
| Functions | `snake_case` | `get_document_by_guid()` |
| Classes | `PascalCase` | `DocumentCRUD` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_CONTENT_LENGTH` |
| Files | `snake_case.py` | `crud.py`, `embeddings.py` |

### Commit Messages

Use short, imperative-mood messages:

```
feat: add bulk document creation endpoint
fix: correct route ordering for catch-all GUID handler
docs: add CONTRIBUTING.md
refactor: extract IP whitelist logic into middleware
test: add integration tests for search endpoint
```

Prefix conventions: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`.

---

## Fork Workflow

1. **Fork** the repository and create a branch from `main`:

   ```bash
   git checkout -b feat/my-feature
   ```

2. **Make your changes**, following the code style guidelines above.

3. **Add or update tests** for any new or changed behavior.

4. **Run the full test suite** and make sure everything passes:

   ```bash
   ruff format .
   ruff check .
   mypy . --ignore-missing-imports
   pytest -v
   ```

5. **Update documentation** if your change affects the API, configuration, or usage. Update `README.md` and relevant docstrings.

6. Keep the change in your fork and record why it is needed, what it does, and
   which checks passed. Upstream does not accept outside pull or merge requests.

### Change Checklist

- [ ] Tests added or updated
- [ ] `ruff format --check .` passes without changes
- [ ] `ruff check .` passes without errors
- [ ] `mypy` passes without new errors
- [ ] `README.md` updated if user-facing behavior changed
- [ ] `CHANGELOG.md` updated under `[Unreleased]`

---

## Documenting Issues

Before recording a new issue in your fork, search its existing issues to avoid
duplicates.

### Bug Reports

Include the following information:

- **Mesh version** (or commit hash)
- **Python version**: `python --version`
- **OS / environment** (Docker, bare metal, etc.)
- **Steps to reproduce** - minimal, complete, and reproducible
- **Expected behavior**
- **Actual behavior** - including full error messages and stack traces

### Feature Requests

- Describe the problem you are trying to solve.
- Explain your proposed solution or approach.
- Note any alternatives you considered.

### Security Issues

Do not open a public issue for security vulnerabilities. Contact the maintainers directly.

---

## Project Structure Reference

```.
├── mesh/                    # Python package (application source)
│   ├── __init__.py          # Package init, version
│   ├── main.py              # FastAPI application, route definitions
│   ├── models.py            # Pydantic request/response models
│   ├── config.py            # Environment-based configuration
│   ├── database.py          # PostgreSQL connection pool and table setup
│   ├── crud.py              # Document, embedding, and metadata operations
│   ├── embeddings.py        # Sentence-transformer embedding service
│   ├── tag_schema.py        # Tag schema loader and auto-inference
│   ├── mesh.yaml            # Tag schema configuration
│   ├── utils.py             # GUID generation, tag normalization, validation
│   └── categorizer/         # AI document classification (opt-in)
│       ├── config.py        # LLM config
│       ├── models.py        # Category/subcategory models
│       ├── taxonomy.py      # Category taxonomy
│       ├── classifier_*.py  # Embedding and LLM classifiers
│       └── router.py        # FastAPI router
├── ui/                      # Built-in web UI (search + map)
│   ├── index.html           # Search page
│   ├── map.html             # Galaxy/timeline visualization
│   └── js/map/              # ES modules (Three.js)
├── scripts/                 # Development utilities
├── mcp_server.py            # MCP server (standalone)
├── tests/                   # Test suite
├── examples/                # Usage examples
├── docs/                    # User documentation
├── pyproject.toml           # Project metadata and tool config
├── requirements.txt         # Python dependencies
├── requirements-dev.txt     # Dev dependencies (pytest, ruff, httpx)
├── docker-compose.yml
├── Dockerfile
├── .env.example             # Environment template
├── .editorconfig            # Editor settings
├── LICENSE
├── SECURITY.md
├── CONTRIBUTING.md
└── MEMORY.md                # Project memory marker
```
