"""Installer entrypoint: ``python -m planner.install``.

Two installer paths are supported, selected by environment:

**Standalone** -- ``docker compose up`` brings up bundled PostgreSQL. The
container creates the database via ``POSTGRES_DB``. ``PLANNER_SCHEMA`` is left
unset, so migrations run in the default ``public`` schema.

**Embedded** -- the operator points ``DATABASE_URL`` at a pre-existing host
PostgreSQL. They are responsible for creating the database. They may also set
``PLANNER_SCHEMA`` to namespace Planner alongside other tenants. The migration
runner creates the schema if missing and applies the SQL files there.

In both cases ``python -m planner.install`` is idempotent: running it on an
already-migrated database is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from planner.db import lifespan
from planner.migrations import MigrationError, apply_pending, status

logger = logging.getLogger(__name__)


def _migrations_dir() -> Path:
    """Locate the migrations directory.

    Honors ``PLANNER_MIGRATIONS_DIR`` if set, otherwise resolves relative to
    the repository root (``<repo>/migrations``).
    """
    explicit = os.environ.get("PLANNER_MIGRATIONS_DIR")
    if explicit:
        return Path(explicit)
    # src/planner/install.py -> src/planner -> src -> repo root
    return Path(__file__).resolve().parents[2] / "migrations"


async def init() -> int:
    """Apply pending migrations. Return process exit code (0 on success)."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    migrations_dir = _migrations_dir()
    schema = os.environ.get("PLANNER_SCHEMA")

    print(f"planner init: dsn={_redact(dsn)} schema={schema or '(default)'}")
    print(f"planner init: migrations dir={migrations_dir}")

    async with lifespan(dsn) as pool:
        applied, pending = await status(pool, migrations_dir, schema=schema)
        print(
            f"planner init: {len(applied)} already applied, "
            f"{len(pending)} pending"
        )
        if not pending:
            print("planner init: up to date")
            return 0
        try:
            done = await apply_pending(pool, migrations_dir, schema=schema)
        except MigrationError as exc:
            print(f"planner init: FAILED -- {exc}", file=sys.stderr)
            return 1
        for m in done:
            print(f"  applied {m.path.name}")
        print(f"planner init: applied {len(done)} migration(s)")
        return 0


def _redact(dsn: str) -> str:
    """Hide password from DSN for log output."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("PLANNER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(init())


if __name__ == "__main__":
    sys.exit(main())
