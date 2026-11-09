"""Async PostgreSQL connection pool.

Wraps ``asyncpg.create_pool`` with two project-specific behaviors:

1. ``DATABASE_URL`` is read from the environment if no DSN is passed.
2. ``PLANNER_SCHEMA``, when set, is applied as ``search_path`` on every
   connection acquired from the pool, so the rest of the codebase can write
   schema-naive SQL.

Two helpers complete the picture:

- :func:`lifespan` — async context manager suitable for ``FastAPI(lifespan=...)``
  that opens a pool on startup and closes it on shutdown.
- :func:`transaction` — async context manager that yields a connection inside a
  transaction (commits on clean exit, rolls back on exception).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 10


class ConfigError(RuntimeError):
    """Raised when DB configuration is missing or invalid."""


def _resolve_dsn(dsn: str | None) -> str:
    if dsn:
        return dsn
    env_dsn = os.environ.get("DATABASE_URL")
    if not env_dsn:
        raise ConfigError(
            "DATABASE_URL is not set and no DSN was passed to create_pool()"
        )
    return env_dsn


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Run on every new connection acquired by the pool."""
    schema = os.environ.get("PLANNER_SCHEMA")
    if schema:
        # asyncpg quotes identifiers passed via parameters incorrectly for SET,
        # so we validate-and-inline. Schema names are constrained to the same
        # alphabet as PostgreSQL identifiers.
        if not schema.replace("_", "").isalnum():
            raise ConfigError(
                f"PLANNER_SCHEMA contains invalid characters: {schema!r}"
            )
        await conn.execute(f'SET search_path TO "{schema}"')


async def create_pool(
    dsn: str | None = None,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
) -> asyncpg.Pool:
    """Create and return an asyncpg pool with project defaults."""
    return await asyncpg.create_pool(
        _resolve_dsn(dsn),
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
    )


@asynccontextmanager
async def lifespan(
    dsn: str | None = None,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
) -> AsyncIterator[asyncpg.Pool]:
    """Open a pool, yield it, close it.

    Usage with FastAPI::

        @asynccontextmanager
        async def app_lifespan(app: FastAPI):
            async with lifespan() as pool:
                app.state.pool = pool
                yield
    """
    pool = await create_pool(dsn, min_size=min_size, max_size=max_size)
    try:
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def transaction(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection and wrap it in a transaction."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn
