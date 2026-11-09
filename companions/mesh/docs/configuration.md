# Configuration

All configuration is done through environment variables in `.env`.

```bash
cp .env.example .env
# Edit .env with your values
```

## Required

| Variable | Description |
|----------|-------------|
| `POSTGRES_PASSWORD` | PostgreSQL password. Must be set, no default. |

That's it. Everything else has sensible defaults.

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DB` | `meshdb` | Database name |
| `POSTGRES_USER` | `postgres` | Database user |
| `POSTGRES_PASSWORD` | **required** | Database password |
| `DATABASE_URL` | auto-built | Full connection string (override if using external DB) |
| `DB_POOL_MIN_SIZE` | `1` | Minimum connection pool size |
| `DB_POOL_MAX_SIZE` | `10` | Maximum connection pool size |

## Embedding model

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDING_MODEL` | `intfloat/multilingual-e5-base` | HuggingFace model name |
| `SIMILARITY_THRESHOLD` | `0.1` | Minimum score for search results (0.0-1.0) |
| `EMBEDDING_CACHE_SIZE` | `1000` | In-memory cache for embeddings |
| `EMBEDDING_BATCH_SIZE` | `32` | Batch size for processing |

The default model supports 100+ languages and produces 768-dimensional vectors.

To use a different model, rebuild the image:

```bash
docker compose build --build-arg EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

The image bakes in the model at build time (~560 MB). At startup, Mesh verifies the baked model matches `EMBEDDING_MODEL` in `.env` and refuses to start if they differ.

## Security

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_REQUIRED` | `false` | Require API key for all requests (except /health) |
| `API_KEYS` | empty | Comma-separated valid API keys |
| `IP_WHITELIST` | empty | Comma-separated CIDR ranges (empty = allow all) |
| `TRUST_PROXY_HEADERS` | `false` | Trust CF-Connecting-IP / X-Real-IP for client IP |
| `CORS_ORIGINS` | empty | Comma-separated allowed origins (empty = deny all) |

### API key authentication

```bash
# .env
AUTH_REQUIRED=true
API_KEYS=key-abc123,key-def456
```

Clients must include: `X-API-Key: key-abc123`

### IP whitelist

```bash
# .env
IP_WHITELIST=10.0.0.0/8,192.168.1.0/24,203.0.113.50/32
```

Only listed IPs can access the API.

## Rate limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_SEARCH` | `60` | Max /search requests per minute per IP |
| `RATE_LIMIT_EMBED` | `30` | Max /embed requests per minute per IP |

Set to `0` to disable.

## Content limits

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONTENT_LENGTH` | `50000` | Maximum document size in characters |
| `MAX_SEARCH_RESULTS` | `100` | Maximum results per search query |

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST` | `0.0.0.0` | Listen address |
| `API_PORT` | `8000` | Listen port |
| `ENVIRONMENT` | `production` | Environment name |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `DEBUG` | `false` | Verbose error output |

## Traefik (reverse proxy)

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAEFIK_HOST` | `localhost` | Domain for Traefik routing |

Only used with `docker-compose.traefik.yml` overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```
