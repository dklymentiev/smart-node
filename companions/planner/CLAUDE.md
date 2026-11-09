# Instructions for AI agents working in this repository

This file is for AI agents (Claude, Cursor, Copilot, etc.) operating inside the Planner repository. It is intentionally short -- the canonical product documentation lives in [docs/](docs/).

## Project context

- **Name:** Planner
- **Type:** Self-hostable task manager (OSS)
- **Stack:** Python 3.11+, FastAPI (REST), MCP (agent interface), PostgreSQL 16
- **License:** MIT
- **Status:** Bootstrap -- design phase, no working code yet

## Hard rules

1. **Independence.** Planner depends on PostgreSQL and nothing else. No imports from any other project, no assumptions about an enclosing system.
2. **Interface parity.** Every operation exposed in any interface (CLI / REST / MCP / UI) must be available in all four. Never add a UI-only feature.
3. **Pipeline-first.** The six-stage pipeline (intake -> assessment -> approval -> execution -> verification -> delivery) is the core abstraction. Do not bypass it.
4. **Non-goals are real.** See `docs/01-vision.md`. If a change feels like it belongs in a memory system, document store, or workflow engine, it does not belong in Planner.

## Documentation conventions

- Product docs follow Software Product Template v1.1 in `docs/` (`01-vision.md`, `02-spec.md`, ...).
- Code docstrings: short, what-not-why. Rationale belongs in `docs/03-architecture.md` as design decisions (DD-NNN).

## Development

```bash
docker compose up -d db
pip install -e ".[dev]"
pytest
```

## Release gates (mandatory before any tag)

- Phase 4 -- Cross-check (functional spec vs technical reference, zero gaps)
- Phase 4.5 -- Live integration (real PostgreSQL, real MCP client, real subprocess, real client-disconnect)
- Phase 4.6 -- UX adequacy (L1 100% / L2 >= 85% / L3 >= 70%)

None may be skipped. See `docs/06-testing-strategy.md` (TBD).
