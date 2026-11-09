# Releasing DOCI

Checklist for cutting a tagged release. No automation yet — this is the
manual recipe. CI gates ship in Phase 6
([`docs/04-roadmap.md`](docs/04-roadmap.md)).

## Before you start

- Working tree is clean: `git status` shows no uncommitted changes on
  `main`.
- You are the person who will personally run the Live Benchmark Gate.
  If you can't, don't cut the release.

## 1. Confirm exit criteria for the phase

Open [`docs/04-roadmap.md`](docs/04-roadmap.md). Every checkbox for the
phase this release closes must be ticked, or the checklist item moved
to a later phase with a written reason.

## 2. Run the full automated suite

```bash
# Unit + analysis
composer install --no-interaction
composer analyse
composer test

# API integration against a running container
docker compose -f docker-compose.dev.yml up -d --build
DOCI_API_KEY="dev-test-key-not-for-production" ./tests/api-test.sh
docker compose -f docker-compose.dev.yml down -v
```

All green. No warnings, no skipped tests without explanation.

## 3. Lint

```bash
find lib api scripts tests -name '*.php' -type f -print0 \
  | xargs -0 -n1 php -l > /tmp/doci-lint.log \
  || (cat /tmp/doci-lint.log; exit 1)
```

Clean output = pass.

## 4. Live Benchmark Gate (MANDATORY)

Per [`docs/06-testing-strategy.md`](docs/06-testing-strategy.md). All
items must be observed and recorded — not "the API tests look fine":

1. **Document create-edit-delete in a browser.** Verify the DB row, the
   file on disk, and the three git commits (`Create`, `Update`,
   `Delete`).
2. **Thread → version → reply chain.** Verify the `version` row, the
   `thread` row, the AI reply in the thread file (if AI used), the
   version bar in the UI.
3. **MCP tool call from a real client.** `doci_inbox` from Claude
   Desktop / Claude Code, item appears in
   `/api/inbox.php?action=list`.
4. **Reverse-proxy auth.** Behind Traefik or any forward-auth proxy
   that sets `Remote-User`, UI loads without an API key and the
   username appears in commit attribution.
5. **CSRF rejection** (once Phase 5 re-enables the gate). Authenticated
   session, `curl -X POST` without token, expect 403.

If any item fails, stop. Fix the underlying bug. Restart this checklist.

## 5. Update VERSION + CHANGELOG

```bash
echo "X.Y.Z" > VERSION
```

Prepend a new section in `CHANGELOG.md` under `[Unreleased]`:

```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added / Changed / Fixed / Security
- bullet list curated from commits since the previous tag
```

Commit:

```bash
git add VERSION CHANGELOG.md
git commit -m "chore: release X.Y.Z"
```

## 6. Tag

```bash
git tag -a vX.Y.Z -m "vX.Y.Z: <one-line summary>"
git push origin main --tags
```

## 7. Post-release

- [ ] Record Live Benchmark evidence in
  `docs/live-benchmark-vX.Y.Z.md` (DB row excerpts, screenshots, git
  log lines). This becomes part of the release artifact.
- [ ] Write a worklog in mesh referencing the release commit + tag.
- [ ] If deploying to production, run from the project directory so
  Traefik labels resolve (example path; substitute your own):
  ```bash
  docker compose --project-directory /srv/doci \
    -f docker-compose.yml up -d --build doci
  ```
- [ ] Verify `/api/health.php` returns 200 on the deployed host.
- [ ] Verify one existing document still renders correctly (open a
  known GUID, confirm content + breadcrumbs + version bar).

## 8. Rollback plan

- Tag the bad release as `vX.Y.Z-rollback-YYYY-MM-DD` for history.
- `git revert <bad-commit>` and ship a `vX.Y.Z+1` with the revert.
  Do **not** delete the bad tag from the public remote — it's a lie
  to people who already pulled it.
- If the rollback requires schema changes: write a
  `migrations/NNN_rollback_X_Y_Z.sql` that is reversible. Never edit
  a previously shipped migration in place — append a new one that
  revokes the change.

## Semver for DOCI

- **MAJOR** — breaking change to the HTTP API contract
  (request/response shapes, status code meaning), the MCP tool names
  or signatures, the database schema in a way that an operator can't
  apply through an additive migration, or the `files/` layout
  (`.data/versions/...` path conventions).
- **MINOR** — new endpoint, new MCP tool, new config knob, new
  doc-type, additive DB change, new optional request fields.
- **PATCH** — bug fixes, CSS, docs, dependency bumps, internal
  refactors with no visible behavior change.

Pre-1.0 (`0.x`), MINOR may still break things — `-beta` / `-rc`
suffixes signal that. After 1.0, breakage requires a MAJOR bump.

## What is NOT allowed

- Amending or force-pushing a published tag.
- Tagging a release without running the Live Benchmark Gate.
- Tagging while `.env` with production credentials is accidentally
  staged — always `git status` first.
- Skipping `CHANGELOG.md` updates because "it's just a small fix".
  Every release gets an entry.
- Editing a previously shipped migration in `migrations/` in place.
- Releasing while [`docs/04-roadmap.md`](docs/04-roadmap.md) still
  claims the phase is `[IN FLIGHT]` — update the status first.
