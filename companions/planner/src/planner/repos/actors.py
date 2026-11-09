"""Actors repository.

Actors are the registry of who can act on tasks (humans, agents, the system
itself). They are foreign-keyed from tasks.assignee, tasks.requester,
task_comments.author, task_log.actor -- so they can never be hard-deleted.
The ``active`` flag exists for retiring an actor without breaking history.
"""

from __future__ import annotations

import re
from typing import Iterable

import asyncpg

from planner.models import Actor, ActorType

ACTOR_ID_RE = re.compile(r"^(?:[a-z0-9][a-z0-9._-]{0,63}|component:[a-z][a-z0-9._-]{0,53})$")


class ActorError(ValueError):
    """Raised on invalid actor input or constraint violations."""


def _row_to_actor(row: asyncpg.Record) -> Actor:
    return Actor(
        id=row["id"],
        display_name=row["display_name"],
        type=row["type"],
        active=row["active"],
        created_at=row["created_at"],
    )


def _validate_id(actor_id: str) -> None:
    if not ACTOR_ID_RE.match(actor_id):
        raise ActorError(
            f"actor id must match {ACTOR_ID_RE.pattern!r}, got {actor_id!r}"
        )


class ActorsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        actor_id: str,
        display_name: str,
        type: ActorType,
    ) -> Actor:
        _validate_id(actor_id)
        if not display_name.strip():
            raise ActorError("display_name must not be blank")
        if type not in ("agent", "human", "system"):
            raise ActorError(f"invalid actor type: {type!r}")

        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO actors (id, display_name, type)
                VALUES ($1, $2, $3)
                RETURNING id, display_name, type, active, created_at
                """,
                actor_id,
                display_name,
                type,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ActorError(f"actor {actor_id!r} already exists") from exc
        assert row is not None
        return _row_to_actor(row)

    async def get(self, actor_id: str) -> Actor | None:
        row = await self._pool.fetchrow(
            "SELECT id, display_name, type, active, created_at FROM actors WHERE id = $1",
            actor_id,
        )
        return _row_to_actor(row) if row else None

    async def list(
        self,
        *,
        type: ActorType | None = None,
        active: bool | None = None,
    ) -> list[Actor]:
        where: list[str] = []
        args: list[object] = []
        if type is not None:
            where.append(f"type = ${len(args) + 1}")
            args.append(type)
        if active is not None:
            where.append(f"active = ${len(args) + 1}")
            args.append(active)
        sql = "SELECT id, display_name, type, active, created_at FROM actors"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        rows = await self._pool.fetch(sql, *args)
        return [_row_to_actor(r) for r in rows]

    async def update(
        self,
        actor_id: str,
        *,
        display_name: str | None = None,
        active: bool | None = None,
    ) -> Actor | None:
        sets: list[str] = []
        args: list[object] = []
        if display_name is not None:
            if not display_name.strip():
                raise ActorError("display_name must not be blank")
            sets.append(f"display_name = ${len(args) + 1}")
            args.append(display_name)
        if active is not None:
            sets.append(f"active = ${len(args) + 1}")
            args.append(active)
        if not sets:
            return await self.get(actor_id)

        args.append(actor_id)
        row = await self._pool.fetchrow(
            f"""
            UPDATE actors
               SET {", ".join(sets)}
             WHERE id = ${len(args)}
            RETURNING id, display_name, type, active, created_at
            """,
            *args,
        )
        return _row_to_actor(row) if row else None

    async def deactivate(self, actor_id: str) -> Actor | None:
        """Soft-delete: actors keep all FK relationships intact."""
        return await self.update(actor_id, active=False)

    async def exists_many(self, actor_ids: Iterable[str]) -> set[str]:
        """Return the subset of ``actor_ids`` that exist in the table."""
        ids = list(set(actor_ids))
        if not ids:
            return set()
        rows = await self._pool.fetch(
            "SELECT id FROM actors WHERE id = ANY($1::text[])",
            ids,
        )
        return {r["id"] for r in rows}
