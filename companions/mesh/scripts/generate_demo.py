#!/usr/bin/env python3
"""
Generate realistic demo data for Mesh semantic memory.
~1000 documents across 15 categories, loaded via PUT /bulk endpoint or saved as JSONL.

Usage:
    python scripts/generate_demo.py --api http://localhost:8001
    python scripts/generate_demo.py --count 1000 --output examples/demo-seed.jsonl --seed 42
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Categories and their topic pools
# ---------------------------------------------------------------------------

CATEGORIES = {
    "Software Development": {
        "count": 100,
        "types": ["worklog", "note", "decision", "task", "rfc"],
        "topics": [
            ("API Design: REST vs GraphQL for Mobile Backend",
             "Evaluated both approaches for our mobile app backend.\n\n"
             "## REST Pros\n- Simpler caching with HTTP standards\n- Better tooling ecosystem\n- Team already experienced\n\n"
             "## GraphQL Pros\n- Single endpoint, flexible queries\n- Reduces over-fetching on mobile\n- Auto-generated types\n\n"
             "## Decision\nGoing with REST + OpenAPI spec. Team velocity matters more than query flexibility at this stage.\n"
             "Will revisit if we hit >20 endpoints."),

            ("Code Review: Authentication Middleware Refactor",
             "Reviewed PR #247 for auth middleware changes.\n\n"
             "## Changes\n- Extracted JWT validation into separate module\n- Added refresh token rotation\n- Moved session storage from memory to Redis\n\n"
             "## Feedback\n1. Good separation of concerns\n2. Need to add rate limiting on token refresh endpoint\n3. Consider adding `jti` claim for token revocation\n4. Missing tests for expired token edge case\n\n"
             "```python\n# Suggested improvement\nasync def validate_token(token: str) -> Claims:\n    try:\n        claims = jwt.decode(token, SECRET, algorithms=['HS256'])\n        if await is_revoked(claims['jti']):\n            raise TokenRevoked()\n        return claims\n    except jwt.ExpiredSignatureError:\n        raise TokenExpired()\n```"),

            ("Debugging: Memory Leak in WebSocket Handler",
             "Spent 3 hours tracking down a memory leak in production.\n\n"
             "## Symptoms\n- RSS growing ~50MB/hour\n- Eventually OOM killed after 18-24 hours\n- Only on servers with >100 concurrent WS connections\n\n"
             "## Root Cause\n"
             "Event listeners were not being cleaned up on disconnect. Each connection registered a callback on the global event bus "
             "but the `on_disconnect` handler had a race condition -- if the client disconnected during a message send, "
             "the cleanup was skipped.\n\n"
             "## Fix\n```javascript\nclass ConnectionManager {\n  disconnect(clientId) {\n    // Always cleanup, regardless of state\n    this.eventBus.removeAllListeners(clientId);\n    this.connections.delete(clientId);\n    this.metrics.decrement('active_connections');\n  }\n}\n```\n\n"
             "Memory now stable at ~200MB under same load."),

            ("Refactoring: Database Query Optimization",
             "Optimized the slowest queries in the dashboard module.\n\n"
             "## Before\n- `/api/dashboard/stats` - 2.3s avg response time\n- N+1 query pattern loading user permissions\n- Full table scan on `events` table\n\n"
             "## After\n- Added composite index on `(user_id, created_at)`\n- Replaced N+1 with JOIN + window function\n- Added materialized view for daily aggregates\n\n"
             "```sql\nCREATE MATERIALIZED VIEW daily_stats AS\nSELECT\n  date_trunc('day', created_at) as day,\n  count(*) as event_count,\n  count(DISTINCT user_id) as unique_users\nFROM events\nGROUP BY 1;\n```\n\n"
             "Response time dropped to 120ms. Will add REFRESH CONCURRENTLY to cron."),

            ("Setting Up CI/CD Pipeline with GitHub Actions",
             "Configured the full CI/CD pipeline for the new microservice.\n\n"
             "## Pipeline Stages\n1. **Lint** - ESLint + Prettier check\n2. **Test** - Unit + Integration tests with PostgreSQL service\n3. **Build** - Docker image with layer caching\n4. **Deploy** - Staging auto-deploy, production manual approval\n\n"
             "## Key Decisions\n- Using `docker/build-push-action@v5` for multi-platform builds\n- Caching node_modules with `actions/cache@v4`\n- Secrets stored in GitHub Environment, not repo-level\n\n"
             "```yaml\njobs:\n  test:\n    runs-on: ubuntu-latest\n    services:\n      postgres:\n        image: postgres:16\n        env:\n          POSTGRES_DB: test\n          POSTGRES_PASSWORD: test\n        ports:\n          - 5432:5432\n    steps:\n      - uses: actions/checkout@v4\n      - run: npm ci\n      - run: npm test\n```\n\n"
             "Pipeline runs in ~4 minutes total."),

            ("TypeScript Migration Strategy",
             "Planning the incremental migration from JavaScript to TypeScript.\n\n"
             "## Approach\n- Strict mode from day one (`strict: true` in tsconfig)\n- Migrate bottom-up: utilities -> services -> controllers -> routes\n- Keep `.js` and `.ts` files side by side during transition\n\n"
             "## Priority Order\n1. `src/utils/` (no dependencies, easy wins)\n2. `src/models/` (define proper interfaces)\n3. `src/services/` (most business logic)\n4. `src/controllers/` (depends on services)\n5. `src/routes/` (thin wrappers)\n\n"
             "## Timeline\n- Week 1-2: Utils + Models (30 files)\n- Week 3-4: Services (25 files)\n- Week 5: Controllers + Routes (15 files)\n- Week 6: Remove all `@ts-ignore` and `any` types"),

            ("Implementing Rate Limiting with Redis",
             "Added rate limiting to protect API endpoints from abuse.\n\n"
             "## Algorithm\nUsing sliding window counter (Redis sorted sets). More accurate than fixed window, less memory than sliding log.\n\n"
             "```python\nasync def check_rate_limit(key: str, limit: int, window: int) -> bool:\n    now = time.time()\n    pipe = redis.pipeline()\n    pipe.zremrangebyscore(key, 0, now - window)\n    pipe.zadd(key, {str(now): now})\n    pipe.zcard(key)\n    pipe.expire(key, window)\n    results = await pipe.execute()\n    return results[2] <= limit\n```\n\n"
             "## Configuration\n| Endpoint | Limit | Window |\n|----------|-------|--------|\n| POST /api/auth | 5 | 60s |\n| GET /api/search | 60 | 60s |\n| PUT /api/documents | 30 | 60s |\n\n"
             "Returning `429 Too Many Requests` with `Retry-After` header."),

            ("Git Workflow: Trunk-Based Development",
             "Switched from Gitflow to trunk-based development.\n\n"
             "## Why\n- Long-lived branches caused painful merges\n- Release branches added ceremony without value\n- Team is small (4 devs), don't need the overhead\n\n"
             "## New Rules\n1. All work on short-lived feature branches (max 2 days)\n2. PRs must be < 400 lines changed\n3. Feature flags for incomplete features\n4. `main` is always deployable\n5. Release = tag on main\n\n"
             "## Feature Flags\nUsing environment variables for now. If we need more sophistication, will evaluate LaunchDarkly or Unleash.\n\n"
             "```python\nFEATURES = {\n    'new_dashboard': os.getenv('FF_NEW_DASHBOARD', 'false') == 'true',\n    'ai_suggestions': os.getenv('FF_AI_SUGGEST', 'false') == 'true',\n}\n```"),

            ("Error Handling Patterns in Async Python",
             "Documenting our error handling conventions for the team.\n\n"
             "## Rules\n1. Never catch bare `Exception` -- always specific types\n2. Use custom exception hierarchy\n3. Log at the boundary, not in library code\n4. Return structured errors from API\n\n"
             "## Exception Hierarchy\n```python\nclass AppError(Exception):\n    status_code = 500\n    \nclass NotFoundError(AppError):\n    status_code = 404\n\nclass ValidationError(AppError):\n    status_code = 422\n\nclass AuthError(AppError):\n    status_code = 401\n```\n\n"
             "## API Error Response\n```json\n{\n  \"error\": {\n    \"code\": \"VALIDATION_ERROR\",\n    \"message\": \"Email is required\",\n    \"details\": {\"field\": \"email\"}\n  }\n}\n```"),

            ("Implementing WebSocket Real-time Notifications",
             "Added real-time notifications via WebSocket for the dashboard.\n\n"
             "## Architecture\n- FastAPI with `websockets` library\n- Redis pub/sub for cross-instance broadcasting\n- Heartbeat every 30s to detect stale connections\n\n"
             "## Message Format\n```json\n{\n  \"type\": \"notification\",\n  \"payload\": {\n    \"id\": \"notif-123\",\n    \"title\": \"New comment on PR #42\",\n    \"action_url\": \"/pr/42\"\n  },\n  \"timestamp\": \"2026-01-15T10:30:00Z\"\n}\n```\n\n"
             "## Connection Flow\n1. Client connects with JWT in query param\n2. Server validates token, adds to connection pool\n3. Subscribe to user-specific Redis channel\n4. Forward messages to client\n5. On disconnect, cleanup and unsubscribe\n\n"
             "Current capacity: ~500 concurrent connections per instance."),

            ("Database Schema Migration Best Practices",
             "Notes from setting up our migration workflow.\n\n"
             "## Tools\n- Alembic for Python projects\n- Flyway for Java services\n- All migrations are forward-only (no down migrations in prod)\n\n"
             "## Rules\n1. Never alter column types without a multi-step migration\n2. Add new columns as nullable, backfill, then add constraint\n3. Index creation must use `CONCURRENTLY`\n4. Every migration needs a rollback plan documented\n5. Test migrations on a prod-size dataset before deploying\n\n"
             "## Example: Adding NOT NULL Column\n```sql\n-- Step 1: Add nullable\nALTER TABLE users ADD COLUMN email_verified boolean;\n\n-- Step 2: Backfill\nUPDATE users SET email_verified = false WHERE email_verified IS NULL;\n\n-- Step 3: Add constraint\nALTER TABLE users ALTER COLUMN email_verified SET NOT NULL;\nALTER TABLE users ALTER COLUMN email_verified SET DEFAULT false;\n```"),

            ("Microservices Communication Patterns",
             "Evaluating sync vs async communication for our new services.\n\n"
             "## Synchronous (HTTP/gRPC)\n- Direct request/response\n- Simple to reason about\n- Creates coupling and cascading failures\n- Use for: queries, real-time responses\n\n"
             "## Asynchronous (Message Queue)\n- Decoupled producers and consumers\n- Natural retry and dead-letter handling\n- Harder to debug, eventual consistency\n- Use for: events, background processing\n\n"
             "## Our Stack\n- **Sync:** gRPC for internal service-to-service\n- **Async:** RabbitMQ for events and background jobs\n- **Hybrid:** API Gateway handles HTTP, translates to gRPC internally\n\n"
             "Key insight: Start synchronous, move to async only when you need the decoupling."),
        ],
    },

    "Infrastructure": {
        "count": 80,
        "types": ["worklog", "note", "decision", "task"],
        "topics": [
            ("Docker Compose Setup for Local Development",
             "Unified the local dev environment for the whole team.\n\n"
             "## Services\n- PostgreSQL 16 with pgvector\n- Redis 7 with persistence\n- MinIO for S3-compatible storage\n- Mailhog for email testing\n- Nginx reverse proxy\n\n"
             "## Key Features\n- `.env.example` with all required vars\n- Health checks on all services\n- Named volumes for data persistence\n- Hot-reload on application code\n\n"
             "```yaml\nservices:\n  app:\n    build: .\n    volumes:\n      - ./src:/app/src\n    environment:\n      - DATABASE_URL=postgresql://dev:dev@postgres/devdb\n    depends_on:\n      postgres:\n        condition: service_healthy\n```\n\n"
             "Onboarding time went from 2 hours to 10 minutes."),

            ("Monitoring Stack: Prometheus + Grafana",
             "Set up monitoring for all production services.\n\n"
             "## Components\n- **Prometheus** - metrics collection (15s scrape interval)\n- **Grafana** - dashboards and alerting\n- **Node Exporter** - system metrics\n- **cAdvisor** - container metrics\n\n"
             "## Key Dashboards\n1. System Overview - CPU, memory, disk, network\n2. Application Metrics - request rate, latency, error rate\n3. Database - connections, query time, replication lag\n4. Container Health - restart count, OOM events\n\n"
             "## Alert Rules\n- CPU > 80% for 5 min\n- Memory > 90% for 3 min\n- Disk > 85%\n- Error rate > 5% for 2 min\n- Response time p99 > 2s\n\n"
             "Alerts go to Telegram channel via webhook."),

            ("Backup Strategy: 3-2-1 Implementation",
             "Implemented proper backup strategy for all production data.\n\n"
             "## 3-2-1 Rule\n- **3** copies of data\n- **2** different storage types\n- **1** offsite copy\n\n"
             "## Implementation\n| Data | Method | Schedule | Retention |\n|------|--------|----------|----------|\n| PostgreSQL | pg_dump + WAL archiving | Hourly + continuous | 30 days |\n| Files | rsync to NAS | Daily | 90 days |\n| Configs | Git repo | On change | Forever |\n| Offsite | Restic to B2 | Daily | 1 year |\n\n"
             "## Verification\n- Weekly restore test to staging\n- Monthly full DR drill\n- Alert if backup age > 2x schedule\n\n"
             "```bash\n#!/bin/bash\n# Daily backup script\npg_dump -Fc meshdb > /backups/mesh-$(date +%Y%m%d).dump\nrestic backup /backups/ /data/ --tag daily\nrestic forget --keep-daily 30 --keep-monthly 12 --prune\n```"),

            ("Nginx Reverse Proxy Configuration",
             "Configured Nginx as reverse proxy for all services.\n\n"
             "## Features\n- SSL termination with Let's Encrypt\n- HTTP/2 support\n- Gzip compression\n- Rate limiting per IP\n- WebSocket upgrade support\n\n"
             "```nginx\nupstream api {\n    server 127.0.0.1:8000;\n    keepalive 32;\n}\n\nserver {\n    listen 443 ssl http2;\n    server_name api.example.com;\n\n    ssl_certificate /etc/letsencrypt/live/api.example.com/fullchain.pem;\n    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;\n\n    location / {\n        proxy_pass http://api;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n    }\n\n    location /ws {\n        proxy_pass http://api;\n        proxy_http_version 1.1;\n        proxy_set_header Upgrade $http_upgrade;\n        proxy_set_header Connection \"upgrade\";\n    }\n}\n```"),

            ("Kubernetes Migration Planning",
             "Evaluating Kubernetes vs staying on Docker Compose.\n\n"
             "## Current Setup\n- 3 VPS servers (Hetzner)\n- Docker Compose on each\n- Manual deployment via SSH\n- No auto-scaling\n\n"
             "## K8s Pros\n- Auto-scaling based on load\n- Self-healing (pod restart)\n- Rolling deployments\n- Service mesh (Istio/Linkerd)\n\n"
             "## K8s Cons\n- Complexity overhead for small team\n- Higher base cost (control plane)\n- Learning curve is steep\n- Over-engineered for current scale\n\n"
             "## Decision\nStay on Docker Compose with Docker Swarm for orchestration. We have <10 services and <1000 RPM. "
             "K8s makes sense at 50+ services or when we need multi-region. Revisit in Q3."),

            ("SSL/TLS Certificate Automation",
             "Automated certificate management across all domains.\n\n"
             "## Setup\n- Traefik as reverse proxy with built-in ACME\n- Let's Encrypt for public domains\n- Self-signed CA for internal services\n\n"
             "## Traefik Configuration\n```yaml\ncertificatesResolvers:\n  letsEncrypt:\n    acme:\n      email: admin@example.com\n      storage: /acme/acme.json\n      httpChallenge:\n        entryPoint: http\n```\n\n"
             "## Internal PKI\n- Created root CA with 10-year validity\n- Intermediate CA for service certs\n- `cfssl` for certificate generation\n- Certs auto-renewed 30 days before expiry\n\n"
             "No more manual cert renewal!"),

            ("Log Aggregation with Loki",
             "Replaced ELK with Grafana Loki for log management.\n\n"
             "## Why Loki over ELK\n- 10x less resource usage\n- Same query language as Prometheus (LogQL)\n- Native Grafana integration\n- No index overhead\n\n"
             "## Architecture\n```\nContainers -> Promtail -> Loki -> Grafana\n                          |\n                          v\n                     S3 (long-term)\n```\n\n"
             "## Retention\n- Hot storage: 7 days (local SSD)\n- Cold storage: 90 days (S3)\n- Compaction: daily\n\n"
             "## Common Queries\n```logql\n{job=\"api\"} |= \"error\" | json | status >= 500\n{job=\"api\"} | json | latency > 1000 | line_format \"{{.method}} {{.path}} {{.latency}}ms\"\n```\n\n"
             "Saved ~60% on infrastructure costs compared to Elasticsearch."),

            ("Network Segmentation with Docker Networks",
             "Organized container networking for better security.\n\n"
             "## Network Layout\n| Network | Purpose | Services |\n|---------|---------|----------|\n| frontend | Public-facing | Nginx, Traefik |\n| backend | App services | API, Workers |\n| data | Databases | PostgreSQL, Redis |\n| monitoring | Metrics | Prometheus, Grafana |\n\n"
             "## Rules\n- Frontend can reach backend only\n- Backend can reach data and monitoring\n- Data network has no external access\n- Monitoring can reach all networks (read-only)\n\n"
             "```yaml\nnetworks:\n  frontend:\n    driver: bridge\n  backend:\n    driver: bridge\n    internal: true\n  data:\n    driver: bridge\n    internal: true\n```"),

            ("Automated Server Provisioning",
             "Created Ansible playbooks for new server setup.\n\n"
             "## Playbook Stages\n1. **Base** - user accounts, SSH hardening, firewall\n2. **Docker** - install Docker CE, configure daemon\n3. **Monitoring** - node exporter, promtail\n4. **Security** - fail2ban, unattended upgrades\n5. **App** - deploy application stack\n\n"
             "## Key Roles\n```\nroles/\n  base/          # System setup, users, SSH\n  docker/        # Docker CE install\n  monitoring/    # Prometheus exporters\n  security/      # Firewall, fail2ban\n  app/           # Application deployment\n```\n\n"
             "New server goes from bare metal to production-ready in ~15 minutes.\n"
             "Tested on Ubuntu 22.04 and 24.04."),

            ("DNS and CDN Configuration",
             "Set up Cloudflare for DNS management and CDN.\n\n"
             "## DNS Records\n- A records for primary domains\n- CNAME for subdomains\n- MX for email\n- TXT for SPF, DKIM, DMARC\n\n"
             "## CDN Settings\n- Cache level: Standard\n- Browser cache TTL: 4 hours\n- Edge cache TTL: 2 hours\n- Minify: JS + CSS + HTML\n- Brotli compression: enabled\n\n"
             "## Page Rules\n1. `api.example.com/*` - Cache level: Bypass (dynamic content)\n2. `static.example.com/*` - Cache everything, Edge TTL: 1 month\n3. `*.example.com/wp-admin/*` - Security level: High\n\n"
             "## Performance Impact\n- TTFB reduced by ~40%\n- Bandwidth savings: ~65%\n- Global availability improved"),
        ],
    },

    "AI & Machine Learning": {
        "count": 80,
        "types": ["research", "note", "decision", "idea", "rfc"],
        "topics": [
            ("RAG Pipeline Architecture",
             "Designed a Retrieval-Augmented Generation pipeline for our knowledge base.\n\n"
             "## Components\n1. **Ingestion** - Parse documents, chunk, embed, store\n2. **Retrieval** - Semantic search + keyword search (hybrid)\n3. **Generation** - LLM with retrieved context\n4. **Evaluation** - Relevance scoring, faithfulness check\n\n"
             "## Chunking Strategy\n- Chunk size: 512 tokens with 50 token overlap\n- Respect paragraph boundaries\n- Keep headers as metadata\n- Maintain document hierarchy\n\n"
             "## Embedding Model\nUsing `intfloat/multilingual-e5-base` (768 dims).\n"
             "Tested against OpenAI ada-002 -- similar quality at zero API cost.\n\n"
             "## Retrieval\n```python\n# Hybrid search: 70% semantic + 30% keyword\nresults = search(\n    query=user_question,\n    semantic_weight=0.7,\n    keyword_weight=0.3,\n    top_k=5\n)\n```\n\n"
             "Accuracy on our test set: 87% relevance@5."),

            ("Fine-tuning LLM on Domain-Specific Data",
             "Experiment log for fine-tuning Llama 3 on our customer support data.\n\n"
             "## Dataset\n- 12,000 support conversations\n- Filtered for quality (CSAT >= 4)\n- Formatted as instruction/response pairs\n- Train/val/test: 80/10/10\n\n"
             "## Training Config\n```yaml\nmodel: meta-llama/Llama-3-8B\nmethod: LoRA\nlora_r: 16\nlora_alpha: 32\nepochs: 3\nbatch_size: 4\nlearning_rate: 2e-4\nwarmup_ratio: 0.03\n```\n\n"
             "## Results\n| Metric | Base | Fine-tuned |\n|--------|------|------------|\n| BLEU | 0.31 | 0.58 |\n| ROUGE-L | 0.42 | 0.71 |\n| Human eval | 3.2/5 | 4.1/5 |\n\n"
             "## Observations\n- 3 epochs was optimal; 5 epochs showed overfitting\n- LoRA r=16 sufficient; r=32 marginal improvement\n- Domain vocabulary greatly improved"),

            ("Embedding Models Comparison",
             "Benchmarked embedding models for our semantic search.\n\n"
             "## Models Tested\n1. OpenAI `text-embedding-3-small` (1536d)\n2. `intfloat/multilingual-e5-base` (768d)\n3. `BAAI/bge-small-en-v1.5` (384d)\n4. `sentence-transformers/all-MiniLM-L6-v2` (384d)\n\n"
             "## Benchmark Results (our dataset, 10K docs)\n| Model | NDCG@10 | Latency | Memory | Cost |\n|-------|---------|---------|--------|------|\n| OpenAI 3-small | 0.89 | 50ms* | 0 | $0.02/1K |\n| e5-base | 0.87 | 35ms | 1.2GB | free |\n| bge-small | 0.83 | 12ms | 400MB | free |\n| MiniLM | 0.79 | 10ms | 300MB | free |\n\n"
             "*API latency, not compute\n\n"
             "## Decision\n`e5-base` for production. Best quality/cost ratio. Multilingual support is a bonus.\n"
             "Will revisit when `e5-large` inference gets faster."),

            ("Prompt Engineering Patterns",
             "Collection of effective prompt patterns we use in production.\n\n"
             "## 1. Chain of Thought\n```\nThink step by step:\n1. Identify the main topic\n2. Extract key entities\n3. Determine sentiment\n4. Formulate response\n```\n\n"
             "## 2. Few-Shot with Examples\n```\nClassify the support ticket:\n\nTicket: \"Can't login\" -> Category: Authentication\nTicket: \"Slow loading\" -> Category: Performance\nTicket: \"Wrong price shown\" -> Category: Bug\nTicket: \"{{input}}\" -> Category:\n```\n\n"
             "## 3. Role + Context + Task\n```\nYou are a senior technical writer.\nContext: API documentation for a REST service.\nTask: Write clear, concise endpoint descriptions.\n```\n\n"
             "## 4. Output Format Specification\n```\nRespond in JSON format:\n{\"category\": string, \"confidence\": float, \"reasoning\": string}\n```\n\n"
             "Key insight: Explicit output format reduces parsing errors by 90%."),

            ("Vector Database Comparison",
             "Evaluated vector databases for our semantic search backend.\n\n"
             "## Candidates\n1. **pgvector** - PostgreSQL extension\n2. **Qdrant** - Dedicated vector DB\n3. **Weaviate** - Multi-modal vector search\n4. **Pinecone** - Managed cloud service\n\n"
             "## Results (1M vectors, 768 dims)\n| DB | QPS | Recall@10 | Memory | Ops |\n|----|-----|-----------|--------|-----|\n| pgvector (HNSW) | 150 | 0.98 | 3GB | Low |\n| Qdrant | 800 | 0.99 | 2GB | Med |\n| Weaviate | 600 | 0.97 | 4GB | Med |\n| Pinecone | 1000 | 0.99 | N/A | Zero |\n\n"
             "## Decision\npgvector for now. We already run PostgreSQL, no new infra needed.\n"
             "At >5M vectors or >500 QPS, will migrate to Qdrant.\n\n"
             "pgvector HNSW index config:\n```sql\nCREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops)\nWITH (m = 16, ef_construction = 64);\n```"),

            ("LLM Cost Optimization Strategies",
             "Reduced our LLM API costs by 70% without quality loss.\n\n"
             "## Strategies Applied\n\n### 1. Model Routing\nRoute queries by complexity:\n- Simple: Haiku ($0.25/MTok)\n- Medium: Sonnet ($3/MTok)\n- Complex: Opus ($15/MTok)\n\n"
             "### 2. Caching\n- Semantic cache: if similar query exists (cosine > 0.95), return cached\n- Saves ~40% of API calls\n\n"
             "### 3. Prompt Optimization\n- Removed redundant instructions\n- Compressed system prompts\n- Used structured output instead of free text\n\n"
             "### 4. Batching\n- Group similar requests\n- Process in batch where possible\n\n"
             "## Cost Breakdown\n| Month | Before | After | Savings |\n|-------|--------|-------|---------|\n| Oct | $2,400 | - | - |\n| Nov | - | $1,200 | 50% |\n| Dec | - | $720 | 70% |"),

            ("Building a Document Categorizer with AI",
             "Implemented automatic document categorization using LLM.\n\n"
             "## Approach\n1. Extract document features (title, first 500 chars, tags)\n2. Send to LLM for classification\n3. Apply confidence threshold\n4. Auto-tag documents above threshold\n\n"
             "## Category Taxonomy\n```\nTechnology/\n  Programming/\n  Infrastructure/\n  AI & ML/\nBusiness/\n  Product/\n  Finance/\n  Marketing/\nPersonal/\n  Notes/\n  Learning/\n  Health/\n```\n\n"
             "## Results\n- Precision: 92%\n- Recall: 88%\n- Processing time: ~200ms per document\n- Cost: ~$0.001 per document (Gemini Flash)\n\n"
             "Using confidence threshold of 0.55. Documents below threshold get `uncategorized` tag."),

            ("AI Agent Architecture Patterns",
             "Research on different AI agent architectures.\n\n"
             "## Pattern 1: ReAct (Reasoning + Acting)\n- LLM reasons about task, decides action\n- Executes tool, observes result\n- Repeats until done\n- Best for: multi-step problem solving\n\n"
             "## Pattern 2: Plan and Execute\n- Create full plan first\n- Execute steps sequentially\n- Re-plan if step fails\n- Best for: complex multi-tool tasks\n\n"
             "## Pattern 3: Multi-Agent\n- Specialist agents for different domains\n- Coordinator routes to appropriate agent\n- Agents can delegate to each other\n- Best for: diverse task types\n\n"
             "## Pattern 4: Reflection\n- Agent executes, then reviews own output\n- Critic agent scores quality\n- Iterate until quality threshold met\n- Best for: high-quality content generation\n\n"
             "We use Pattern 3 (multi-agent) with Rein orchestrator. Each specialist has defined role and constraints."),

            ("Semantic Search Quality Metrics",
             "Defined metrics for measuring search quality.\n\n"
             "## Metrics\n\n### 1. NDCG@K (Normalized Discounted Cumulative Gain)\n- Measures ranking quality\n- Accounts for position of relevant results\n- Our target: NDCG@10 >= 0.85\n\n"
             "### 2. Recall@K\n- Fraction of relevant docs in top K\n- Our target: Recall@5 >= 0.80\n\n"
             "### 3. MRR (Mean Reciprocal Rank)\n- Position of first relevant result\n- Our target: MRR >= 0.90\n\n"
             "### 4. User Satisfaction\n- Click-through rate on search results\n- Time to find answer\n- Search refinement rate (lower is better)\n\n"
             "## Evaluation Dataset\n- 500 queries with manual relevance judgments\n- 3 annotators per query\n- Cohen's kappa: 0.78 (good agreement)\n\n"
             "Current performance: NDCG@10=0.87, Recall@5=0.82, MRR=0.91"),

            ("Implementing Streaming LLM Responses",
             "Added streaming support for LLM responses in our chat interface.\n\n"
             "## Server-Side (FastAPI + SSE)\n```python\n@app.post('/api/chat/stream')\nasync def chat_stream(request: ChatRequest):\n    async def generate():\n        async for chunk in llm.stream(request.messages):\n            yield f'data: {json.dumps({\"text\": chunk})}\\n\\n'\n        yield 'data: [DONE]\\n\\n'\n    return StreamingResponse(generate(), media_type='text/event-stream')\n```\n\n"
             "## Client-Side (JavaScript)\n```javascript\nconst response = await fetch('/api/chat/stream', {\n  method: 'POST',\n  body: JSON.stringify({ messages }),\n});\nconst reader = response.body.getReader();\nconst decoder = new TextDecoder();\n\nwhile (true) {\n  const { done, value } = await reader.read();\n  if (done) break;\n  const text = decoder.decode(value);\n  appendToChat(text);\n}\n```\n\n"
             "Time to first token: ~200ms. Users perceive much faster responses."),
        ],
    },

    "Product Management": {
        "count": 60,
        "types": ["note", "decision", "rfc", "task", "discussion"],
        "topics": [
            ("Q1 Product Roadmap Review",
             "Reviewed progress on Q1 objectives.\n\n"
             "## Completed\n- User authentication revamp (SSO support)\n- Mobile responsive redesign\n- API v2 launch\n\n"
             "## In Progress\n- Real-time collaboration features (60% done)\n- Analytics dashboard v2 (40% done)\n\n"
             "## Deferred to Q2\n- AI-powered search (waiting for RAG pipeline)\n- Multi-language support (deprioritized)\n\n"
             "## Key Metrics\n| Metric | Target | Actual |\n|--------|--------|--------|\n| MAU | 10,000 | 12,300 |\n| DAU/MAU | 40% | 38% |\n| NPS | 50 | 47 |\n| Churn | <5% | 4.2% |\n\n"
             "Overall: on track. Focus Q2 on engagement (DAU/MAU) and NPS."),

            ("User Story: Document Search Improvement",
             "## User Story\nAs a knowledge worker, I want to search my documents by meaning (not just keywords) "
             "so that I can find relevant information even when I don't remember exact terms.\n\n"
             "## Acceptance Criteria\n1. Search returns semantically similar documents\n2. Results ranked by relevance score\n3. Search completes in <500ms for 10K documents\n4. Supports natural language queries\n5. Highlights matching passages in results\n\n"
             "## Technical Notes\n- Use embedding-based semantic search\n- Hybrid approach: combine with full-text search\n- Pre-compute embeddings on document creation\n\n"
             "## Priority: High\n## Estimate: 2 sprints\n## Dependencies: Embedding pipeline (done)"),

            ("Feature Prioritization Framework",
             "Adopted RICE scoring for feature prioritization.\n\n"
             "## RICE Formula\nScore = (Reach x Impact x Confidence) / Effort\n\n"
             "## Scoring Guide\n- **Reach**: # of users affected per quarter (100-10000)\n- **Impact**: 0.25 (minimal), 0.5 (low), 1 (medium), 2 (high), 3 (massive)\n- **Confidence**: 50% (low), 80% (medium), 100% (high)\n- **Effort**: person-weeks\n\n"
             "## Current Backlog (Top 5)\n| Feature | R | I | C | E | Score |\n|---------|---|---|---|---|-------|\n| Semantic search | 5000 | 2 | 80% | 4 | 2000 |\n| Mobile app | 8000 | 1 | 60% | 12 | 400 |\n| API webhooks | 2000 | 2 | 100% | 2 | 2000 |\n| Dark mode | 6000 | 0.5 | 100% | 1 | 3000 |\n| Bulk import | 3000 | 1 | 90% | 3 | 900 |\n\n"
             "Dark mode wins on score but semantic search is strategic. Decision: do both in parallel."),

            ("Product Launch Checklist",
             "Standard checklist for feature launches.\n\n"
             "## Pre-Launch (T-2 weeks)\n- [ ] Feature complete in staging\n- [ ] QA sign-off\n- [ ] Security review\n- [ ] Performance testing\n- [ ] Documentation updated\n- [ ] Release notes drafted\n\n"
             "## Launch Day\n- [ ] Feature flag enabled for beta users\n- [ ] Monitor error rates and latency\n- [ ] Support team briefed\n- [ ] Social media posts scheduled\n\n"
             "## Post-Launch (T+1 week)\n- [ ] Gather user feedback\n- [ ] Check adoption metrics\n- [ ] Fix critical bugs\n- [ ] Retrospective\n\n"
             "## Rollback Plan\n1. Disable feature flag\n2. Revert database migration if needed\n3. Notify affected users\n4. Post-mortem within 24 hours"),

            ("Customer Feedback Analysis - January",
             "Analyzed 200+ pieces of customer feedback from January.\n\n"
             "## Top Themes\n1. **Search quality** (45 mentions) - \"Can't find what I'm looking for\"\n2. **Performance** (32 mentions) - \"Loading takes too long\"\n3. **Mobile experience** (28 mentions) - \"Doesn't work well on phone\"\n4. **Collaboration** (22 mentions) - \"Want to share documents with team\"\n5. **Export options** (15 mentions) - \"Need PDF/Word export\"\n\n"
             "## Sentiment\n- Positive: 42%\n- Neutral: 35%\n- Negative: 23%\n\n"
             "## Action Items\n1. Prioritize semantic search (addresses #1)\n2. Optimize API response times (addresses #2)\n3. Mobile redesign in Q2 (addresses #3)\n4. Team features on roadmap (addresses #4)"),

            ("OKR Planning: Q2 Objectives",
             "Setting objectives for Q2.\n\n"
             "## O1: Improve User Engagement\n- KR1: Increase DAU/MAU from 38% to 45%\n- KR2: Reduce time-to-value from 15 min to 5 min\n- KR3: Increase feature adoption rate to 60%\n\n"
             "## O2: Expand Market Reach\n- KR1: Launch in 3 new markets (DE, FR, JP)\n- KR2: Achieve 500 new signups per week\n- KR3: Reduce CAC by 20%\n\n"
             "## O3: Build Platform Reliability\n- KR1: Achieve 99.9% uptime\n- KR2: Reduce P1 incidents to <2 per month\n- KR3: Reduce mean time to resolution to <1 hour\n\n"
             "## O4: Enhance AI Capabilities\n- KR1: Launch semantic search with >85% relevance\n- KR2: Auto-categorization accuracy >90%\n- KR3: AI features used by >30% of active users"),

            ("A/B Testing Framework Setup",
             "Implemented A/B testing framework for feature experiments.\n\n"
             "## Architecture\n- Feature flags with variant assignment\n- User bucketing by consistent hash\n- Event tracking for metrics\n- Statistical analysis pipeline\n\n"
             "## Current Experiments\n| Experiment | Variants | Metric | Status |\n|------------|----------|--------|--------|\n| Search UI | List vs Cards | CTR | Running |\n| Onboarding | 3-step vs 5-step | Completion | Concluded |\n| Pricing | Annual vs Monthly | Conversion | Running |\n\n"
             "## Onboarding Results\n- 3-step: 72% completion\n- 5-step: 58% completion\n- Winner: 3-step (p < 0.01)\n\n"
             "Minimum sample size: 1000 users per variant for 80% power."),
        ],
    },

    "Personal Notes": {
        "count": 60,
        "types": ["note", "idea", "discussion"],
        "topics": [
            ("Weekly Goals and Reflection",
             "## This Week's Goals\n1. Finish the API documentation\n2. Review 3 pull requests\n3. Set up monitoring alerts\n4. Write blog post draft\n\n"
             "## Last Week's Reflection\n- Completed the database migration successfully\n- Got sidetracked by urgent bug fix on Tuesday\n- Need to protect focus time better\n- Started morning routine of 30 min reading\n\n"
             "## Key Learning\nSaid 'no' to two meeting invites that didn't need my presence. "
             "Freed up 2 hours that I used for deep work. Need to keep doing this.\n\n"
             "## Energy Levels\n- Monday: High (fresh start)\n- Tuesday: Low (bug fire)\n- Wednesday: Medium\n- Thursday: High (flow state on docs)\n- Friday: Medium\n\n"
             "Pattern: Tuesdays tend to be interrupt-heavy. Block calendar for focus time."),

            ("Ideas for Side Projects",
             "Collection of side project ideas to explore.\n\n"
             "## 1. CLI Bookmark Manager\n- Save URLs from terminal with tags\n- Full-text search across bookmarks\n- Export to various formats\n- Tech: Rust + SQLite\n- Complexity: Low, weekend project\n\n"
             "## 2. Personal Knowledge Graph\n- Connect notes, articles, ideas visually\n- Auto-detect relationships\n- Graph visualization\n- Tech: Python + Neo4j + D3.js\n- Complexity: Medium, 2-3 weeks\n\n"
             "## 3. Smart Home Dashboard\n- Aggregate data from Home Assistant\n- Custom widgets for energy, weather, tasks\n- E-ink display in hallway\n- Tech: React + Raspberry Pi\n- Complexity: Medium, 2 weeks\n\n"
             "Priority: Start with #1 (smallest scope, highest utility)."),

            ("Morning Routine Optimization",
             "Experimenting with my morning routine for better productivity.\n\n"
             "## Current Routine (6:30 - 8:30)\n1. 6:30 - Wake up, no phone\n2. 6:45 - Coffee + 20 min reading\n3. 7:15 - Exercise (3x/week) or walk (other days)\n4. 7:45 - Shower\n5. 8:00 - Plan the day (review calendar, top 3 priorities)\n6. 8:30 - Start deep work\n\n"
             "## What Works\n- No phone before 8am is game-changing\n- Reading first thing sets a calm tone\n- Planning top 3 priorities prevents scattered work\n\n"
             "## What Needs Improvement\n- Exercise consistency (only 2x last week instead of 3x)\n- Sometimes skip planning when feeling rushed\n- Need to sleep earlier to wake at 6:30 consistently\n\n"
             "## Experiment\nTrying 'habit stacking' - attach exercise to coffee (coffee only after exercise)."),

            ("Personal Finance Review - January",
             "Monthly financial check-in.\n\n"
             "## Income\n- Salary: $X\n- Side projects: $Y\n- Investments: $Z\n\n"
             "## Budget vs Actual\n| Category | Budget | Actual | Status |\n|----------|--------|--------|--------|\n| Housing | 30% | 29% | [OK] |\n| Food | 15% | 18% | Over |\n| Transport | 10% | 8% | Under |\n| Savings | 20% | 22% | [OK] |\n| Other | 25% | 23% | [OK] |\n\n"
             "## Notes\n- Food spending up due to holiday gatherings\n- Saved extra from transport (WFH more this month)\n- Emergency fund at 5 months (target: 6)\n- Rebalanced portfolio: increased bond allocation"),

            ("Book Reading Notes: Atomic Habits",
             "Key takeaways from 'Atomic Habits' by James Clear.\n\n"
             "## Core Framework: Four Laws\n1. **Make it obvious** - Design your environment\n2. **Make it attractive** - Pair with enjoyable activity\n3. **Make it easy** - Reduce friction\n4. **Make it satisfying** - Immediate reward\n\n"
             "## Best Insights\n- \"You do not rise to the level of your goals. You fall to the level of your systems.\"\n- Identity-based habits: focus on who you want to become, not what you want to achieve\n- The 2-minute rule: scale down any habit to 2 minutes to start\n- Habit stacking: 'After I [current habit], I will [new habit]'\n\n"
             "## Application\n- Implemented 2-minute rule for exercise: 'Put on running shoes'\n- Habit stack: After coffee, read 20 minutes\n- Environment design: Phone charges in another room at night\n\n"
             "Rating: 5/5 - Most actionable self-improvement book I've read."),

            ("Year-End Life Review",
             "Annual reflection across life areas.\n\n"
             "## Career\n- Promoted to senior engineer\n- Led 2 major projects\n- Started mentoring junior devs\n- Area to improve: public speaking, conference talks\n\n"
             "## Health\n- Consistent exercise 3x/week (mostly)\n- Lost 5kg, reached target weight\n- Started meditation (still inconsistent)\n- Area to improve: sleep quality, less screen time at night\n\n"
             "## Relationships\n- Made effort to meet friends monthly\n- Better communication with partner\n- Called family weekly\n- Area to improve: make new friends outside work\n\n"
             "## Learning\n- Read 24 books (target was 20)\n- Completed Rust course\n- Started learning Japanese\n- Area to improve: deeper focus, less breadth\n\n"
             "## One Word for Next Year: DEPTH\n"
             "Go deeper into fewer things rather than spreading thin."),

            ("Cooking Experiments Log",
             "Tracking new recipes I've tried.\n\n"
             "## Sourdough Bread\n- Attempt #3 was the best\n- Key: longer cold ferment (24h vs 12h)\n- Hydration 75% is my sweet spot\n- Need a better scoring blade\n\n"
             "## Thai Green Curry\n- Homemade paste is 10x better than store-bought\n- Galangal makes a huge difference\n- Found good Asian grocery on Main St\n- Freezing paste in ice cube trays works great\n\n"
             "## Pizza Dough\n- 72-hour cold ferment for best flavor\n- 65% hydration for home oven\n- Cast iron pan method gives good crust\n- Baking steel ordered for next attempt\n\n"
             "## General Notes\n- Meal prep Sundays: curry base + rice for 3 weekday lunches\n- Salt early, salt enough\n- Mise en place really does make cooking more enjoyable"),
        ],
    },

    "Books & Reading": {
        "count": 50,
        "types": ["note", "research"],
        "topics": [
            ("Book Summary: Designing Data-Intensive Applications",
             "Summary of DDIA by Martin Kleppmann.\n\n"
             "## Part I: Foundations\n- **Reliability**: System works correctly despite faults\n- **Scalability**: System handles growth gracefully\n- **Maintainability**: People can work on it productively\n\n"
             "## Part II: Distributed Data\n- Replication: leader-follower, multi-leader, leaderless\n- Partitioning: by key range or hash\n- Transactions: ACID properties, isolation levels\n- Consensus: Paxos, Raft\n\n"
             "## Part III: Derived Data\n- Batch processing: MapReduce, Spark\n- Stream processing: Kafka, event sourcing\n- Future of data systems\n\n"
             "## Key Takeaways\n1. There's no perfect database; understand trade-offs\n2. Distributed systems are fundamentally different from single-node\n3. Eventual consistency is not just a technical concept, it's a business decision\n\n"
             "Rating: 5/5 - Essential reading for any backend engineer."),

            ("Book Review: The Pragmatic Programmer",
             "20th anniversary edition review.\n\n"
             "## Top Principles\n1. **DRY** (Don't Repeat Yourself) - Knowledge should have a single representation\n2. **Orthogonality** - Components should be independent\n3. **Tracer Bullets** - Build end-to-end thin slice first\n4. **Broken Windows** - Fix bad designs, wrong decisions immediately\n5. **Stone Soup** - Be a catalyst for change\n\n"
             "## Most Impactful Tips\n- \"Care about your craft\" - Take pride in your work\n- \"Think about your work\" - Don't code on autopilot\n- \"You can't write perfect software\" - Design for failure\n- \"Use a single editor well\" - Master your tools\n\n"
             "## What's Changed in 20 Years\n- Cloud computing wasn't a thing\n- CI/CD is now standard practice\n- Testing is expected, not optional\n- Version control is universal\n\n"
             "Still highly relevant. Recommended for all levels."),

            ("Reading List: Q1 Technical Books",
             "Books to read this quarter.\n\n"
             "## In Progress\n1. **System Design Interview (Vol 2)** - Alex Xu\n   - Currently on Ch. 7 (Hotel Reservation System)\n   - Great real-world examples\n\n"
             "## Planned\n2. **Crafting Interpreters** - Robert Nystrom\n   - Want to understand language design\n   - Will implement in Rust instead of Java\n\n"
             "3. **Database Internals** - Alex Petrov\n   - Deeper understanding of B-trees, LSM\n   - Complement to DDIA\n\n"
             "## Completed\n4. **Staff Engineer** - Will Larson\n   - Good framework for IC leadership\n   - Key concept: 'Create technical leverage'\n\n"
             "5. **A Philosophy of Software Design** - John Ousterhout\n   - Deep vs shallow modules\n   - Complexity is the enemy\n   - Changed how I think about API design"),

            ("Quotes Collection: Software Engineering",
             "Favorite quotes from books and talks.\n\n"
             "## On Simplicity\n> \"Simplicity is prerequisite for reliability.\" - Edsger Dijkstra\n\n"
             "> \"Perfection is achieved not when there is nothing more to add, but when there is nothing left to take away.\" - Antoine de Saint-Exupery\n\n"
             "## On Design\n> \"The purpose of abstracting is not to be vague, but to create a new semantic level in which one can be absolutely precise.\" - Dijkstra\n\n"
             "> \"Any fool can write code that a computer can understand. Good programmers write code that humans can understand.\" - Martin Fowler\n\n"
             "## On Process\n> \"Weeks of coding can save you hours of planning.\" - Unknown\n\n"
             "> \"Make it work, make it right, make it fast.\" - Kent Beck\n\n"
             "## On Systems\n> \"A complex system that works is invariably found to have evolved from a simple system that works.\" - John Gall"),

            ("Book Summary: Clean Architecture",
             "Robert C. Martin's Clean Architecture summary.\n\n"
             "## Core Principles\n- **Dependency Rule**: Dependencies point inward (toward business logic)\n- **Entities**: Enterprise business rules\n- **Use Cases**: Application business rules\n- **Interface Adapters**: Controllers, presenters, gateways\n- **Frameworks**: Web, DB, UI (outermost ring)\n\n"
             "## The Architecture\n```\n[Frameworks & Drivers]\n  [Interface Adapters]\n    [Use Cases]\n      [Entities]\n```\n\n"
             "## Key Insight\nThe architecture should scream its use case, not its framework.\n"
             "Looking at the top-level structure should tell you 'this is a healthcare system', "
             "not 'this is a Rails app'.\n\n"
             "## Practical Application\n- Separate business logic from infrastructure\n- Use dependency injection\n- Test business rules without UI or database\n- Framework is a detail, not the architecture\n\n"
             "Useful principles, though sometimes over-engineered for small projects."),

            ("Book Summary: The Phoenix Project",
             "Novel about DevOps transformation. Key takeaways.\n\n"
             "## The Three Ways\n\n### First Way: Flow\n- Work flows from left to right (Dev -> Ops -> Customer)\n- Make work visible\n- Reduce batch sizes\n- Reduce WIP (Work in Progress)\n\n"
             "### Second Way: Feedback\n- Fast feedback loops at every stage\n- Don't pass defects downstream\n- Create quality at source\n\n"
             "### Third Way: Continual Learning\n- Foster experimentation\n- Allow failure and learn from it\n- Practice makes perfect\n\n"
             "## The Four Types of Work\n1. **Business projects** - Revenue-generating\n2. **Internal projects** - Infrastructure, tooling\n3. **Changes** - Maintenance, updates\n4. **Unplanned work** - Firefighting (the killer)\n\n"
             "## Key Quote\n> \"Improving daily work is more important than doing daily work.\"\n\n"
             "Entertaining and educational. Good for convincing management about DevOps."),
        ],
    },

    "News & Articles": {
        "count": 50,
        "types": ["note", "research"],
        "topics": [
            ("Saved: The State of AI Report 2025",
             "Key highlights from the annual State of AI report.\n\n"
             "## Model Capabilities\n- GPT-5 level models now available open-source\n- Multimodal models are the default\n- Code generation reaches human-level on benchmarks\n- Reasoning models (o3-class) show emergent capabilities\n\n"
             "## Industry Trends\n- AI spending exceeds $200B globally\n- Enterprise adoption at 67% (up from 42%)\n- Regulation frameworks in EU, US, China\n- AI safety research growing 3x YoY\n\n"
             "## Open Source\n- Llama, Mistral, DeepSeek leading open models\n- Fine-tuning commodity hardware feasible\n- Self-hosted LLMs viable for production\n\n"
             "## My Takeaways\n- Local models good enough for 80% of use cases\n- RAG remains the most practical enterprise pattern\n- Agent frameworks still immature but improving fast"),

            ("Article: Why SQLite is Great for Prototyping",
             "Notes from a blog post about SQLite in production.\n\n"
             "## Arguments For\n- Zero configuration, no server\n- Single file, easy backup\n- Surprisingly fast for read-heavy workloads\n- WAL mode supports concurrent reads\n- Perfect for embedded, mobile, edge\n\n"
             "## Arguments Against\n- Single writer at a time\n- No network access (same machine only)\n- No fine-grained access control\n- Limited concurrent write throughput\n\n"
             "## When to Use\n- Prototyping and development\n- Single-user applications\n- Embedded devices / mobile\n- Read-heavy workloads\n- Test databases\n\n"
             "## When NOT to Use\n- Multiple servers need access\n- High write concurrency\n- Need replication\n\n"
             "Interesting trend: Turso/LibSQL adding replication to SQLite. Could change the equation."),

            ("Saved: PostgreSQL 17 New Features",
             "Overview of new features in PostgreSQL 17.\n\n"
             "## Performance\n- Improved VACUUM performance (up to 20x for large tables)\n- Better query planning for window functions\n- Incremental backup support\n- Parallel B-tree index builds\n\n"
             "## New SQL Features\n- `JSON_TABLE()` function (finally!)\n- Merge command improvements\n- SQL/JSON path improvements\n\n"
             "## Security\n- `pg_maintain` role for routine maintenance\n- Improved SCRAM-SHA-256 handling\n\n"
             "## Developer Experience\n- `COPY ... RETURNING` for inserted rows\n- Better error messages for constraint violations\n- `pg_stat_checkpointer` for monitoring\n\n"
             "Most excited about `JSON_TABLE()` and incremental backups.\n"
             "Planning upgrade for Q2."),

            ("Tech Trend: WebAssembly Beyond the Browser",
             "Research on WASM's expanding ecosystem.\n\n"
             "## Current Use Cases\n1. **Edge Computing** - Cloudflare Workers, Fastly Compute\n2. **Server-side** - Spin, Wasmtime, WasmEdge\n3. **Plugin Systems** - Extism, Envoy WASM filters\n4. **Blockchain** - Smart contracts (CosmWasm)\n\n"
             "## Why WASM?\n- Near-native performance\n- Language agnostic (Rust, Go, C++, Python)\n- Sandboxed execution\n- Portable across platforms\n- Fast startup (< 1ms)\n\n"
             "## Challenges\n- Component model still evolving\n- Debugging is primitive\n- Ecosystem fragmented (WASI, WAGI, etc.)\n- GC support still new\n\n"
             "## Prediction\nWASM will become the standard for plugin systems within 2 years.\n"
             "Already seeing this in databases (PGlite), CDNs, and edge functions."),

            ("Saved: Rust vs Go for Backend Services",
             "Comparison article with my annotations.\n\n"
             "## Performance\n- Rust: Faster, lower memory, no GC pauses\n- Go: Fast enough, GC is excellent, simpler concurrency\n\n"
             "## Developer Experience\n- Rust: Steep learning curve, compiler is strict but helpful\n- Go: Easy to learn, productive quickly, less expressiveness\n\n"
             "## Ecosystem\n- Rust: Cargo is excellent, ecosystem growing fast (Actix, Axum)\n- Go: Standard library covers 80%, less dependency management\n\n"
             "## When to Choose What\n| Factor | Choose Rust | Choose Go |\n|--------|-------------|----------|\n| Performance critical | X | |\n| Team familiarity | | X (usually) |\n| Hire ability | | X |\n| Systems programming | X | |\n| Microservices | | X |\n| CLI tools | X | X (both good) |\n\n"
             "## My Take\nGo for most backend services (team productivity wins).\n"
             "Rust for performance-critical paths, parsers, and systems work.\n"
             "Not a religious war -- use the right tool for the job."),

            ("Article Notes: The Twelve-Factor App in 2026",
             "Modern interpretation of the 12-factor methodology.\n\n"
             "## Still Highly Relevant\n1. **Codebase** - One repo per service (monorepos also valid)\n2. **Dependencies** - Lock files, reproducible builds\n3. **Config** - Environment variables, not config files\n4. **Backing services** - Treat as attached resources\n5. **Build, release, run** - Container images are the artifact\n\n"
             "## Updated for Modern Stack\n6. **Processes** - Containers are the new process model\n7. **Port binding** - Standard with containers\n8. **Concurrency** - Horizontal scaling via container orchestration\n9. **Disposability** - Graceful shutdown, health checks\n10. **Dev/prod parity** - Docker makes this achievable\n\n"
             "## New Factors to Consider\n11. **Observability** - Structured logging, metrics, traces (not just logs)\n12. **Security** - Zero-trust, secrets management, supply chain\n\n"
             "The methodology aged well because it's about principles, not tools."),
        ],
    },

    "Research": {
        "count": 60,
        "types": ["research", "note", "idea"],
        "topics": [
            ("Benchmarking PostgreSQL Connection Pooling",
             "Compared connection pooling strategies under load.\n\n"
             "## Setup\n- PostgreSQL 16 on 4-core VM\n- pgbench with 100 concurrent clients\n- Test duration: 5 minutes each\n- Measured: TPS, latency p50/p99, CPU usage\n\n"
             "## Results\n| Configuration | TPS | p50 | p99 | CPU |\n|---------------|-----|-----|-----|-----|\n| No pooling (direct) | 850 | 45ms | 250ms | 90% |\n| PgBouncer (transaction) | 3200 | 12ms | 45ms | 60% |\n| PgBouncer (session) | 1400 | 28ms | 120ms | 75% |\n| Built-in (asyncpg pool) | 2800 | 15ms | 55ms | 65% |\n\n"
             "## Observations\n- PgBouncer in transaction mode is the clear winner\n- Session mode barely better than no pooling\n- asyncpg pool is solid when you can't deploy PgBouncer\n- Pool size 20-30 optimal for our workload\n\n"
             "## Configuration\n```ini\n[pgbouncer]\npool_mode = transaction\nmax_client_conn = 200\ndefault_pool_size = 25\nreserve_pool_size = 5\n```"),

            ("HTTP/3 and QUIC Investigation",
             "Research on adopting HTTP/3 for our services.\n\n"
             "## What's HTTP/3?\n- Built on QUIC (UDP-based transport)\n- Eliminates head-of-line blocking\n- Built-in encryption (TLS 1.3)\n- Connection migration (IP change doesn't break connection)\n- 0-RTT connection establishment\n\n"
             "## Benefits for Us\n- Better mobile experience (connection migration)\n- Faster page loads (0-RTT)\n- Reduced latency on lossy networks\n\n"
             "## Current Support\n- Nginx: experimental (since 1.25)\n- Caddy: full support\n- Cloudflare: enabled by default\n- Browsers: Chrome, Firefox, Safari all support\n\n"
             "## Decision\nEnable via Cloudflare (no server changes needed).\n"
             "Revisit direct server support when Nginx module stabilizes.\n\n"
             "Risk: Low. Browsers fall back to HTTP/2 automatically."),

            ("Memory Allocation Patterns in Python",
             "Investigation into Python memory usage for our embedding service.\n\n"
             "## Problem\nEmbedding service memory grows to 4GB over 24 hours.\n\n"
             "## Tools Used\n- `tracemalloc` for allocation tracking\n- `objgraph` for reference counting\n- `memory_profiler` for line-by-line analysis\n\n"
             "## Findings\n1. NumPy arrays from batch embedding not being freed\n2. LRU cache growing unbounded\n3. Logging handler keeping references to old records\n\n"
             "## Fixes Applied\n```python\n# 1. Explicit cleanup after batch processing\ndel embeddings\ngc.collect()\n\n# 2. Bounded cache\n@lru_cache(maxsize=1000)  # was unbounded\ndef get_embedding(text):\n    ...\n\n# 3. Rotating log handler\nhandler = RotatingFileHandler(\n    'app.log', maxBytes=10*1024*1024, backupCount=5\n)\n```\n\n"
             "Memory now stable at ~1.5GB under same workload."),

            ("Comparing Message Queues for Event-Driven Architecture",
             "Evaluated message queue options for our new event system.\n\n"
             "## Candidates\n1. **RabbitMQ** - Traditional message broker\n2. **Apache Kafka** - Distributed event streaming\n3. **Redis Streams** - Lightweight, built-in\n4. **NATS** - Cloud-native messaging\n\n"
             "## Comparison\n| Feature | RabbitMQ | Kafka | Redis | NATS |\n|---------|----------|-------|-------|------|\n| Throughput | 50K/s | 1M/s | 100K/s | 300K/s |\n| Latency | <1ms | <5ms | <1ms | <1ms |\n| Persistence | Yes | Yes | Optional | Optional |\n| Ordering | Per-queue | Per-partition | Per-stream | No |\n| Complexity | Medium | High | Low | Low |\n| Memory | Medium | High | Low | Low |\n\n"
             "## Decision\nRedis Streams for now. Already running Redis, simple API, good enough throughput.\n"
             "Will migrate to Kafka only if we need >100K events/sec or multi-DC replication."),

            ("Analysis: Cost of Self-Hosting vs Cloud Services",
             "Compared costs of self-hosting our stack vs managed services.\n\n"
             "## Current Self-Hosted Stack\n| Service | Server | Monthly Cost |\n|---------|--------|-------------|\n| PostgreSQL | Hetzner CX31 | $12 |\n| Redis | Same server | $0 |\n| App servers (x2) | Hetzner CX21 | $16 |\n| Storage (500GB) | Hetzner Volume | $12 |\n| Backups | Backblaze B2 | $3 |\n| **Total** | | **$43** |\n\n"
             "## Equivalent Managed Services\n| Service | Provider | Monthly Cost |\n|---------|----------|-------------|\n| RDS PostgreSQL | AWS | $50 |\n| ElastiCache Redis | AWS | $25 |\n| ECS Fargate (x2) | AWS | $60 |\n| EBS Storage | AWS | $50 |\n| S3 Backups | AWS | $5 |\n| **Total** | | **$190** |\n\n"
             "## Analysis\n- Self-hosted: $43/mo + ~5 hrs/mo maintenance\n- Managed: $190/mo + ~1 hr/mo maintenance\n- Delta: $147/mo = $1,764/year\n- Break-even if maintenance time valued at $29/hr\n\n"
             "Staying self-hosted. The maintenance time is actually learning time."),

            ("WebSocket vs Server-Sent Events Analysis",
             "Evaluating real-time communication options.\n\n"
             "## WebSocket\n- Full-duplex communication\n- Custom protocol over TCP\n- Requires connection management\n- Better for: chat, gaming, collaborative editing\n\n"
             "## Server-Sent Events (SSE)\n- Server to client only\n- Standard HTTP (works with proxies)\n- Auto-reconnection built-in\n- Better for: notifications, live feeds, progress updates\n\n"
             "## Comparison\n| Feature | WebSocket | SSE |\n|---------|-----------|-----|\n| Direction | Bidirectional | Server -> Client |\n| Protocol | ws:// | Standard HTTP |\n| Reconnection | Manual | Automatic |\n| Binary data | Yes | No (text only) |\n| Proxy support | Needs upgrade | Works everywhere |\n| Browser support | Universal | Universal (polyfill for IE) |\n\n"
             "## Decision for Our Use Case\nSSE for notifications and activity feeds (90% of our real-time needs).\n"
             "WebSocket only for the collaborative editor feature."),
        ],
    },

    "Learning": {
        "count": 60,
        "types": ["note", "research", "idea"],
        "topics": [
            ("TIL: PostgreSQL EXPLAIN ANALYZE Deep Dive",
             "Today I learned how to properly read EXPLAIN ANALYZE output.\n\n"
             "## Key Concepts\n- **Seq Scan**: Full table scan (bad for large tables)\n- **Index Scan**: Uses index (good)\n- **Bitmap Index Scan**: Multiple index results combined\n- **Hash Join**: Build hash table, probe with other relation\n- **Nested Loop**: For each row in outer, scan inner\n\n"
             "## Reading the Output\n```sql\nEXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)\nSELECT * FROM orders WHERE customer_id = 42;\n```\n\n"
             "```\nIndex Scan using idx_orders_customer on orders\n  (cost=0.42..8.44 rows=1 width=100)\n  (actual time=0.023..0.025 rows=3 loops=1)\n  Index Cond: (customer_id = 42)\n  Buffers: shared hit=4\n```\n\n"
             "## What to Look For\n1. **actual vs estimated rows**: If off by 10x+, run ANALYZE\n2. **Buffers: shared hit vs read**: Reads mean cache misses\n3. **loops**: Multiply time by loops for true cost\n4. **Seq Scan on large table**: Add an index\n\n"
             "## Useful Settings\n```sql\nSET enable_seqscan = off;  -- Force index usage (testing only!)\nSET work_mem = '256MB';     -- Larger sorts in memory\n```"),

            ("Rust Ownership Model Cheatsheet",
             "Quick reference for Rust's ownership rules.\n\n"
             "## Three Rules\n1. Each value has exactly one owner\n2. When owner goes out of scope, value is dropped\n3. You can have either ONE mutable reference OR any number of immutable references\n\n"
             "## Move vs Copy\n```rust\n// Move (heap data)\nlet s1 = String::from(\"hello\");\nlet s2 = s1; // s1 is INVALID now\n\n// Copy (stack data)\nlet x = 5;\nlet y = x; // x is still valid\n```\n\n"
             "## Borrowing\n```rust\nfn takes_ref(s: &String) { }      // Immutable borrow\nfn takes_mut(s: &mut String) { }   // Mutable borrow\n\nlet mut s = String::from(\"hello\");\ntakes_ref(&s);      // OK\ntakes_mut(&mut s);  // OK\n```\n\n"
             "## Lifetime Basics\n```rust\nfn longest<'a>(x: &'a str, y: &'a str) -> &'a str {\n    if x.len() > y.len() { x } else { y }\n}\n```\n\n"
             "Key insight: The compiler is your friend. Fight it less, listen to it more."),

            ("Course Notes: System Design Fundamentals",
             "Notes from the system design course.\n\n"
             "## Load Balancing\n- Round Robin: Simple, equal distribution\n- Least Connections: Route to least busy server\n- Consistent Hashing: Sticky sessions, good for caching\n- Geographic: Route to nearest datacenter\n\n"
             "## Caching Strategies\n- **Cache Aside**: App reads cache, fills on miss\n- **Write Through**: Write to cache and DB together\n- **Write Behind**: Write to cache, async to DB\n- **Read Through**: Cache fetches from DB on miss\n\n"
             "## Database Scaling\n- **Vertical**: Bigger machine (limited)\n- **Read Replicas**: Scale reads, not writes\n- **Sharding**: Split data across machines\n- **CQRS**: Separate read and write models\n\n"
             "## CAP Theorem\n- Consistency, Availability, Partition Tolerance\n- Pick 2 (but P is mandatory in distributed systems)\n- CP: Strong consistency, may be unavailable (banking)\n- AP: Always available, eventually consistent (social media)"),

            ("Tutorial Notes: Docker Multi-Stage Builds",
             "How to create small, secure Docker images.\n\n"
             "## The Problem\n- Build tools inflate image size\n- Dev dependencies in production\n- Large images = slow deploys, more attack surface\n\n"
             "## Multi-Stage Solution\n```dockerfile\n# Stage 1: Build\nFROM node:20 AS builder\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci\nCOPY . .\nRUN npm run build\n\n# Stage 2: Production\nFROM node:20-alpine\nWORKDIR /app\nCOPY --from=builder /app/dist ./dist\nCOPY --from=builder /app/node_modules ./node_modules\nEXPOSE 3000\nCMD [\"node\", \"dist/index.js\"]\n```\n\n"
             "## Results\n| Stage | Image Size |\n|-------|------------|\n| Single stage | 1.2 GB |\n| Multi-stage | 180 MB |\n| With distroless | 95 MB |\n\n"
             "## Tips\n- Order COPY commands for better layer caching\n- Use `.dockerignore` to exclude `node_modules`, `.git`\n- Consider `distroless` base for even smaller images\n- Always use specific image tags, not `latest`"),

            ("Learning Log: Kubernetes Concepts",
             "Notes as I learn Kubernetes.\n\n"
             "## Core Objects\n- **Pod**: Smallest deployable unit (1+ containers)\n- **Deployment**: Manages pod replicas, rolling updates\n- **Service**: Stable network endpoint for pods\n- **ConfigMap**: Configuration data\n- **Secret**: Sensitive data (base64 encoded)\n- **Ingress**: External HTTP(S) routing\n\n"
             "## Key Commands\n```bash\nkubectl get pods                    # List pods\nkubectl describe pod <name>         # Pod details\nkubectl logs <pod> -f               # Stream logs\nkubectl exec -it <pod> -- bash      # Shell access\nkubectl apply -f deployment.yaml    # Apply config\nkubectl rollout status deployment/x # Check rollout\n```\n\n"
             "## Architecture\n```\n                 [API Server]\n                      |\n    [etcd]    [Scheduler]    [Controller Manager]\n                      |\n         [kubelet] [kube-proxy] [Container Runtime]\n              \\         |          /\n               \\        |         /\n                [   Worker Node   ]\n```\n\n"
             "Still confused about: ServiceMesh, Operators, CRDs. Will study next week."),

            ("Cheatsheet: Git Advanced Commands",
             "Git commands beyond the basics.\n\n"
             "## Interactive Rebase\n```bash\ngit rebase -i HEAD~5    # Rewrite last 5 commits\n# pick, squash, reword, edit, drop\n```\n\n"
             "## Cherry Pick\n```bash\ngit cherry-pick abc123    # Apply specific commit\ngit cherry-pick A..B      # Range of commits\n```\n\n"
             "## Bisect (Find Bug Introduction)\n```bash\ngit bisect start\ngit bisect bad              # Current commit is bad\ngit bisect good v1.0        # This tag was good\n# Git checks out middle commit\ngit bisect good/bad         # Keep marking\ngit bisect reset            # Done\n```\n\n"
             "## Reflog (Undo Almost Anything)\n```bash\ngit reflog                  # Show all HEAD movements\ngit reset --hard HEAD@{2}   # Go back 2 moves\n```\n\n"
             "## Worktrees\n```bash\ngit worktree add ../feature-x feature-x\n# Work on multiple branches simultaneously\ngit worktree remove ../feature-x\n```\n\n"
             "## Stash with Name\n```bash\ngit stash push -m \"WIP: auth refactor\"\ngit stash list\ngit stash pop stash@{2}\n```"),

            ("TIL: CSS Container Queries",
             "Finally, responsive design based on container, not viewport!\n\n"
             "## The Problem\n- Media queries based on viewport width\n- Component doesn't know its container size\n- Same component in sidebar vs main area = different viewport, same problem\n\n"
             "## Container Queries Solution\n```css\n/* 1. Define containment context */\n.card-container {\n  container-type: inline-size;\n  container-name: card;\n}\n\n/* 2. Query against container */\n@container card (min-width: 400px) {\n  .card {\n    display: grid;\n    grid-template-columns: 1fr 2fr;\n  }\n}\n\n@container card (max-width: 399px) {\n  .card {\n    display: flex;\n    flex-direction: column;\n  }\n}\n```\n\n"
             "## Browser Support\n- Chrome 105+\n- Firefox 110+\n- Safari 16+\n- ~92% global coverage\n\n"
             "This changes everything for component libraries. "
             "Components can now be truly self-contained and responsive."),
        ],
    },

    "Finance & Business": {
        "count": 50,
        "types": ["note", "research", "decision", "idea"],
        "topics": [
            ("SaaS Pricing Strategy Research",
             "Researching pricing models for our product.\n\n"
             "## Models Evaluated\n\n### 1. Per-User (Seat-Based)\n- Predictable revenue\n- Easy to understand\n- Penalizes team growth\n- Example: Slack, Notion\n\n"
             "### 2. Usage-Based\n- Aligns cost with value\n- Low barrier to entry\n- Revenue unpredictable\n- Example: AWS, Twilio\n\n"
             "### 3. Tiered\n- Clear upgrade path\n- Feature differentiation\n- Can feel arbitrary\n- Example: GitHub, Dropbox\n\n"
             "### 4. Flat Rate\n- Simple, no surprises\n- Unlimited usage encourages adoption\n- Hard to capture enterprise value\n- Example: Basecamp\n\n"
             "## Our Decision\nTiered pricing with usage caps:\n- Free: 100 docs, 1 user\n- Pro ($15/mo): 10K docs, 5 users\n- Team ($49/mo): Unlimited docs, 20 users\n- Enterprise: Custom"),

            ("Monthly Revenue Report",
             "Financial metrics for this month.\n\n"
             "## Revenue\n| Source | Amount | MoM Change |\n|--------|--------|------------|\n| Subscriptions | $12,400 | +8% |\n| API Usage | $3,200 | +15% |\n| Enterprise | $8,000 | 0% |\n| **Total** | **$23,600** | **+9%** |\n\n"
             "## Key Metrics\n- MRR: $23,600\n- ARR: $283,200\n- Churn rate: 3.2% (down from 4.1%)\n- LTV: $840\n- CAC: $120\n- LTV/CAC: 7.0 (healthy)\n\n"
             "## Analysis\n- API usage growing fastest -- indicates platform stickiness\n- Churn reduction from improved onboarding\n- Enterprise pipeline has 3 deals in negotiation\n\n"
             "## Actions\n1. Double down on API/platform story\n2. Continue onboarding improvements\n3. Close at least 1 enterprise deal this quarter"),

            ("Investment Research: Index Fund Strategy",
             "Notes on personal investment approach.\n\n"
             "## Core Portfolio (80%)\n- 60% US Total Market (VTI/ITOT)\n- 25% International Developed (VXUS/IXUS)\n- 15% Emerging Markets (VWO/IEMG)\n\n"
             "## Fixed Income (15%)\n- 70% US Aggregate Bonds (BND)\n- 30% TIPS (TIP)\n\n"
             "## Alternatives (5%)\n- REITs (VNQ)\n- Gold (GLD) -- small hedge position\n\n"
             "## Rules\n1. Contribute monthly regardless of market\n2. Rebalance annually (or if >5% drift)\n3. Never sell in panic\n4. Increase contribution when income increases\n5. Tax-advantaged accounts first\n\n"
             "## Historical Returns (10yr avg)\n- US Market: ~10% annualized\n- International: ~6% annualized\n- Bonds: ~3% annualized\n- Blended portfolio: ~8% annualized\n\n"
             "Boring strategy beats exciting speculation over decades."),

            ("Business Model Canvas: Knowledge Management Tool",
             "Canvas for our knowledge management product.\n\n"
             "## Value Proposition\n- Never lose a thought or note again\n- Find anything with natural language search\n- AI-powered organization and connections\n\n"
             "## Customer Segments\n- Individual knowledge workers\n- Small development teams (5-20)\n- Research teams\n\n"
             "## Channels\n- Product Hunt launch\n- Developer blogs and communities\n- SEO on knowledge management keywords\n- Open source community\n\n"
             "## Revenue Streams\n- Freemium subscriptions\n- API access for integrations\n- Enterprise licenses\n\n"
             "## Key Resources\n- AI/ML engineering team\n- Cloud infrastructure\n- User data and models\n\n"
             "## Cost Structure\n- Engineering salaries (60%)\n- Infrastructure (20%)\n- Marketing (10%)\n- Operations (10%)\n\n"
             "## Competitive Advantage\nSemantic search quality + open-source core + self-host option."),

            ("Startup Runway Calculator",
             "Planning financial runway for the next 18 months.\n\n"
             "## Monthly Costs\n| Category | Amount |\n|----------|--------|\n| Salaries (4 people) | $28,000 |\n| Infrastructure | $2,000 |\n| Tools & Software | $800 |\n| Studio & Misc | $1,200 |\n| Marketing | $3,000 |\n| **Total** | **$35,000** |\n\n"
             "## Revenue Projection\n| Month | MRR (Conservative) | MRR (Optimistic) |\n|-------|------|------|\n| Jan | $8,000 | $10,000 |\n| Mar | $12,000 | $18,000 |\n| Jun | $20,000 | $30,000 |\n| Sep | $28,000 | $42,000 |\n| Dec | $35,000 | $55,000 |\n\n"
             "## Runway\n- Cash on hand: $180,000\n- Conservative: Break-even in ~10 months\n- Optimistic: Break-even in ~7 months\n- Worst case (no growth): 5.1 months of runway\n\n"
             "Need to hit $20K MRR by June or start fundraising process."),
        ],
    },

    "Health & Productivity": {
        "count": 50,
        "types": ["note", "idea", "discussion"],
        "topics": [
            ("Pomodoro Technique Optimization",
             "My modified Pomodoro setup for deep work.\n\n"
             "## Modified Rules\n- 50 min work / 10 min break (not 25/5)\n- Max 4 pomodoros before long break (30 min)\n- No notifications during pomodoro\n- Physical movement during breaks\n\n"
             "## Why 50/10 Instead of 25/5\n- 25 minutes is too short for programming flow state\n- Context switching cost is ~15 minutes\n- 50 minutes allows proper deep dive\n- Works better for complex creative work\n\n"
             "## Break Activities\n- Walk around the block\n- Stretch routine (5 movements)\n- Make tea/coffee\n- Quick tidy-up\n- NOT: checking phone, social media, email\n\n"
             "## Tracking\nUsing a simple text file:\n```\n2026-01-15: [X][X][X][ ] - 3/4 pomodoros\n  - Implemented auth middleware\n  - Fixed 2 bugs in search\n```\n\n"
             "Average productive pomodoros: 3.2/day (pretty good)."),

            ("Ergonomic Workspace Setup Guide",
             "Optimized my desk setup to reduce strain.\n\n"
             "## Monitor\n- 27\" 4K at arm's length\n- Top of screen at eye level\n- Slight tilt back (~5 degrees)\n- No glare from windows\n\n"
             "## Chair\n- Armrests at desk height\n- Lumbar support engaged\n- Feet flat on floor\n- Thighs parallel to ground\n\n"
             "## Keyboard & Mouse\n- Split keyboard (Kinesis Advantage360)\n- Vertical mouse\n- Wrist rests only during breaks, not while typing\n- Keyboard at negative tilt\n\n"
             "## Routine\n- Stand desk: 20 min standing per hour\n- Eye rule: 20-20-20 (every 20 min, look 20 feet away, 20 seconds)\n- Stretch every 90 minutes\n- Walk after lunch\n\n"
             "## Result\n- Wrist pain gone after 2 weeks\n- Neck tension much less\n- More energy in the afternoon"),

            ("Sleep Optimization Notes",
             "Research and experiments on improving sleep quality.\n\n"
             "## What Works\n1. **Consistent schedule**: Same time +/- 30 min, even weekends\n2. **Cool room**: 18-19C is optimal\n3. **No screens 1 hour before bed**: Kindle is OK\n4. **Caffeine cutoff**: None after 2pm\n5. **Magnesium glycinate**: 400mg before bed\n\n"
             "## What Didn't Work\n- Melatonin: Made me groggy next morning\n- White noise machine: Prefer silence\n- Blue light glasses: Marginal effect\n\n"
             "## Sleep Stages (from tracker)\n| Stage | Average | Target |\n|-------|---------|--------|\n| Deep sleep | 1h 20m | 1h 30m |\n| REM | 1h 45m | 2h |\n| Light sleep | 3h 30m | 3h |\n| Awake | 15m | <15m |\n\n"
             "## Key Insight\nThe single biggest factor is consistent wake time.\n"
             "Sleep pressure builds naturally when you get up at the same time daily.\n"
             "Training my body to wake at 6:30 was harder than any other habit."),

            ("Weekly Review Template",
             "Template I use every Friday for weekly review.\n\n"
             "## 1. What Got Done\n- List all completed tasks and achievements\n- Note any unexpected wins\n\n"
             "## 2. What Didn't Get Done\n- Review uncompleted tasks\n- Understand why (blocked, deprioritized, or procrastinated?)\n- Reschedule or drop\n\n"
             "## 3. Key Learnings\n- What did I learn this week?\n- Any 'aha' moments?\n- What would I do differently?\n\n"
             "## 4. Next Week's Priorities\n- Top 3 priorities (must accomplish)\n- 3 nice-to-haves\n- Calendar review for meetings and deadlines\n\n"
             "## 5. Energy and Mood Check\n- Rate each day 1-5\n- Identify patterns\n- What drained energy? What created energy?\n\n"
             "## 6. Gratitude\n- Three things I'm grateful for this week\n\n"
             "Takes ~30 minutes. Best investment of the week."),

            ("Home Gym Workout Routine",
             "Minimal equipment routine, 3x/week.\n\n"
             "## Equipment\n- Pull-up bar\n- Adjustable dumbbells (5-40kg)\n- Resistance bands\n- Yoga mat\n\n"
             "## Day A: Push + Core\n1. Push-ups: 3x15-20\n2. DB Overhead Press: 3x10\n3. DB Bench Press: 3x10\n4. Dips (on chair): 3x12\n5. Plank: 3x60s\n6. Ab wheel: 3x10\n\n"
             "## Day B: Pull + Core\n1. Pull-ups: 3x8-12\n2. DB Rows: 3x10\n3. Band Face Pulls: 3x15\n4. DB Curls: 3x12\n5. Dead Bug: 3x10 each side\n6. Pallof Press: 3x10 each side\n\n"
             "## Day C: Legs + Conditioning\n1. DB Goblet Squat: 3x12\n2. DB Romanian Deadlift: 3x10\n3. DB Lunges: 3x10 each\n4. Calf Raises: 3x15\n5. KB Swings: 3x20 (or DB alternative)\n6. 10 min HIIT finisher\n\n"
             "## Schedule\nMon: A, Wed: B, Fri: C\nSat: Active recovery (walk/yoga)"),
        ],
    },

    "Travel & Life": {
        "count": 50,
        "types": ["note", "idea"],
        "topics": [
            ("Trip Notes: Lisbon, Portugal",
             "5 days in Lisbon. One of my favorite cities.\n\n"
             "## Highlights\n- **Alfama district**: Winding streets, fado music, amazing views\n- **Pasteis de Belem**: The original pastel de nata. Worth the wait.\n- **Time Out Market**: Great food hall, try everything\n- **Tram 28**: Iconic but very crowded. Take it early morning.\n- **LX Factory**: Creative hub, great bookstore, cool cafes\n\n"
             "## Food Recommendations\n- Cervejaria Ramiro: Best seafood, get the shrimp\n- Manteigaria: Better pastel de nata than Belem (hot take)\n- Solar dos Presuntos: Traditional Portuguese, book ahead\n- A Cevicheria: Peruvian-Portuguese fusion, incredible\n\n"
             "## Practical Tips\n- Lisboa Card: Worth it for 72h (free transport + museums)\n- Uber is cheaper than taxis\n- Hills are brutal in summer heat\n- Many restaurants closed on Sundays\n- Cash only at some traditional places\n\n"
             "## Budget\n- Flights: $180 (Ryan Air, book 3 months ahead)\n- Accommodation: $65/night (Airbnb in Bairro Alto)\n- Food: ~$40/day\n- Activities: ~$20/day\n- Total: ~$600 for 5 days"),

            ("Apartment Hunting Checklist",
             "What to check when viewing apartments.\n\n"
             "## Must-Haves\n- [ ] Natural light in main rooms\n- [ ] Reliable internet (test speed during visit)\n- [ ] Quiet neighborhood (visit at different times)\n- [ ] Within 30 min commute\n- [ ] Proper kitchen (not kitchenette)\n- [ ] Washing machine (or laundry in building)\n\n"
             "## Nice-to-Haves\n- [ ] Balcony or terrace\n- [ ] Dedicated workspace area\n- [ ] Bathtub (not just shower)\n- [ ] Storage room\n- [ ] Bike parking\n\n"
             "## Red Flags\n- Landlord refuses to put things in writing\n- Signs of mold or water damage\n- Noisy neighbors during visit\n- Building maintenance looks neglected\n- Too good to be true price\n\n"
             "## Questions to Ask\n1. What's included in rent? (Utilities, internet, parking)\n2. How responsive is maintenance?\n3. Pet policy?\n4. Lease term flexibility?\n5. History of rent increases?"),

            ("Packing List for Digital Nomad Travel",
             "Optimized packing list for 2-4 week trips.\n\n"
             "## Tech\n- Laptop + charger\n- Phone + cable\n- Wireless earbuds + over-ear headphones\n- USB-C hub\n- Portable monitor (15\")\n- Universal adapter\n- Power bank (20000mAh)\n\n"
             "## Clothing (1 week rotation)\n- 5 t-shirts (merino wool)\n- 2 shorts / 2 pants\n- 7 underwear + socks\n- 1 hoodie\n- 1 rain jacket (packable)\n- Sandals + walking shoes\n\n"
             "## Accessories\n- Passport + copies\n- Microfiber towel\n- Water bottle (collapsible)\n- First aid basics\n- Padlock for hostels\n- Packing cubes (game changer)\n\n"
             "## Everything fits in:\n- 40L backpack (carry-on size)\n- Small day bag\n\n"
             "Total weight: ~8kg\n\n"
             "Rule: If I haven't used something on 3 trips, it gets removed from the list."),

            ("Restaurant Reviews: Local Favorites",
             "My go-to restaurants ranked.\n\n"
             "## Italian\n**Trattoria Verde** - Authentic, family-run\n- Best carbonara in the city\n- House wine is surprisingly good\n- Reservation needed for weekends\n- Price: $$\n- Rating: 9/10\n\n"
             "## Japanese\n**Sushi Tanaka** - Omakase experience\n- Fresh fish daily\n- Sit at the bar for the full experience\n- Expensive but worth it for special occasions\n- Price: $$$$\n- Rating: 8.5/10\n\n"
             "## Mexican\n**Casa Verde** - Modern Mexican\n- Great tacos al pastor\n- Mezcal selection is incredible\n- Fun atmosphere, can be loud\n- Price: $$\n- Rating: 8/10\n\n"
             "## Vietnamese\n**Pho Saigon** - Hole in the wall\n- Best pho in town, hands down\n- Cash only, no reservations\n- Long lunch lines but moves fast\n- Price: $\n- Rating: 9/10"),

            ("Weekend Trip Planner Template",
             "Template for planning 2-3 day getaways.\n\n"
             "## Pre-Trip\n- [ ] Accommodation booked\n- [ ] Transport arranged (flights/trains/car)\n- [ ] Key attractions researched\n- [ ] Restaurants shortlisted\n- [ ] Weather checked\n- [ ] Pack (see packing list note)\n\n"
             "## Day Planning\nRule: max 3 planned activities per day. Leave room for spontaneity.\n\n"
             "### Day 1 (Arrival)\n- Arrive, drop bags\n- Explore neighborhood on foot\n- Nice dinner\n\n"
             "### Day 2 (Full Day)\n- Morning: main attraction\n- Lunch: local favorite\n- Afternoon: secondary attraction or free time\n- Evening: different neighborhood\n\n"
             "### Day 3 (Departure)\n- Morning: breakfast spot\n- Quick walk or visit\n- Head to airport/station\n\n"
             "## Budget Template\n| Item | Estimate |\n|------|----------|\n| Transport | |\n| Accommodation | |\n| Food (per day) | |\n| Activities | |\n| Buffer (10%) | |\n| **Total** | |"),
        ],
    },

    "Design & UX": {
        "count": 40,
        "types": ["note", "decision", "rfc", "research", "idea"],
        "topics": [
            ("Design System Architecture",
             "Building a design system from scratch for our product.\n\n"
             "## Principles\n1. **Consistency over creativity** -- predictable patterns reduce cognitive load\n2. **Composable tokens** -- colors, spacing, typography defined once\n3. **Accessible by default** -- every component meets WCAG 2.1 AA\n\n"
             "## Token Hierarchy\n```\nPrimitive tokens  -> color-blue-500, spacing-4\nSemantic tokens   -> color-primary, spacing-md\nComponent tokens  -> button-bg, button-padding\n```\n\n"
             "## Component Library\nBuilt with Storybook + React. Each component has:\n- Props documentation\n- Accessibility notes\n- Usage examples\n- Do/Don't guidelines\n\n"
             "## Tooling\n- Figma for design, with token sync via Style Dictionary\n- Storybook 8 for component playground\n- Chromatic for visual regression testing\n\n"
             "Starting with: Button, Input, Card, Modal, Toast, Avatar, Badge."),

            ("Accessibility Audit Results",
             "WCAG 2.1 AA audit of our main application.\n\n"
             "## Critical Issues (P0)\n1. Missing alt text on 23 images\n2. Color contrast ratio below 4.5:1 on secondary text\n3. No keyboard navigation in dropdown menus\n4. Form errors not announced to screen readers\n\n"
             "## Major Issues (P1)\n1. Focus trap missing in modal dialogs\n2. Skip navigation link absent\n3. Tab order incorrect in sidebar navigation\n4. No visible focus indicators on custom components\n\n"
             "## Tools Used\n- Axe DevTools (automated scan)\n- NVDA + Chrome (manual screen reader testing)\n- Colour Contrast Analyser\n- Keyboard-only navigation walkthrough\n\n"
             "## Remediation Plan\n- Week 1: Fix all P0 issues\n- Week 2-3: Fix P1 issues\n- Week 4: Full retest\n- Ongoing: Add axe-core to CI pipeline\n\n"
             "Key learning: Automated tools catch ~30% of issues. Manual testing is essential."),

            ("User Research: Onboarding Flow",
             "Conducted 8 user interviews to evaluate our onboarding experience.\n\n"
             "## Method\n- 30-minute moderated sessions via Zoom\n- Think-aloud protocol while completing onboarding\n- Mix of technical and non-technical users\n\n"
             "## Key Findings\n1. **Confusion at Step 2** (6/8 users): API key setup was unclear\n2. **Dropped at Step 4** (3/8 users): Too many configuration options\n3. **Positive** (7/8): Quick Start template was very helpful\n4. **Unexpected** (4/8): Users wanted to see a populated demo first\n\n"
             "## Recommendations\n1. Add inline help tooltips at Step 2\n2. Progressive disclosure: hide advanced config behind 'Show more'\n3. Add 'Try demo first' option before signup\n4. Reduce onboarding from 5 steps to 3\n\n"
             "## Metrics Impact (Projected)\n- Completion rate: 58% -> 75% (estimated)\n- Time to first value: 15 min -> 5 min\n\n"
             "Scheduling follow-up usability test after implementing changes."),

            ("UI Patterns: Empty States and Error Handling",
             "Design guidelines for empty states and error handling across the product.\n\n"
             "## Empty States\nEvery empty state should have:\n1. **Illustration** -- friendly, not corporate\n2. **Headline** -- what this section is for\n3. **Description** -- why it's empty and what to do\n4. **CTA button** -- primary action to populate it\n\n"
             "## Error States\n| Type | Tone | Action |\n|------|------|--------|\n| Network error | Sympathetic | Retry button |\n| 404 Not Found | Helpful | Link to home + search |\n| Permission denied | Neutral | Request access link |\n| Server error (5xx) | Apologetic | Auto-retry + status page link |\n| Validation error | Instructive | Inline field hints |\n\n"
             "## Principles\n- Never show raw error codes to users\n- Always provide a next step\n- Maintain brand voice even in error states\n- Log technical details silently for debugging\n\n"
             "## Loading States\n- Skeleton screens over spinners (perceived performance)\n- Progressive loading for lists (show first items immediately)\n- Optimistic updates for user actions (undo instead of confirm)"),

            ("Figma Workflow and Component Organization",
             "How we organize our Figma workspace.\n\n"
             "## File Structure\n```\nProject/\n  Design System/\n    Tokens (colors, typography, spacing)\n    Components (all reusable components)\n    Patterns (common layouts)\n  Product/\n    v2.1 Feature A/\n    v2.2 Feature B/\n  Archive/\n    v1.x (old designs)\n```\n\n"
             "## Component Naming\n`Category/ComponentName/Variant/State`\n\n"
             "Example: `Forms/Input/Default/Focus`\n\n"
             "## Branch Strategy\n- Main branch = source of truth\n- Feature branches for new work\n- Design reviews before merge\n- Weekly cleanup of stale branches\n\n"
             "## Handoff Process\n1. Design in Figma with proper component variants\n2. Add specs and annotations\n3. Link to dev ticket\n4. Dev inspects in Figma (no manual redlines)\n5. Visual QA in Storybook\n\n"
             "## Plugins We Use\n- Stark (accessibility)\n- Content Reel (realistic data)\n- Autoflow (user flow diagrams)\n- Style Dictionary (token export)"),
        ],
    },

    "DevOps & SRE": {
        "count": 40,
        "types": ["worklog", "note", "decision", "task", "rfc"],
        "topics": [
            ("Incident Response Playbook",
             "Standardized incident response process for the team.\n\n"
             "## Severity Levels\n| Level | Criteria | Response Time | Comms |\n|-------|----------|---------------|-------|\n| SEV1 | Service down, data loss risk | 15 min | Page on-call + all-hands |\n| SEV2 | Major feature broken | 30 min | Page on-call + team lead |\n| SEV3 | Minor degradation | 4 hours | Slack alert |\n| SEV4 | Cosmetic/low impact | Next business day | Ticket |\n\n"
             "## Incident Flow\n```\nDetect -> Triage -> Mitigate -> Resolve -> Review\n```\n\n"
             "1. **Detect**: Alerts fire (PagerDuty) or user report\n2. **Triage**: Assign severity, open incident channel\n3. **Mitigate**: Reduce impact (rollback, scale, disable)\n4. **Resolve**: Root cause fix deployed and verified\n5. **Review**: Blameless postmortem within 48 hours\n\n"
             "## On-Call Toolkit\n- Grafana dashboards bookmarked\n- Runbook links in PagerDuty alerts\n- `ssh` access and credentials in 1Password vault\n- Rollback scripts tested monthly"),

            ("SLO Framework and Error Budgets",
             "Defining Service Level Objectives for our platform.\n\n"
             "## SLIs (Service Level Indicators)\n1. **Availability**: % of successful requests (non-5xx)\n2. **Latency**: p50, p95, p99 response times\n3. **Correctness**: % of requests returning correct data\n\n"
             "## SLOs\n| Service | Availability | Latency p99 | Error Budget (30d) |\n|---------|--------------|-------------|--------------------|\n| API | 99.9% | <500ms | 43 min downtime |\n| Search | 99.5% | <2s | 3.6 hrs downtime |\n| Webhooks | 99.0% | <10s | 7.2 hrs downtime |\n\n"
             "## Error Budget Policy\n- Budget > 50%: Normal development velocity\n- Budget 25-50%: Prioritize reliability work\n- Budget < 25%: Feature freeze, focus on stability\n- Budget exhausted: Halt all non-reliability changes\n\n"
             "## Burn Rate Alerts\n- Fast burn (14.4x): Page immediately (budget gone in 2 days)\n- Slow burn (3x): Ticket, review in standup\n\n"
             "Implemented using Prometheus recording rules + Grafana alerts."),

            ("Chaos Engineering: First Game Day",
             "Ran our first chaos engineering experiment in staging.\n\n"
             "## Experiment: Database Failover\n- **Hypothesis**: If primary PostgreSQL goes down, replica promotes within 30s and app recovers\n- **Method**: Kill primary container during normal load test\n- **Steady state**: 200 req/s, <100ms p99 latency\n\n"
             "## Results\n| Metric | Expected | Actual |\n|--------|----------|--------|\n| Failover time | <30s | 45s |\n| Error spike | <5% errors | 12% errors for 60s |\n| Recovery | Full in 60s | Full in 90s |\n\n"
             "## Issues Found\n1. Connection pool didn't reconnect automatically -- added retry logic\n2. Health check interval too slow (30s) -- reduced to 10s\n3. No circuit breaker on write path -- requests queued instead of failing fast\n\n"
             "## Action Items\n- [ ] Add connection pool retry with exponential backoff\n- [ ] Reduce health check interval to 10s\n- [ ] Implement circuit breaker on database writes\n- [ ] Schedule next game day: Redis failover\n\n"
             "Chaos engineering pays for itself. Found 3 issues that would have bitten us in production."),

            ("On-Call Rotation Setup",
             "Established on-call rotation for the engineering team.\n\n"
             "## Rotation Schedule\n- 1 week on-call per engineer\n- 5 engineers in rotation = on-call every 5 weeks\n- Handoff every Monday at 10:00 UTC\n- Shadow on-call for first rotation\n\n"
             "## Compensation\n- Flat weekly stipend for being on-call\n- 2x pay for hours actively responding\n- Comp day after SEV1/SEV2 incidents\n\n"
             "## Expectations\n- Acknowledge pages within 15 min\n- Laptop available (no remote hiking)\n- Sober enough to debug production\n- Escalate if unsure -- never hero-debug alone\n\n"
             "## Tooling\n- PagerDuty for alerting and escalation\n- Incident.io for incident management\n- Grafana for dashboards\n- Slack `#incidents` channel for coordination\n\n"
             "## Runbooks\nEvery alert links to a runbook with:\n1. What triggered the alert\n2. How to verify the issue\n3. Steps to mitigate\n4. When to escalate"),

            ("Postmortem: 2-Hour API Outage",
             "Blameless postmortem for the January 15 API outage.\n\n"
             "## Timeline\n- 14:30 -- Deploy of v2.4.1 to production\n- 14:35 -- Error rate spikes to 40%\n- 14:40 -- PagerDuty alert fires, on-call acknowledged\n- 14:55 -- Root cause identified: migration locked the users table\n- 15:10 -- Rollback deployed, migration cancelled\n- 15:15 -- Lock released, error rate dropping\n- 15:30 -- Full recovery confirmed\n- 16:30 -- Service stable, monitoring extended\n\n"
             "## Root Cause\nDatabase migration added a NOT NULL column with DEFAULT on the `users` table (5M rows).\n"
             "This acquired an ACCESS EXCLUSIVE lock for ~25 minutes, blocking all queries.\n\n"
             "## Contributing Factors\n- Migration not tested on production-size dataset\n- No lock timeout configured in migration tool\n- Deployment during peak traffic (14:30 local)\n\n"
             "## Action Items\n1. All migrations must be tested on prod-size dataset first\n2. Add `statement_timeout = 5s` to migration runner\n3. Deploy window moved to 06:00 UTC (low traffic)\n4. Add lock monitoring alert (lock wait > 10s)\n\n"
             "## Lessons Learned\n- ALTER TABLE with DEFAULT on large tables is dangerous in PostgreSQL < 11\n- We are on PostgreSQL 16 but the ORM generated old-style migration\n- Added `CREATE ... NOT NULL DEFAULT` check to CI linter"),
        ],
    },

    "Career & Management": {
        "count": 30,
        "types": ["note", "idea", "discussion", "decision", "rfc"],
        "topics": [
            ("1-on-1 Meeting Template and Best Practices",
             "How I run effective 1-on-1 meetings.\n\n"
             "## Structure (30 min)\n- 5 min: How are you doing? (genuine check-in)\n- 10 min: Their agenda (what they want to discuss)\n- 10 min: Feedback and coaching\n- 5 min: Action items and next steps\n\n"
             "## Questions I Rotate\n- What's the biggest blocker you're facing right now?\n- Is there anything you'd change about how the team works?\n- What have you learned recently that excited you?\n- Where do you want to be in 6 months?\n- Is there anything I can do better as your manager?\n\n"
             "## Anti-patterns to Avoid\n- Using 1-on-1s for status updates (that's what standups are for)\n- Cancelling regularly (signals they're not important)\n- Doing all the talking\n- Avoiding difficult conversations\n\n"
             "## Notes\n- Keep a shared doc for running notes\n- Track action items and follow up\n- Consistency > length: 30 min weekly beats 60 min monthly"),

            ("Hiring Process: Senior Engineer Role",
             "Our hiring pipeline for senior engineering positions.\n\n"
             "## Pipeline Stages\n1. **Resume screen** (2 min) - Does experience match?\n2. **Intro call** (30 min) - Culture fit, career goals, expectations\n3. **Technical screen** (60 min) - Coding problem + system design discussion\n4. **Take-home project** (4 hr max) - Real-world problem, no trick questions\n5. **On-site** (3 hrs) - Deep dive on take-home + team interviews\n6. **Offer** - Within 48 hours of on-site\n\n"
             "## What We Look For\n- Problem-solving over trivia\n- Communication and collaboration\n- System design thinking\n- Growth mindset and curiosity\n- Code quality: readable, tested, documented\n\n"
             "## What We Don't Do\n- Whiteboard algorithm puzzles\n- Gotcha questions\n- Unpaid trial periods\n- Ghost candidates (always respond)\n\n"
             "## Metrics\n- Time to offer: 2 weeks target\n- Offer acceptance rate: 85%\n- Pipeline-to-hire ratio: 1:15\n\n"
             "Key rule: Hire for potential, not just current skill."),

            ("Building Engineering Team Culture",
             "Notes on creating a healthy engineering culture.\n\n"
             "## Our Values\n1. **Ship incrementally**: Small PRs, frequent deploys, iterate fast\n2. **Own your code**: You build it, you run it, you fix it\n3. **Assume good intent**: Code review is about the code, not the person\n4. **Learn in public**: Share what you learned, ask questions openly\n5. **Sustainable pace**: No hero culture, no midnight deploys\n\n"
             "## Rituals\n- **Weekly demos**: Every Friday, show what you built\n- **Tech talks**: Monthly, anyone can present (15 min)\n- **Blameless postmortems**: After every incident\n- **Hack days**: One day per month for exploration\n\n"
             "## Code Review Culture\n- Approve with comments (don't block on style nits)\n- Use 'nit:' prefix for optional suggestions\n- Review within 4 hours during business hours\n- Celebrate good code, don't just point out problems\n\n"
             "## Onboarding New Engineers\n- Day 1: Ship a small PR (confidence boost)\n- Week 1: Pair with each team member once\n- Month 1: Own a small feature end-to-end\n- Assigned buddy for first 3 months"),

            ("Code Review Culture Guide",
             "How we do code reviews to maximize learning and minimize friction.\n\n"
             "## Principles\n1. Reviews are about shared understanding, not gatekeeping\n2. Praise good patterns, don't just critique\n3. Ask questions rather than make demands\n4. Prefer suggestions over prescriptions\n\n"
             "## Comment Conventions\n- `nit:` -- optional style suggestion, don't block merge\n- `question:` -- I don't understand this, please explain\n- `suggestion:` -- Consider this alternative approach\n- `issue:` -- This needs to change before merge\n- `praise:` -- This is really well done, highlighting for the team\n\n"
             "## SLA\n- Initial review within 4 business hours\n- PR size: max 400 lines (exceptions for generated code)\n- Stacked PRs for larger features\n- Author responds to all comments before merge\n\n"
             "## Anti-patterns\n- Style wars (use a formatter, enforce in CI)\n- Bikeshedding on naming (if it's clear enough, ship it)\n- Blocking on personal preference\n- Drive-by 'LGTM' without reading"),

            ("Tech Leadership: Staff Engineer Path",
             "Notes on growing into a staff+ engineering role.\n\n"
             "## Staff Engineer Archetypes (Will Larson)\n1. **Tech Lead**: Partner with a manager, guide a team\n2. **Architect**: High-level technical vision and direction\n3. **Solver**: Deep-dive into the hardest problems\n4. **Right Hand**: Extension of VP Eng, operate at org level\n\n"
             "## Key Competencies\n- **Technical**: Deep expertise + broad awareness\n- **Influence**: Lead without authority, build consensus\n- **Communication**: Write clearly, present to non-technical audience\n- **Judgment**: When to build, buy, or wait\n- **Mentorship**: Multiply yourself through others\n\n"
             "## How to Get There\n1. Take on ambiguous problems with org-wide impact\n2. Write design docs that get adopted across teams\n3. Mentor 2-3 engineers consistently\n4. Build relationships outside your immediate team\n5. Develop a 'point of view' on technical direction\n\n"
             "## Books\n- 'Staff Engineer' by Will Larson\n- 'The Staff Engineer's Path' by Tanya Reilly\n- 'An Elegant Puzzle' by Will Larson\n\n"
             "Key insight: Staff engineering is about creating leverage, not writing more code."),
        ],
    },
}

# ---------------------------------------------------------------------------
# Content variations - additional templates for generating more documents
# ---------------------------------------------------------------------------

VARIATION_TEMPLATES = {
    "Software Development": [
        ("Implementing {feature} with {tech}",
         "## Overview\nImplemented {feature} using {tech}.\n\n"
         "## Approach\n- Started with a minimal proof of concept\n- Iterated based on feedback from the team\n- Added comprehensive tests\n\n"
         "## Challenges\n- Integration with existing codebase required refactoring\n- Performance tuning took longer than expected\n- Had to handle edge cases in data validation\n\n"
         "## Results\n- Feature deployed to production\n- No regressions in existing functionality\n- Response time within acceptable limits\n\n"
         "## Next Steps\n- Monitor for edge cases in production\n- Gather user feedback\n- Consider extending with {related_feature}"),
        ("Bug Fix: {bug_description}",
         "## Issue\n{bug_description}\n\n"
         "## Investigation\n1. Reproduced in staging environment\n2. Traced through logs to identify the failing component\n3. Found root cause in {component}\n\n"
         "## Root Cause\nThe {component} was not handling {edge_case} correctly. "
         "When {condition}, the code path skipped validation.\n\n"
         "## Fix\n- Added proper null check before processing\n- Added guard clause for boundary conditions\n- Updated error handling to provide better messages\n\n"
         "## Testing\n- Added unit test covering the specific scenario\n- Verified fix in staging\n- Monitored production for 24 hours after deploy"),
    ],
    "Infrastructure": [
        ("Server Migration: {service}",
         "## Objective\nMigrate {service} to new infrastructure.\n\n"
         "## Steps Taken\n1. Provisioned new server with required specs\n2. Installed dependencies and configured services\n3. Migrated data with zero downtime approach\n4. Updated DNS and verified connectivity\n5. Monitored for 48 hours\n\n"
         "## Outcome\n- Migration completed successfully\n- No data loss\n- Performance improved by ~30%\n- Old server decommissioned after 1 week"),
        ("Scaling {service} for High Availability",
         "## Objective\nScale {service} to handle 10x current load.\n\n"
         "## Changes\n- Added read replicas for database queries\n- Configured connection pooling with PgBouncer\n- Implemented horizontal scaling with load balancer\n- Added health checks and auto-restart policies\n\n"
         "## Results\n- Sustained 2000 req/s (was 200 req/s)\n- p99 latency under 100ms\n- Zero downtime during scaling events"),
    ],
    "AI & Machine Learning": [
        ("Experiment: {experiment_name}",
         "## Hypothesis\n{hypothesis}\n\n"
         "## Setup\n- Dataset: {dataset_description}\n- Model: {model_name}\n- Metrics: accuracy, F1 score, latency\n\n"
         "## Results\n| Variant | Accuracy | F1 | Latency |\n|---------|----------|-----|--------|\n| Baseline | 0.82 | 0.79 | 45ms |\n| New approach | 0.87 | 0.85 | 52ms |\n\n"
         "## Conclusion\nNew approach shows promising improvement. "
         "Latency increase is acceptable for the quality gain.\n"
         "Will proceed with A/B test on 10% of traffic."),
        ("Evaluating {model_name} for {experiment_name}",
         "## Goal\nTest whether {model_name} can improve our {experiment_name} pipeline.\n\n"
         "## Methodology\n- Ran on same evaluation dataset (500 samples)\n- Compared against current production model\n- Measured latency, throughput, and quality metrics\n\n"
         "## Key Findings\n- Quality improved by ~8% on average\n- Inference latency within acceptable range\n- Memory footprint manageable in production\n\n"
         "## Recommendation\nProceed with staged rollout. Monitor quality metrics for 1 week before full switch."),
    ],
    "Personal Notes": [
        ("Daily Reflection: {date_str}",
         "## Today\n- Worked on {task1}\n- Had a good conversation about {topic}\n- Learned something new about {learning}\n\n"
         "## Mood\nGenerally positive. Energy was good in the morning, dipped after lunch.\n\n"
         "## Tomorrow\n- Continue with {task2}\n- Block time for deep work in the morning\n- Remember to take breaks"),
    ],
    "Books & Reading": [
        ("Book Notes: {book_title}",
         "## Key Ideas\n- The main thesis is about building systems that last\n- Focus on fundamentals over trends\n- Practical examples from real-world projects\n\n"
         "## Favorite Quotes\n> \"The best code is no code at all.\"\n\n"
         "## My Takeaways\n1. Apply the 80/20 rule to learning\n2. Build small projects to internalize concepts\n3. Revisit fundamentals periodically\n\n"
         "## Rating: 4/5\nWorth reading for anyone working in technology."),
    ],
    "News & Articles": [
        ("Article: {article_topic}",
         "## Summary\nInteresting article about {article_topic} and its implications.\n\n"
         "## Key Points\n- The industry is moving toward this approach\n- Several large companies have adopted it successfully\n- Open-source tooling is maturing rapidly\n\n"
         "## My Thoughts\n- Relevant to what we're building\n- Worth experimenting with in a side project\n- Could simplify our architecture if adopted"),
    ],
    "Travel & Life": [
        ("Trip Notes: {city}",
         "## Highlights\n- Beautiful architecture and great food scene\n- Public transport was excellent and affordable\n- Found some amazing local restaurants\n\n"
         "## Food Recommendations\n- Local market was incredible for lunch\n- Coffee culture is strong here\n- Street food is worth trying\n\n"
         "## Practical Tips\n- Best to visit in shoulder season\n- Cash useful for smaller shops\n- Walking is the best way to explore the center\n\n"
         "## Budget\n- Accommodation: moderate\n- Food: affordable\n- Overall: great value for money"),
    ],
    "Finance & Business": [
        ("Financial Analysis: {finance_topic}",
         "## Overview\nReview of {finance_topic} performance and outlook.\n\n"
         "## Key Metrics\n- Revenue growth on track\n- Operating costs within budget\n- Customer acquisition cost improving\n\n"
         "## Risks\n- Market conditions uncertain\n- Competition increasing\n- Need to diversify revenue streams\n\n"
         "## Action Items\n- Review pricing strategy\n- Explore new market segments\n- Reduce operational overhead where possible"),
    ],
    "Health & Productivity": [
        ("Productivity Experiment: {productivity_method}",
         "## Setup\nTried {productivity_method} for two weeks.\n\n"
         "## What Worked\n- Better focus during deep work sessions\n- More intentional about task prioritization\n- Reduced context switching\n\n"
         "## What Didn't Work\n- Hard to maintain on meeting-heavy days\n- Requires discipline in the first few days\n\n"
         "## Verdict\nWill continue using it with slight modifications for my schedule."),
    ],
    "Research": [
        ("Benchmarking {tech} vs {alt_tech}",
         "## Setup\n- Tested both under identical conditions\n- 10K concurrent users simulation\n- Measured throughput, latency, resource usage\n\n"
         "## Results\n| Metric | {tech} | {alt_tech} |\n|--------|--------|--------|\n| Throughput | High | Medium |\n| Latency p99 | 45ms | 120ms |\n| Memory | 500MB | 1.2GB |\n\n"
         "## Conclusion\n{tech} is the better fit for our use case based on throughput and memory efficiency."),
    ],
    "Product Management": [
        ("Feature Proposal: {feature}",
         "## Problem\nUsers have been requesting better {feature} support.\n\n"
         "## Proposed Solution\n- Implement {feature} with progressive rollout\n- Start with core functionality, iterate based on feedback\n- Target completion: next quarter\n\n"
         "## Success Metrics\n- Adoption rate > 30% within first month\n- User satisfaction score > 4.0/5\n- No increase in support tickets\n\n"
         "## Effort Estimate\n- 2 engineers, 3 weeks\n- Design: 1 week\n- Implementation: 2 weeks\n- QA: 1 week (overlapping)"),
    ],
    "Design & UX": [
        ("Design Review: {feature} Component",
         "## Context\nReviewing the design for the new {feature} component.\n\n"
         "## Feedback\n- Layout is clean and consistent with design system\n- Consider adding a loading state for slow connections\n- Color contrast needs checking for accessibility\n- Mobile breakpoint could use refinement\n\n"
         "## Decision\nApproved with minor revisions. Designer will update by end of week."),
    ],
    "DevOps & SRE": [
        ("Alert Tuning: Reducing Noise for {service}",
         "## Problem\n{service} generating too many false positive alerts.\n\n"
         "## Changes\n- Increased threshold from 80% to 90% for CPU alerts\n- Added 5-minute sustained condition (was instant)\n- Grouped related alerts to reduce notification spam\n- Added business-hours-only for non-critical alerts\n\n"
         "## Results\n- Alert volume reduced by 60%\n- On-call acknowledgment time improved\n- Zero missed real incidents since tuning"),
    ],
    "Career & Management": [
        ("Meeting Notes: {meeting_topic}",
         "## Attendees\nEngineering leads, product manager, design lead\n\n"
         "## Agenda\n- Discuss {meeting_topic}\n- Align on priorities for next sprint\n- Address open questions from last week\n\n"
         "## Key Decisions\n- Moving forward with the proposed approach\n- Will allocate dedicated time for tech debt\n- Next review scheduled for two weeks\n\n"
         "## Action Items\n- [ ] Draft proposal by Friday\n- [ ] Schedule follow-up with stakeholders\n- [ ] Update roadmap document"),
    ],
    "Learning": [
        ("TIL: {learning} Deep Dive",
         "## What I Learned\nSpent time understanding {learning} in depth.\n\n"
         "## Key Concepts\n- The underlying mechanism is simpler than expected\n- Edge cases to watch out for in production\n- Performance characteristics worth knowing\n\n"
         "## Practical Example\nApplied this in a side project and saw immediate improvement.\n\n"
         "## Resources\n- Official documentation was the best source\n- Found a great blog post with real-world examples\n- Will add to team knowledge base"),
    ],
}

FEATURES = ["caching layer", "search autocomplete", "file upload", "webhook system",
            "notification service", "batch processing", "data export", "user permissions",
            "audit logging", "API versioning", "health checks", "retry mechanism",
            "dark mode", "SSO integration", "real-time sync", "bulk import",
            "two-factor auth", "email templates", "activity feed", "bookmark system"]

TECHS = ["Redis", "FastAPI", "React", "PostgreSQL", "Docker", "WebSocket",
         "GraphQL", "gRPC", "Celery", "SQLAlchemy", "Next.js", "Rust",
         "TypeScript", "Kafka", "Svelte", "Tailwind CSS"]

BUG_DESCRIPTIONS = [
    "Login fails intermittently for users with special characters in password",
    "Pagination returns duplicate items when sorting by created_at",
    "File upload timeout on files larger than 10MB",
    "Search results not updated after document deletion",
    "Memory spike when processing large CSV imports",
    "Race condition in concurrent document updates",
    "Incorrect timezone conversion for users in UTC+offset zones",
    "WebSocket connection drops every 60 seconds behind load balancer",
    "Session token not invalidated after password change",
    "Infinite scroll fetches same page repeatedly on slow networks",
    "Email notifications sent twice due to retry without idempotency key",
    "Dashboard chart renders empty when dataset has single data point",
    "Export fails silently for documents with emoji in title",
    "API returns 500 instead of 422 for malformed JSON body",
]

SERVICES = ["PostgreSQL cluster", "Redis cache", "API gateway", "monitoring stack",
            "CI/CD pipeline", "backup system", "log aggregation", "DNS services",
            "message queue", "object storage", "search index", "auth service"]

EXPERIMENTS = ["context window optimization", "hybrid search scoring", "embedding quantization",
               "batch inference pipeline", "few-shot classification", "prompt compression",
               "multi-modal retrieval", "document chunking strategy", "reranking pipeline",
               "sparse-dense hybrid search"]

COMPONENTS = ["auth module", "search indexer", "event bus", "cache layer", "API router",
              "queue worker", "rate limiter", "session manager", "webhook dispatcher"]
EDGE_CASES = ["null input", "concurrent access", "unicode characters", "empty response",
              "timeout", "duplicate request", "oversized payload", "stale cache"]

TASKS = ["the auth refactor", "API documentation", "test coverage improvement",
         "performance optimization", "code review backlog", "infrastructure updates",
         "onboarding flow redesign", "search relevance tuning", "CI pipeline speed-up"]

TOPICS_GENERAL = ["system design", "team processes", "code quality", "user experience",
                  "scalability", "documentation practices", "developer experience",
                  "incident response", "technical debt", "API design"]

LEARNINGS = ["async/await patterns", "database indexing", "CSS grid layout",
             "Linux networking", "Git internals", "HTTP caching headers",
             "WebAssembly basics", "Rust lifetimes", "container networking",
             "PostgreSQL JSONB queries", "OAuth2 flows", "DNS resolution"]

BOOK_TITLES = [
    "The Art of Readable Code", "Refactoring by Martin Fowler",
    "Domain-Driven Design", "Release It!", "Site Reliability Engineering",
    "Building Microservices", "Accelerate", "Team Topologies",
    "The Manager's Path", "Shape Up by Basecamp",
]

ARTICLE_TOPICS = [
    "edge computing and serverless", "developer productivity metrics",
    "the future of container orchestration", "TypeScript 6 features",
    "sustainable software engineering", "platform engineering trends",
    "AI-assisted code review", "database per service pattern",
    "progressive web apps in 2026", "zero-trust networking",
]

CITIES = ["Berlin", "Tokyo", "Barcelona", "Prague", "Amsterdam",
          "Seoul", "Vienna", "Copenhagen", "Montreal", "Melbourne"]

FINANCE_TOPICS = [
    "Q4 revenue performance", "subscription tier optimization",
    "infrastructure cost reduction", "annual budget planning",
    "competitive pricing analysis", "unit economics review",
]

PRODUCTIVITY_METHODS = [
    "time blocking", "the Eisenhower matrix", "weekly themes",
    "two-day rule for habits", "the 5-hour rule", "energy management",
]

MEETING_TOPICS = [
    "Q2 hiring plan", "tech debt prioritization", "team restructuring",
    "cross-team collaboration", "annual review calibration",
    "engineering ladder update",
]

ALT_TECHS = ["MySQL", "MongoDB", "DynamoDB", "CockroachDB", "SQLite",
             "Valkey", "Memcached", "RabbitMQ", "NATS", "Pulsar"]


def generate_date(months_back: int = 6) -> str:
    """Generate a random date within the last N months, biased toward recent."""
    now = datetime.now()
    # Bias toward recent dates: exponential distribution
    days_back = int(random.expovariate(1 / 30) % (months_back * 30))
    dt = now - timedelta(days=days_back)
    return dt.strftime("%Y-%m-%d")


def generate_created_at(date_str: str) -> str:
    """Generate an ISO datetime from a date string."""
    hour = random.randint(7, 22)
    minute = random.randint(0, 59)
    return f"{date_str}T{hour:02d}:{minute:02d}:00Z"


def fill_template(template: str, **kwargs) -> str:
    """Fill template with values, ignoring missing keys."""
    result = template
    for k, v in kwargs.items():
        result = result.replace(f"{{{k}}}", str(v))
    return result


def generate_documents(target_count: int = 1000) -> list:
    """Generate a list of demo documents."""
    docs = []

    # Phase 1: Use all hand-written topics
    for category, info in CATEGORIES.items():
        for title, content in info["topics"]:
            doc_type = random.choice(info["types"])
            date_str = generate_date()
            full_content = f"# {title}\n\n{content}"
            tags = [f"type:{doc_type}", f"date:{date_str}"]
            docs.append({
                "content": full_content,
                "tags": tags,
                "source": "demo",
                "created_at": generate_created_at(date_str),
            })

    # Phase 2: Generate variations to reach target count (with dedup)
    import hashlib
    seen_hashes = {hashlib.md5(d["content"].encode()).hexdigest() for d in docs}
    max_attempts = target_count * 5  # avoid infinite loop
    attempts = 0
    while len(docs) < target_count and attempts < max_attempts:
        attempts += 1
        category = random.choice(list(CATEGORIES.keys()))
        info = CATEGORIES[category]
        doc_type = random.choice(info["types"])
        date_str = generate_date()

        # Either use a variation template or remix an existing topic
        if category in VARIATION_TEMPLATES and random.random() < 0.6:
            tmpl_title, tmpl_content = random.choice(VARIATION_TEMPLATES[category])
            kwargs = dict(
                feature=random.choice(FEATURES),
                tech=random.choice(TECHS),
                alt_tech=random.choice(ALT_TECHS),
                related_feature=random.choice(FEATURES),
                bug_description=random.choice(BUG_DESCRIPTIONS),
                component=random.choice(COMPONENTS),
                edge_case=random.choice(EDGE_CASES),
                condition="the input is null or empty",
                service=random.choice(SERVICES),
                experiment_name=random.choice(EXPERIMENTS),
                hypothesis="Changing the approach will improve metrics",
                dataset_description="10K labeled samples",
                model_name="fine-tuned transformer",
                date_str=date_str,
                task1=random.choice(TASKS),
                task2=random.choice(TASKS),
                topic=random.choice(TOPICS_GENERAL),
                learning=random.choice(LEARNINGS),
                book_title=random.choice(BOOK_TITLES),
                article_topic=random.choice(ARTICLE_TOPICS),
                city=random.choice(CITIES),
                finance_topic=random.choice(FINANCE_TOPICS),
                productivity_method=random.choice(PRODUCTIVITY_METHODS),
                meeting_topic=random.choice(MEETING_TOPICS),
            )
            title = fill_template(tmpl_title, **kwargs)
            content = fill_template(tmpl_content, **kwargs)
        else:
            # Pick a random existing topic and remix it with unique details
            orig_title, orig_content = random.choice(info["topics"])
            followup_types = [
                "Follow-up", "Revisited", "Part 2", "Continued",
                "Deep Dive", "Update", "Lessons Learned", "Retrospective",
            ]
            observations = [
                "Implementation went smoother than expected",
                "Performance benchmarks exceeded targets by 15%",
                "Discovered a subtle race condition during load testing",
                "Team adopted the pattern across 3 other services",
                "Reduced memory usage by 40% after profiling",
                "Edge case handling needed more work than anticipated",
                "Documentation was lacking, spent time writing guides",
                "Monitoring revealed unexpected traffic patterns",
                "Rollback plan proved essential during deployment",
                "Integration tests caught 2 regressions early",
                "Customer feedback validated our approach",
                "Found a simpler solution after discussing with the team",
                "Latency dropped from 200ms to 45ms with caching",
                "Had to revert and try a different strategy",
                "Cross-team collaboration improved the final design",
                "Security audit required additional changes",
                "Load testing showed we need horizontal scaling at 5K QPS",
                "Migrated 2M records without downtime",
            ]
            picked = random.sample(observations, min(4, len(observations)))
            title = f"{orig_title} - {random.choice(followup_types)}"
            content = (
                f"Follow-up notes on the previous entry.\n\n"
                f"## Update ({date_str})\n"
                f"After revisiting this topic, here are additional observations:\n\n"
                + "".join(f"- {obs}\n" for obs in picked) +
                f"\n## Next Steps\n"
                f"- {random.choice(TASKS) if 'TASKS' in dir() else 'Continue iterating'}\n"
                f"- Review in {random.choice([1,2,3,4])} weeks\n\n"
                f"## Related\nSee the original notes for full context."
            )

        full_content = f"# {title}\n\n{content}"
        content_hash = hashlib.md5(full_content.encode()).hexdigest()
        if content_hash in seen_hashes:
            continue  # skip duplicate
        seen_hashes.add(content_hash)
        tags = [f"type:{doc_type}", f"date:{date_str}"]
        docs.append({
            "content": full_content,
            "tags": tags,
            "source": "demo",
            "created_at": generate_created_at(date_str),
        })

    return docs[:target_count]


def load_documents(api_url: str, docs: list, batch_size: int = 100) -> None:
    """Load documents into Mesh via PUT /bulk endpoint."""
    total = len(docs)
    loaded = 0
    errors = 0

    for i in range(0, total, batch_size):
        batch = docs[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} docs)...", end=" ", flush=True)

        try:
            resp = requests.put(
                f"{api_url.rstrip('/')}/bulk",
                json=batch,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                created = data.get("created", len(batch))
                skipped = data.get("skipped", 0)
                loaded += created
                print(f"OK (created: {created}, skipped: {skipped})")
            else:
                errors += len(batch)
                print(f"ERROR {resp.status_code}: {resp.text[:200]}")
        except requests.exceptions.RequestException as e:
            errors += len(batch)
            print(f"ERROR: {e}")

        # Small delay between batches
        if i + batch_size < total:
            time.sleep(0.5)

    print(f"\nDone: {loaded} created, {errors} errors out of {total} total")


def check_api(api_url: str) -> bool:
    """Check if the API is reachable."""
    try:
        resp = requests.get(f"{api_url.rstrip('/')}/health", timeout=10)
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


def get_stats(api_url: str) -> Optional[dict]:
    """Get API stats."""
    try:
        resp = requests.get(f"{api_url.rstrip('/')}/stats", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.exceptions.RequestException:
        pass
    return None


def enrich_tags(docs: list) -> None:
    """Enrich documents with category/topic/project/status tags inline.

    Imports classification rules from enrich_demo_tags.py and applies them
    to each document so the JSONL output is self-contained.
    """
    # Import classify_document from sibling script
    script_dir = Path(__file__).parent
    enrich_path = script_dir / "enrich_demo_tags.py"
    if not enrich_path.exists():
        print("  [WARN] enrich_demo_tags.py not found, skipping enrichment")
        return

    import importlib.util
    spec = importlib.util.spec_from_file_location("enrich_demo_tags", enrich_path)
    enrich_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(enrich_mod)
    classify_document = enrich_mod.classify_document

    enriched = 0
    for doc in docs:
        new_tags = classify_document(doc["content"], doc["tags"])
        if new_tags:
            doc["tags"].extend(new_tags)
            enriched += 1

    print(f"  Enriched {enriched}/{len(docs)} documents with category/topic tags")


def main():
    parser = argparse.ArgumentParser(description="Generate demo data for Mesh")
    parser.add_argument("--api", default="http://localhost:8001", help="Mesh API URL")
    parser.add_argument("--count", type=int, default=500, help="Number of documents to generate")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for bulk upload")
    parser.add_argument("--dry-run", action="store_true", help="Generate but don't upload")
    parser.add_argument("--output", type=str, default=None,
                        help="Write documents to JSONL file instead of uploading to API")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for deterministic output")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    print(f"Mesh Demo Data Generator")
    if args.output:
        print(f"  Output: {args.output}")
    else:
        print(f"  API: {args.api}")
    print(f"  Target: {args.count} documents")
    if args.seed is not None:
        print(f"  Seed: {args.seed}")
    print()

    # Check API availability (only when uploading)
    if not args.dry_run and not args.output:
        print("Checking API...", end=" ", flush=True)
        if not check_api(args.api):
            print("FAILED")
            print(f"Cannot reach {args.api}. Is the demo instance running?")
            sys.exit(1)
        print("OK")

        # Show current stats
        stats = get_stats(args.api)
        if stats:
            doc_count = stats.get("total_documents", stats.get("documents", 0))
            print(f"Current documents: {doc_count}")
        print()

    # Generate documents
    print(f"Generating {args.count} documents...")
    docs = generate_documents(args.count)

    # Enrich with tags
    print("Enriching tags...")
    enrich_tags(docs)

    # Show distribution
    type_counts: dict = {}
    cat_counts: dict = {}
    for doc in docs:
        for tag in doc["tags"]:
            if tag.startswith("type:"):
                t = tag.split(":")[1]
                type_counts[t] = type_counts.get(t, 0) + 1
            if tag.startswith("category:"):
                c = tag.split(":", 1)[1]
                cat_counts[c] = cat_counts.get(c, 0) + 1
    print(f"  Distribution by type:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t}: {c}")
    if cat_counts:
        print(f"  Distribution by category:")
        for t, c in sorted(cat_counts.items(), key=lambda x: -x[1]):
            print(f"    {t}: {c}")
    print()

    # Write to JSONL file
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            for doc in docs:
                f.write(json.dumps(doc, default=str) + "\n")
        print(f"Written {len(docs)} documents to {args.output}")
        return

    if args.dry_run:
        print("Dry run - not uploading. Sample document:")
        print(json.dumps(docs[0], indent=2, default=str))
        return

    # Load documents
    print("Loading documents...")
    load_documents(args.api, docs, args.batch_size)

    # Final stats
    print()
    print("Waiting for stats update...", flush=True)
    time.sleep(2)
    stats = get_stats(args.api)
    if stats:
        doc_count = stats.get("total_documents", stats.get("documents", 0))
        queue = stats.get("embedding_queue_size", stats.get("queue_size", "?"))
        print(f"Documents: {doc_count}")
        print(f"Embedding queue: {queue}")
        print()
        print("Embeddings will be processed in the background.")
        print(f"Monitor progress: curl {args.api}/stats")


if __name__ == "__main__":
    main()
