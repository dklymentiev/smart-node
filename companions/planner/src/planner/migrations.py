"""Migration runner.

Scans ``migrations/`` for files matching ``NNNN_*.sql`` (zero-padded sequence),
applies pending ones in order inside a single transaction each, and tracks
applied versions in a ``_migrations`` table.

Idempotent: re-running after success is a no-op. Re-running after a partial
failure rolls back the failed migration cleanly because each migration runs in
its own transaction.

Schema selection:
    - If ``PLANNER_SCHEMA`` is unset, migrations run in the connection's default
      schema (typically ``public``). This is the standalone mode.
    - If ``PLANNER_SCHEMA`` is set, that schema is created if missing and
      ``search_path`` is set to it for the duration of each migration. This is
      the embedded mode where the operator hosts Planner alongside other
      tenants in one database.

The ``_migrations`` table itself lives in the same schema as the migrations.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


def discover(migrations_dir: Path) -> list[Migration]:
    """Return all valid migrations from ``migrations_dir`` sorted by version.

    Raises ``MigrationError`` on duplicate versions or malformed filenames that
    look like migrations but do not match the strict pattern.
    """
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory does not exist: {migrations_dir}")

    found: dict[int, Migration] = {}
    for entry in sorted(migrations_dir.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".sql"):
            continue
        match = MIGRATION_FILENAME_RE.match(entry.name)
        if not match:
            raise MigrationError(
                f"migration filename does not match NNNN_name.sql: {entry.name}"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{found[version].path.name} and {entry.name}"
            )
        found[version] = Migration(version=version, name=entry.stem, path=entry)

    return [found[v] for v in sorted(found)]


async def _ensure_schema(conn: asyncpg.Connection, schema: str | None) -> None:
    if not schema:
        return
    # CREATE SCHEMA IF NOT EXISTS is safe and idempotent.
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}"')


async def _ensure_tracking_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def _applied_versions(conn: asyncpg.Connection) -> set[int]:
    rows = await conn.fetch("SELECT version FROM _migrations")
    return {row["version"] for row in rows}


async def apply_pending(
    pool: asyncpg.Pool,
    migrations_dir: Path,
    *,
    schema: str | None = None,
) -> list[Migration]:
    """Apply every migration whose version is not yet in ``_migrations``.

    Returns the list of migrations that were applied in this call (possibly
    empty). Each migration runs in its own transaction. The first failure
    aborts the run and re-raises ``MigrationError``; earlier successful
    migrations remain committed.
    """
    if schema is None:
        schema = os.environ.get("PLANNER_SCHEMA") or None

    migrations = discover(migrations_dir)
    applied_now: list[Migration] = []

    async with pool.acquire() as conn:
        await _ensure_schema(conn, schema)
        await _ensure_tracking_table(conn)
        already = await _applied_versions(conn)

        pending = [m for m in migrations if m.version not in already]
        if not pending:
            logger.info("migrations: nothing to apply (%d already on disk)", len(migrations))
            return []

        for migration in pending:
            logger.info(
                "migrations: applying %04d_%s",
                migration.version,
                migration.name.split("_", 1)[-1] if "_" in migration.name else migration.name,
            )
            try:
                async with conn.transaction():
                    await _ensure_schema(conn, schema)
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO _migrations (version, name) VALUES ($1, $2)",
                        migration.version,
                        migration.name,
                    )
            except Exception as exc:
                raise MigrationError(
                    f"migration {migration.path.name} failed: {exc}"
                ) from exc
            applied_now.append(migration)

    logger.info("migrations: applied %d migration(s)", len(applied_now))
    return applied_now


async def status(
    pool: asyncpg.Pool,
    migrations_dir: Path,
    *,
    schema: str | None = None,
) -> tuple[list[Migration], list[Migration]]:
    """Return ``(applied, pending)`` lists of migrations on disk."""
    if schema is None:
        schema = os.environ.get("PLANNER_SCHEMA") or None

    migrations = discover(migrations_dir)

    async with pool.acquire() as conn:
        await _ensure_schema(conn, schema)
        await _ensure_tracking_table(conn)
        already = await _applied_versions(conn)

    applied = [m for m in migrations if m.version in already]
    pending = [m for m in migrations if m.version not in already]
    return applied, pending
