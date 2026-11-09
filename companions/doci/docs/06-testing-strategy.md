# DOCI — Testing Strategy

## Pyramid

```
                    ┌──────────────────┐
                    │ Live Benchmark   │  ← MANDATORY before tagging a release
                    │  (real Docker,   │
                    │  Postgres, git,  │
                    │  MCP over HTTP)  │
                    └──────────────────┘
                  ┌──────────────────────┐
                  │ API integration tests │  ← tests/api-test.sh
                  │  (bash + curl)         │
                  └──────────────────────┘
              ┌──────────────────────────────┐
              │ Unit tests (PHPUnit)          │  ← pure domain logic
              │  tests/Unit/                  │
              └──────────────────────────────┘
```

Three layers, three failure modes they catch:

- **Unit** — argument validation, path canonicalization, GUID format,
  pure-function business rules. Caught early, run on every commit.
- **API integration** — full request/response over HTTP against a
  running container: auth, JSON shape, DB row, git commit, file on
  disk. Catches wiring bugs.
- **Live benchmark** — manual, before tagging. Catches the things
  containers, mocks, and shell tests cannot model: a real browser
  hitting the UI, a real MCP client over stdio, a real reverse-proxy
  header from Traefik.

## Unit tests (`tests/Unit/`)

**Scope:** pure domain logic in `lib/` — no DB, no filesystem, no HTTP.
If a function needs I/O to be tested, split the testable piece out.

| File | Covers | Tests |
|---|---|---|
| `ValidationTest.php` | `lib/validation.php` — path canonicalization, traversal guard, GUID format check | 20 |
| `PathSafetyTest.php` | `lib/validation.php::validate_path_within` against a real temp directory (the only realpath-dependent function in `validation.php`) | 6 |
| `MarkdownTest.php` | `lib/markdown.php` pure helpers: `extract_title`, `doci_is_html_document`, `find_thread_quote_in_source`, `check_thread_wrap_target`, `process_footnotes`. DB-backed helpers (`rewrite_links_to_guids`, `path_to_guid_url`, `repair_dead_guid_link`) are left to integration. | 21 |
| `MeshTest.php` | `lib/mesh.php` pure helpers: `getDOCIMeshGuid` (deterministic md5 prefix), `getMeshSkipFolders` (env CSV parsing). HTTP clients (`syncDocumentToMesh`, `searchMesh`) are left to integration. | 8 |

**Configuration:** `phpunit.xml` at repo root. Coverage source = `lib/`.
Strict mode (`failOnRisky=true`, `failOnWarning=true`).

**Run:**
```bash
composer test
# or directly
vendor/bin/phpunit
# inside the container
docker compose exec doci vendor/bin/phpunit
```

**Gate:** 100% green before any commit to `main`. CI enforces.

**What goes here:** anything in `lib/` that takes data in and returns
data out. `lib/validation.php` is the model. `lib/response.php`'s
status-code mapping and `lib/git.php`'s commit-message formatting
belong here when added.

**What does NOT go here:**
- Anything that calls `get_db()` — that's integration.
- Anything that writes to `files/` — that's integration.
- HTTP handlers in `api/*.php` — that's integration.

## API integration tests (`tests/api-test.sh`)

**Scope:** every public HTTP endpoint, hit with real `curl` against a
running container, asserting status code, JSON shape, and observable
side effects (DB row, file on disk, git commit).

**Configuration:** auto-detects the `doci` container IP via `docker
inspect`; respects `DOCI_URL` and `DOCI_API_KEY` overrides. Bypasses
the reverse-proxy by default to test the API in isolation.

**Run:**
```bash
docker compose -f docker-compose.dev.yml up -d
DOCI_API_KEY="dev-test-key-not-for-production" ./tests/api-test.sh
```

**Coverage:** 12 test cases across documents, inbox, threads, versions,
health. Cleanup tracked via `CLEANUP_GUIDS` — every created resource
is soft-deleted at the end.

**Gate:** all green before tagging any release. Required after any
change to `api/*.php` or `documents.php`.

**Planned expansion (Phase 5):**
- `auth_traefik` — Remote-User header path (currently only API key is exercised).
- `csrf_rejection` — with `DOCI_CSRF_ENABLED=true`, verify that a
  missing token returns 403.
- `register` — index an out-of-band file, verify idempotency.
- `key_documents` — full M:N CRUD.
- `version_chain` — verify version → original linkage and ordering.

## Live Benchmark Gate (MANDATORY)

**Why it exists.** The Software Product Template v1.1 added this gate
after the Flint R0-R10 crisis: 1293 green unit tests missed three
integration bugs that only surfaced on the first live benchmark run.
DOCI ships PHP behind Apache, talks to Postgres, writes to a git
working tree, and exposes an MCP server that auto-detects container
IPs — exactly the class of integration code where mocks lie.

**Definition.** A release candidate is not a release until the person
tagging it has observed, on a real `docker compose up` deployment (not
a local PHP built-in server, not mocked):

1. **Real document create-edit-delete round trip in a browser.**
   Open http://localhost:8080, create a new document via the UI, edit
   it, soft-delete it. Verify the row in Postgres (`SELECT
   guid,path,deleted_at FROM documents WHERE guid='<g>'`), the file
   in `files/<path>.md`, and the three git commits (`Create`,
   `Update`, `Delete`) in `files/.git/`.

2. **Real thread → version → reply chain.** Select a sentence in a
   document, right-click, Start Thread, write a comment, tick AI,
   submit. Verify: a `version` row exists with `original_guid` set,
   a `thread` row exists with `parent_guid` pointing at the version,
   the AI reply appears in the thread file, the version bar at the
   top of the page renders `Original | DV-1`.

3. **Real MCP tool call from a real client.** Configure
   `mcp_server.py` in Claude Desktop or Claude Code. Call
   `doci_inbox(text="benchmark")`. Verify the item appears in
   `/api/inbox.php?action=list`.

4. **Real reverse-proxy auth.** Put DOCI behind Traefik (or any
   forward-auth proxy) configured to set `Remote-User`. Open the UI
   without an API key. Verify the session is created and the username
   shows in commit attribution.

5. **Real CSRF rejection** (once Phase 5 re-enables the gate).
   Browser session authenticated via `Remote-User`. `curl -X POST`
   without the CSRF token. Expect 403.

**Who runs it.** The person tagging the release. Not CI. CI can run
synthetic equivalents (`tests/api-test.sh` headless) but those don't
count as the gate.

**Failure mode.** If any item fails, the tag does not get created.
Root-cause must be fixed and re-benchmarked. Do not paper over with
"the API tests are green".

**Evidence.** Copy DB rows, file paths, and screenshots into
`docs/live-benchmark-<version>.md`. The release artifact includes this.

## UX Adequacy Gate (Phase 4.6) — not applicable

The Software Product Process Blueprint makes Phase 4.6 mandatory for
conversational / agent products (chatbots, voice assistants, CLIs
with prompts, customer-facing AIs).

DOCI is a **document workspace with a markdown viewer, REST/MCP/CLI
surfaces, and an optional AI replier inside threads.** The AI replier
is the only conversational surface — and even there, the human writes
a comment, the AI replies, the human can edit the reply manually.
There is no greeting flow, no obedience contract, no register
contract to violate. The L1 failure modes Phase 4.6 catches
(dumping tool names on hello, ignoring `stop`, auto-executing plans)
don't exist here.

Decision recorded: 2026-05-23, this document.

If a future DOCI release adds a conversational helper — e.g. "ask
your documents" assistant that calls multiple tools per turn, voice
front-end, Telegram bot answering knowledge-base questions — the
gate becomes mandatory and this section must be replaced with a
benchmark suite, an L1 test inventory, and a target score per the
blueprint.

## Coverage expectations

| Layer | Current | Target at v1.0 |
|---|---|---|
| `lib/` (pure logic) | `validation.php`, `markdown.php` (pure helpers), `mesh.php` (pure helpers) | 1+ unit test per public function |
| `api/` (HTTP handlers) | 12 cases | one happy path + one error case per endpoint |
| `documents.php` (repository) | none direct | exercised through API tests; unit-test the pure helpers if extracted |
| UI (`index.php`, JS) | none | live benchmark only |

DOCI does not enforce a numeric line-coverage floor. The floor is
**every public function in `lib/` has a unit test** and **every
endpoint in `api/` has at least one API test**.

## CI pipeline

`.github/workflows/test.yml` runs on every push and PR against `main`,
matrixed across PHP 8.2 and 8.3:

```
1. composer install                (~10s)
2. composer test                   (~5s)   — phpunit unit suite
3. composer analyse                (~5s)   — phpstan level 5 on lib/ + api/  (continue-on-error)
4. lint job: find . -name '*.php' | xargs -n1 php -l
```

Stages 1-4 are pure PHP. The Docker-backed integration stage
(`tests/api-test.sh`) is **not** wired into CI yet — runs locally via
`make api-test`. Adding it is part of Phase 6.

The live benchmark is manual, not in CI.

## Bug reproduction rule

Every bug fix MUST include a regression test:
1. Write a test that reproduces the bug (fails before the fix).
2. Apply the fix.
3. Verify the test passes.
4. The test stays in the suite permanently.

If the bug only appears under real conditions (live benchmark), the
regression test must be at the API or live-benchmark level — do not
"fix" integration bugs with unit-test mocks.

## Anti-patterns

- **Mocking `get_db()` in a unit test and calling it integration.**
  It's a unit test of whatever wrapped the mock. Real Postgres or it
  doesn't count.
- **`if ($result) { /* pass */ }` with no assertion.** Assert the
  observable outcome — a specific GUID format, a specific HTTP code,
  a specific row count.
- **Skipping the live benchmark for "small changes".** Refactors
  break integrations. That's exactly the case the gate exists for.
- **Writing a test against `localhost:8080` and committing the
  hardcoded URL.** Use `$DOCI_URL` with a sensible default.

---

> Next: [07-operations.md](07-operations.md) — how to deploy and operate what you tested.
