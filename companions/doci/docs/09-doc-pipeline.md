# DOCI — Documentation Pipeline

How DOCI's documentation is produced, validated, and kept current.
Companion to [`04-roadmap.md`](04-roadmap.md) (Phase 3 closes the
canonical doc set) and [`06-testing-strategy.md`](06-testing-strategy.md)
(test-related doc gates).

## Three layers, three audiences

| Layer | Audience | Source of truth | Generated? |
|---|---|---|---|
| **L1 — Code reference** | A developer extending DOCI | PHP source + docblocks | Optional (phpDocumentor); not wired today |
| **L2 — Architecture** | A new contributor or auditor | `docs/01-vision.md` … `docs/09-doc-pipeline.md`, repo root `.md` | Hand-written, lint-checked against canon |
| **L3 — User-facing** | Someone running an instance | `files/` (the demo content rendered through the DOCI UI itself) | Hand-written, dog-fooded through the product |

DOCI is unusual in that L3 lives **inside** the product. The same
engine that renders user documents renders the demo / landing /
tutorial content. No separate docs site, no Sphinx build, no GitHub
Pages.

## Layer 1 — Code reference

**Status:** not wired in v0.1.0. PHP source has docblocks in most
files (`config.php`, `documents.php`, `api/*.php`, `lib/*.php`) but
nothing currently renders them.

**When to wire it:**
- The public surface (`api/`, `lib/`) grows past what the hand-written
  [`05-api-reference.md`](05-api-reference.md) can cover comfortably.
- A contributor asks for "where do I read the function reference".

**How to wire it (when the time comes):**

```bash
composer require --dev phpdocumentor/phpdocumentor
vendor/bin/phpdoc -d lib/ -d api/ -t docs/_code-reference/
```

Output goes to `docs/_code-reference/` (gitignored — generated, not
checked in). A CI step regenerates on every release tag and uploads
to the docs branch.

**Rule when wired:** every public function in `lib/` and every
endpoint handler in `api/` must have a docblock. CI fails on missing
ones.

## Layer 2 — Architecture & process docs

**Source of truth:** `docs/0N-*.md` files in this directory plus
root-level `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`,
`RELEASING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CLAUDE.md`.

**Numbering canon:**

| File | Owner | Update trigger |
|---|---|---|
| `01-vision.md` | Product | Direction change |
| `02-spec.md` | Product + Dev | New user-facing operation |
| `03-architecture.md` | Tech lead | New component or design decision |
| `04-roadmap.md` | Tech lead | Phase transition |
| `05-api-reference.md` | Dev | Endpoint added / changed / removed |
| `06-testing-strategy.md` | Dev / QA | Test layer added; live benchmark contract change |
| `07-operations.md` | DevOps | New env var; new container; new incident category |
| `08-changelog.md` | Dev (release manager) | Every tagged release |
| `09-doc-pipeline.md` | Dev | This pipeline changes |
| `data-dictionary.md` | Dev | Schema change |

`08-changelog.md` is a stub that points to root `CHANGELOG.md` — DOCI
keeps the changelog at repo root for tooling compatibility (GitHub
release notes, dependabot, packagist).

**Staleness signals (manual today, candidate for automation):**

| Signal | Check | Frequency |
|---|---|---|
| Endpoint exists in `api/` but missing from `05-api-reference.md` | `ls api/*.php` vs grep | Every release |
| Env var read by `config.php` but missing from `07-operations.md` Configuration table | grep `getenv(` vs the table | Every release |
| Migration in `migrations/` but no entry in `data-dictionary.md` | `ls migrations/*.sql` vs grep | Every migration |
| Roadmap phase claims `[DONE]` but exit-gate items still unchecked | `grep -B1 'DONE'` + manual | Every phase boundary |

Build script for the above (Phase 6 task — does not exist yet):

```bash
scripts/check-docs.sh
# Exits non-zero with a list of detected drifts.
```

## Layer 3 — User-facing content

This is the content that lives in `files/` and renders through the
DOCI UI. There are two slices:

1. **Landing + onboarding** — `files/README.md` is the root document
   visitors first see. The router falls back to it when `files/index.md`
   is absent (which is the default ship state), so it serves both `/`
   and `/README.html`. The `farm/` workspace next to it is the
   end-to-end demo a curious operator can click through. v0.1.0
   shipped a 30-file `tour/` + `demo-farm/` set; Phase 4 of the
   roadmap collapsed that to the README + farm pair.

2. **Live documentation of the DOCI instance itself** — operators can
   import their `docs/` markdown into `files/docs/` and have it
   rendered through the UI, with threads, versions, and the AI
   replier available against the docs themselves. This is optional —
   the source-of-truth `docs/` stays in git.

**Rule for L3:** every feature listed in [`01-vision.md`](01-vision.md)
must be demonstrable from the root document within one or two scroll
screens. The Phase 4 hallway test enforces this — five new users
identify every feature without reading the spec.

## Eating our own dog food

DOCI documents itself **in DOCI** wherever possible:

| Artifact | Lives in git? | Also rendered through DOCI? |
|---|---|---|
| `docs/0N-*.md` | yes (canonical) | yes (operators can mount `docs/` into `files/docs/` for in-product reading) |
| Demo / landing / tour (`files/`) | yes (seeded into image) | yes (this is the product surface) |
| Internal worklogs / decisions | not in OSS repo | yes — kept on a private DOCI instance |
| Incident post-mortems | yes (`incidents/`) | yes — registered as `key_documents` with domain `incidents` |

The author's private DOCI instance is the production reference
implementation — features prove themselves there before being
extracted into OSS. See [`01-vision.md`](01-vision.md) "Audience" for
why this matters.

## Pipeline as it stands today (v0.1.0)

```
Hand-write    →   git commit   →   GitHub mirror   →   release tag
docs/*.md         (peer review                          (changelog
README, etc.       informally,                           updated, docs
                   no CI yet)                            shipped baked
                                                        into image)
                                                            |
                                                            v
                                              In-product L3 content
                                              served via files/ on
                                              the running container
```

Gaps versus the canon:
- No `scripts/check-docs.sh` for staleness detection (Phase 6).
- No CI to enforce docstring coverage (Phase 6).
- L1 code reference not generated (deferred until needed).
- Dependency graph / layer-violation check (canon Layer 2) not
  implemented — DOCI is small enough that visual review of
  `composer.json` autoload + `require_once` lines is sufficient.

## Pipeline as it should stand at v1.0

```
Source change                  Pre-commit               CI on PR / main
─────────────                  ──────────               ───────────────
api/ change         →  composer analyse        →  scripts/check-docs.sh
                       (phpstan level 5)          (drift detection)
                                                       |
docs/ change        →  markdownlint            →  link checker
                                                  (no dead links)
                                                       |
migrations/         →  hand-update             →  schema vs
                       data-dictionary.md         data-dictionary diff
                                                       |
release tag         →  RELEASING.md checklist  →  changelog gate
                                                  (HEAD entry exists)
```

The composer scripts already cover the analyse step. The rest is
Phase 6.

## When to update this document

- A new doc is added to `docs/`. Update the L2 numbering table.
- A new layer is wired (L1 code reference, or CI staleness check).
  Replace the "as it stands today" diagram with the new reality.
- The doc-canon template at
  `files/templates/software-product/` in this repo bumps a version.
  Reconcile the differences and bump this doc's revision.

---

> Index: [README](../README.md) | Previous: [08-changelog](08-changelog.md)
