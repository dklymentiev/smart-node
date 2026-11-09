# DOCI — Operations Runbook

How to deploy, monitor, and recover a running DOCI instance.
Companion to [`RELEASING.md`](../RELEASING.md) (release process),
[`03-architecture.md`](03-architecture.md) (topology), and
[`06-testing-strategy.md`](06-testing-strategy.md) (Live Benchmark Gate
that runs before every deploy).

## Topology snapshot

Single-host docker compose. Two compose files are shipped:

| File | Use | Postgres |
|------|-----|----------|
| `docker-compose.dev.yml` | Local trial, integration tests | Embedded `postgres:16-alpine` on host port 5433 |
| `docker-compose.yml` | Production | **External** — expects a `postgres` container reachable on the `web` network (rename to your proxy network) |

Container layout in production:

| Container | Image | Purpose | Health |
|-----------|-------|---------|--------|
| `doci` | `doci:latest` (built locally) | Apache 2.4 + PHP 8.2, code baked in | `curl /api/health.php` every 30s |
| _external_ | `postgres:16` (managed elsewhere) | Metadata DB | Managed by its owner |

Bind mounts (host → container):
- `./files:/var/www/html/files` — document storage + git working tree.
  This is the only persistent application state. **Back this up.**
- `./.git:/var/www/html/.git:ro` — repo metadata, read-only in
  container. Lets the UI show the version it's running.
- Named volume `doci-logs:/var/log/doci` — app log stream.

Code is **baked into the image** (Dockerfile `COPY . .`). To change
code in production you rebuild the image. There is no bind mount for
PHP source — bind-mounting source over the baked code works for dev
but is not supported in prod.

## Deploy

### Dev (local trial)

```bash
docker compose -f docker-compose.dev.yml up -d
# UI on http://localhost:8080, Postgres on host 5433
# DOCI_ENV=development + DOCI_DEV_AUTO_AUTH=true auto-authenticate as user "dev"
```

The dev compose builds the image, starts an embedded Postgres,
applies migrations via `/docker-entrypoint-initdb.d`, seeds whatever
is in `./files/` and indexes it.

### Production

Pre-flight, once per host:

- [ ] Docker 24+ and docker compose v2 installed.
- [ ] An external Postgres 14+ reachable on a docker network named
  `web` (or rename the `web` network in `docker-compose.yml` to
  match your proxy's existing network). DB `doci`, user `doci_app`,
  password set.
- [ ] Traefik (or another reverse proxy) on the same `web`
  network, configured to issue TLS for `DOCI_DOMAIN`.
- [ ] A forward-auth middleware (e.g. Authum) that sets
  `Remote-User` for authenticated browser requests. API-key callers
  bypass this.
- [ ] `.env` file at the project root with at minimum:
  - `DOCI_DB_PASS`
  - `DOCI_API_KEY_HASH` (SHA-256 of the secret you'll hand to agents)
  - `DOCI_DOMAIN`
  - `APP_URL`, `AUTH_URL`

Deploy:

```bash
docker compose \
  --project-directory /srv/doci \
  -f docker-compose.yml \
  up -d --build doci
```

The `--project-directory` flag is **mandatory in prod**. Running
`docker compose up -d` from another working directory drops the
Traefik labels and the container detaches from the reverse proxy.
Always use the absolute path.

### What happens on container start

The entrypoint (`scripts/docker-entrypoint.sh`) does:

1. Fail-fast if `DOCI_DOMAIN` is unset (it's used as the git author
   domain on every commit).
2. Initialise `files/` as a git repo on first run; commit any
   pre-seeded content as `Initial DOCI content`.
3. On subsequent runs, if the host bind-mounted new files into
   `files/` between starts, commit them as `Sync seed content on
   container start`.
4. Background task (after a 5s sleep so Apache doesn't wait on it):
   - Apply migrations from `migrations/*.sql` via PDO. Each migration
     uses `IF NOT EXISTS`, so the loop is idempotent across restarts.
   - Run `scripts/index-documents.php` to register any markdown/HTML
     file that has no DB row yet.
   - Run `scripts/normalize-links.php` to rewrite relative links into
     stable GUID URLs; commit as `Normalise internal links to GUID form`.
5. `exec docker-php-entrypoint` — hand off to the upstream `php:8.2-apache`
   entrypoint, which starts Apache in the foreground.

A clean start log ends with Apache `child workers ready` within ~10s
of `[doci-entrypoint]` lines. The healthcheck (`/api/health.php`)
passes by then.

### Verifying deploy

```bash
docker exec doci cat /var/www/html/VERSION              # match expected version
curl -s https://${DOCI_DOMAIN}/api/health.php | jq .    # {"status":"ok", ...}
docker logs --tail 50 doci | grep -iE 'error|fatal'     # zero matches expected
```

Then run the Live Benchmark per
[`06-testing-strategy.md`](06-testing-strategy.md) before declaring
the deploy complete.

### Rollback

Tags are immutable. Rollback = re-deploy the previous image tag.

```bash
git checkout v0.1.0
docker compose \
  --project-directory /srv/doci \
  -f docker-compose.yml \
  up -d --build doci
```

If the new release applied a migration that the previous code cannot
read (new NOT NULL column, dropped table), rollback is **not** safe —
escalate. Migrations in DOCI are forward-only by convention; see
**Migrations** below.

**Rule:** if rollback does not resolve the issue within 15 minutes,
escalate to a full incident.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `DB_HOST` | yes | `postgres` | Postgres host (matches the production compose service name) |
| `DB_PORT` | no | `5432` | Postgres port |
| `DB_NAME` | no | `doci` | Database name |
| `DB_USER` | no | `doci_app` | DB user |
| `DB_PASS` | **yes** | — | DB password (fail-fast at boot if unset) |
| `DOCI_DOMAIN` | **yes** | — | Domain used in git author email + Traefik routing |
| `DOCI_API_KEY_HASH` | recommended | empty | SHA-256 hash of the API key. Empty → API-key auth disabled |
| `APP_URL` | recommended | `https://doci.example.com` | Public base URL |
| `AUTH_URL` | no | empty | Forward-auth logout endpoint |
| `AI_GATEWAY_URL` | no | empty | OpenAI-compatible `/chat/completions` endpoint. Empty → AI thread replies disabled |
| `AI_GATEWAY_SSL_VERIFY` | no | `true` | Set to `false` only for self-signed dev gateways |
| `DOCI_ENV` | no | `production` | `development` is the only value that allows `DOCI_DEV_AUTO_AUTH=true` to take effect |
| `DOCI_DEV_AUTO_AUTH` | no | `false` | `true` auto-authenticates browser requests as `dev`; requires `DOCI_ENV=development` (else 500) |
| `DOCI_TRUSTED_PROXIES` | **yes (in production)** | empty | Comma-separated IPv4 / CIDR list. Only requests from these source IPs may set `Remote-User`. Use the literal `none` to declare "API-key only" intentionally. Empty or `0.0.0.0/0` with `DOCI_ENV=production` is a fatal startup error |
| `DOCI_LOG_LEVEL` | no | `WARN` | Log threshold: `ERROR` &lt; `WARN` &lt; `INFO` &lt; `DEBUG`. Security-relevant actions (auth, CSRF, deletes, API-key usage) log regardless of level |
| `TZ` | no | `UTC` | Container timezone |
| `EXTERNAL_SCRIPTS_URL` | no | empty | CDN for shared JS, if any |

**Rule:** never commit secrets. `.env` lives outside git
(`chmod 600`). Use a secrets manager for shared deploys.

**Hashing the API key:**
```bash
KEY=$(openssl rand -hex 32)            # the secret you'll hand to agents
HASH=$(echo -n "$KEY" | sha256sum | cut -d' ' -f1)
echo "DOCI_API_KEY_HASH=$HASH" >> .env
```

## Monitoring

### Health checks

| Endpoint | Method | Expected | Interval | Alert |
|---|---|---|---|---|
| `/api/health.php` | GET | 200 + `{"status":"ok"}` | 30s | 3 consecutive failures |

The Dockerfile sets this as `HEALTHCHECK` so `docker ps` shows
container health directly. The endpoint probes DB connectivity and
`files/` writability.

### Key signals

| Signal | Source | Investigate when |
|---|---|---|
| `/api/health.php` returns 503 | Healthcheck or Traefik | Immediately — DB down or `files/` not writable |
| Apache 5xx rate | nginx in front, or `docker logs doci` | > 1% over 5 min |
| `doci-logs` volume contains `ERROR` lines | App log (`/var/log/doci/app.log`) | Any new ERROR |
| Disk usage on `files/` host volume | host monitoring | > 75% — large versioned dirs can grow |
| `documents` table row count drift vs file count | `index-documents.php` re-run | When file count > row count by > 10 |

DOCI does not ship a Prometheus exporter or a built-in dashboard.
Wire whatever monitoring the host already uses.

## Logs

| Stream | Location | What |
|--------|----------|------|
| Apache access | `docker logs doci` | Every HTTP hit |
| Apache error | `docker logs doci` | 5xx, PHP fatal errors |
| App log | `/var/log/doci/app.log` (named volume `doci-logs`) | DOCI's own structured log (JSON lines); chattiness controlled by `DOCI_LOG_LEVEL`, security events always |
| Git commits | `files/.git/` (`git log`) | Every document mutation |

Tail the app log:
```bash
docker exec doci tail -f /var/log/doci/app.log
```

The app log is JSON-per-line: `{ts, rid, level, user, uid, ip, action, data}`.
`rid` is a per-request id — useful for correlating multiple log lines
from one request.

## Migrations

Migrations live in `migrations/NNN_<name>.sql` and are applied two ways:

1. **First start of dev Postgres:** `migrations/` is mounted to
   `/docker-entrypoint-initdb.d` in `docker-compose.dev.yml`. Postgres
   runs them once at first container init.
2. **Every container start of the app:** the entrypoint loops over
   `migrations/*.sql` and applies them via PDO. Each statement uses
   `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, so
   re-applying is a no-op.

This dual mechanism means production (external Postgres, no
`docker-entrypoint-initdb.d`) still picks up new migrations whenever
the app container restarts.

### Verifying a migration applied

```bash
docker exec doci php -r '
  require "/var/www/html/src/config.php";
  $r = get_db()->query("SELECT table_name FROM information_schema.tables
                         WHERE table_schema='\''public'\''")->fetchAll(PDO::FETCH_COLUMN);
  print_r($r);
'
```

### Authoring a new migration

- Filename: `NNN_<short_name>.sql`, increment `NNN` from the last one.
- Every DDL statement must be idempotent (`IF NOT EXISTS`,
  `ADD COLUMN IF NOT EXISTS`, etc.) — the entrypoint re-runs all of
  them on every start.
- Forward-only. Never edit a migration that has already been deployed
  — write a new one that revokes the change.
- No `DROP TABLE` or `DROP COLUMN` of a column that the current code
  still reads. Stage destructive migrations behind a code-only
  release first.

## API key rotation

The `DOCI_API_KEY_HASH` env var holds the SHA-256 of one (1) API key.
There is no built-in grace window — rotation is a hard cut.

1. Generate a new key + hash (see **Configuration** above).
2. Update `.env`, redeploy the container.
3. Update every caller (MCP clients, agents, cron, `deep` CLI env)
   with the new plaintext key. Old key stops working the moment the
   container restarts.

If the leak window is wider than the rotation can cover (e.g. agents
on machines you don't control), redeploy the container with
`DOCI_API_KEY_HASH=` (empty, disables API-key auth), force everyone
to the Remote-User browser flow, then re-enable with a new key.

## Backup & recovery

| Data | Method | Frequency | Retention | RTO |
|---|---|---|---|---|
| Postgres `documents` table | `pg_dump doci` | Every 6h | 30 days | < 15 min |
| `files/` working tree | `git push` to a remote, OR `rsync` to off-host | Every commit (push hook) or every 6h | Forever (git is append-only) | < 5 min |
| `files/.data/` (threads, versions) | Same as above — they're inside the git tree | — | — | — |
| `.env` (secrets) | Out-of-band (encrypted vault) | On change | Forever | < 5 min |

`files/` is the source of truth. The `documents` table is derived
metadata — if you lose only the DB, `scripts/index-documents.php`
rebuilds rows for every file on disk. You will lose tags, summaries,
and per-row `created_by` (these were authored through the API, not
embedded in markdown). Soft-delete tombstones are also lost — files
that had `deleted_at` set will reappear in the UI as live documents.

For DR drills, run quarterly:
1. Restore Postgres from backup to a test instance.
2. Clone `files/` from the git remote.
3. `docker compose -f docker-compose.dev.yml up -d` pointed at the
   restored DB + cloned files.
4. Compare `SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL`
   against the file count on disk. They should match within the
   backup window.
5. Click a few documents in the UI; verify versions and threads load.

## Incident response

### Severity levels

| Level | Definition | Response | Example |
|---|---|---|---|
| S1 | Service down, no one can read or write | 15 min | `/api/health.php` returns 503; Apache won't start |
| S2 | Major feature broken, workaround exists | 1 hour | Threads can't be created (DB column drift) |
| S3 | Minor issue, limited impact | 4 hours | One MCP tool returns malformed JSON |
| S4 | Cosmetic / edge case | Next phase | Breadcrumb title wrong for nested versions |

### Response process

1. **Detect** — alert from healthcheck, or user report.
2. **Assess** — pick severity.
3. **Mitigate** — for S1/S2, rollback to the previous tag first; fix later.
4. **Communicate** — post in the team channel; if external users are
   affected, update status page.
5. **Resolve** — deploy fix with a regression test
   ([`06-testing-strategy.md`](06-testing-strategy.md): "every bug
   fix MUST include a regression test").
6. **Postmortem** — within 48h for S1/S2. Use the template below.

### Postmortem template

```
Title:    [YYYY-MM-DD] One-line description
Duration: HH:MM to HH:MM UTC (Xh Ym)
Impact:   N users affected, N requests failed, N documents lost (if any)
Root cause: What actually broke and why
Timeline: Minute-by-minute actions taken
Action items:
  - [ ] Preventive: changes that prevent recurrence
  - [ ] Detective: monitoring that would catch this sooner
  - [ ] Corrective: process gap that allowed it
```

File postmortems as documents in DOCI itself, under
`incidents/YYYY-MM-DD-<slug>.md`, then add to `key_documents` with
domain `incidents` so they surface for future on-call rotations.

---

> Next: [08-changelog.md](08-changelog.md) — track what changed and when.
