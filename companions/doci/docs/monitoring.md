# DOCI — Monitoring & Observability

What to watch when a DOCI instance is in production. Companion to
[`07-operations.md`](07-operations.md) (the broader runbook); this
file isolates the monitoring/observability surface so on-call can
land here and skip past the deploy/incident sections.

DOCI does not ship a Prometheus exporter or a built-in dashboard.
It exposes one liveness probe, two log streams, and a few
table-level signals — wire those into the host's existing stack
(Grafana, Datadog, Uptime Kuma, plain cron, whatever you already
run).

## Signals at a glance

| Signal | Where | Healthy | Alert when |
|---|---|---|---|
| `/api/health.php` | HTTP probe | `200` + `{"status":"ok"}` | 3 consecutive failures (S1) |
| Apache 5xx rate | Reverse proxy access log or `docker logs doci` | < 0.1% over 5 min | > 1% over 5 min (S2) |
| `ERROR` lines in app log | `/var/log/doci/app.log` (volume `doci-logs`) | none | any new line (S2) |
| `files/` host disk usage | Host monitoring | < 75% | > 75% — versions chain can grow (S3) |
| File / row count drift | `documents` table vs `find files -name '*.md'` | within 10 | > 10 — out-of-band edits (S3) |
| Git commit lag | `git -C files log -1 --format=%ct` | within 60s of last write | > 5 min after a write (S3) |

Severity levels and response targets live in
[`07-operations.md#incident-response`](07-operations.md).

## The health probe

`GET /api/health.php` returns 200 with:

```json
{
  "status": "ok",
  "db": "connected",
  "files_writable": true,
  "version": "0.1.0"
}
```

It exits non-200 when:
- DB connection fails (PDO throws) — body has `db: "error"`.
- `files/` is not writable — body has `files_writable: false`.

The Dockerfile sets this as `HEALTHCHECK`, so `docker ps` shows
container health directly, and Traefik will pull the container out
of rotation on three consecutive failures (default
`healthCheck.fall=3`).

Wire it as the single source of truth for "is DOCI up":

```yaml
# Uptime Kuma / Healthchecks.io / your tool
- type: http
  url: https://${DOCI_DOMAIN}/api/health.php
  interval: 30s
  expect_status: 200
  expect_body_contains: '"status":"ok"'
```

## App log

DOCI emits a structured JSON-per-line application log to
`/var/log/doci/app.log` (mounted as the `doci-logs` named volume).
`DOCI_LOG_LEVEL` controls chattiness — `WARN` (default), `INFO`,
`DEBUG`, or `ERROR`. Security-relevant actions (auth failures, CSRF
violations, deletes, API-key usage) bypass the threshold and always
log:

```json
{"ts":"2026-05-24T14:30:12Z","rid":"r-7f3a","level":"info","user":"alice","uid":"a1b2c3d4-…","ip":"10.0.0.5","action":"document.update","data":{"guid":"…","fields":["tags"]}}
```

| Field | Meaning |
|---|---|
| `ts` | UTC timestamp, ISO8601 |
| `rid` | Per-request id — correlate multiple lines from one HTTP call |
| `level` | `debug` / `info` / `warning` / `error` |
| `user` | Remote-User value or `api-key` |
| `uid` | Document GUID the action targeted, when relevant |
| `ip` | Caller IP from `REMOTE_ADDR` |
| `action` | Dotted action name, e.g. `document.create`, `thread.reply`, `inbox.promote` |
| `data` | Action-specific payload metadata (never the document body) |

Tail live:

```bash
docker exec doci tail -f /var/log/doci/app.log
```

Pipe into Loki / OpenSearch / Splunk by mounting the volume into
your log shipper, or rotate it with `logrotate` and ship the
rotated files. JSON-per-line means no parser pain.

`DOCI_LOG_LEVEL=WARN` (prod default) keeps the log mostly silent —
warnings, errors, and security events still write; per-call info
lines do not. Flip to `INFO` or `DEBUG` temporarily when chasing a
bug; the volume can grow fast under load.

## Apache logs

`docker logs doci` shows the combined Apache access + error log.
The interesting signal is the 5xx rate; anything above 1% over 5
minutes is worth a page.

Useful one-liners:

```bash
# 5xx in the last hour
docker logs --since 1h doci 2>&1 | awk '$9 ~ /^5/' | wc -l

# Top callers by request volume
docker logs --since 1h doci 2>&1 | awk '{print $1}' | sort | uniq -c | sort -rn | head

# Slow requests (Apache logs %D in microseconds at end of line if mod_log_config
# is configured for that; not the default — add a custom LogFormat if needed)
```

## Database signals

Drift between disk and the index table is the silent failure mode:
files added out-of-band by `git pull` or a script that didn't go
through the API get committed, but the `documents` table doesn't
know about them until `scripts/index-documents.php` runs.

```bash
docker exec doci psql -h "$DB_HOST" -U "$DB_USER" -d doci -tAc \
  "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL"

docker exec doci find /var/www/html/files -name '*.md' \
  ! -path '*/\.data/threads/*' ! -path '*/\.data/versions/*' | wc -l
```

If the file count exceeds the row count by more than 10:

```bash
docker exec doci php scripts/index-documents.php
```

The entrypoint runs the same script on every container start, so
this only surfaces when files arrive *between* restarts.

## Git lag

Every API mutation ends with a git commit in `files/.git/`. If
commits stop happening but writes are still landing on disk, the
git hook is failing and the audit trail is gapping:

```bash
docker exec doci git -C files log -1 --format='%ct  %s'
# Compare epoch to `date +%s` — anything > 300s behind a known
# recent write is a problem.
```

Causes: disk full, corrupt index, a pre-commit hook installed
inside the container after a build. The fix is usually
`git -C files reset HEAD` followed by a manual re-commit; see
[`07-operations.md#backup--recovery`](07-operations.md).

## Optional: Prometheus shim

If you want metrics, the lightest path is a sidecar that scrapes
`/api/health.php` and exports a single gauge. Example one-liner with
`blackbox_exporter`:

```yaml
# blackbox.yml
modules:
  doci_health:
    prober: http
    http:
      preferred_ip_protocol: ip4
      valid_status_codes: [200]
      fail_if_body_not_matches_regexp: ['"status":"ok"']
```

DOCI itself does not expose `/metrics`. Adding it is on the
roadmap when there's clear demand; see
[`04-roadmap.md`](04-roadmap.md).

## What to dashboard

Minimum:

1. **Uptime** — health probe status, last 7 days.
2. **5xx rate** — line, last 24h, alert > 1% over 5 min.
3. **App-log error count** — bars, hourly, alert on first error.
4. **Disk usage on `files/`** — gauge, alert > 75%.

That's enough to know when something has gone wrong; the JSON
log + git log together explain what.
