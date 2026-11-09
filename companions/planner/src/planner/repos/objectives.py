"""Objectives repository.

Objectives form a tree under a project: ``strategy`` typically at the root,
``roadmap`` / ``milestone`` / ``goal`` / ``initiative`` / ``campaign`` nested
underneath. The ``parent_id`` self-reference enforces no formal type
hierarchy, so any type can nest under any other -- the application decides
what makes sense for a given project.

Tree retrieval is the only non-trivial query in this repo. We pull the whole
project's objectives in one recursive CTE call, then assemble the tree in
Python. That keeps the query simple and lets the caller decide how to render.
"""

from __future__ import annotations

import asyncpg

from planner.models import Objective, ObjectiveNode, ObjectiveStatus, ObjectiveType

_OBJ_COLS = (
    "id, parent_id, project_id, title, type, status, "
    "sort_order, description, created_at, updated_at"
)

_VALID_TYPES = ("strategy", "roadmap", "milestone", "goal", "initiative", "campaign")
_VALID_STATUSES = ("active", "completed", "paused", "cancelled", "draft")


class ObjectiveError(ValueError):
    """Raised on invalid objective input or constraint violations."""


def _row_to_objective(row: asyncpg.Record) -> Objective:
    return Objective(
        id=row["id"],
        parent_id=row["parent_id"],
        project_id=row["project_id"],
        title=row["title"],
        type=row["type"],
        status=row["status"],
        sort_order=row["sort_order"],
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ObjectivesRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        *,
        project_id: int,
        title: str,
        type: ObjectiveType,
        parent_id: int | None = None,
        description: str | None = None,
        sort_order: int = 0,
        status: ObjectiveStatus = "active",
    ) -> Objective:
        if type not in _VALID_TYPES:
            raise ObjectiveError(f"invalid objective type: {type!r}")
        if status not in _VALID_STATUSES:
            raise ObjectiveError(f"invalid objective status: {status!r}")
        if not title.strip():
            raise ObjectiveError("title must not be blank")

        try:
            row = await self._pool.fetchrow(
                f"""
                INSERT INTO objectives
                    (parent_id, project_id, title, type, status, sort_order, description)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING {_OBJ_COLS}
                """,
                parent_id,
                project_id,
                title,
                type,
                status,
                sort_order,
                description,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise ObjectiveError(f"parent or project does not exist: {exc}") from exc
        assert row is not None
        return _row_to_objective(row)

    async def get(self, objective_id: int) -> Objective | None:
        row = await self._pool.fetchrow(
            f"SELECT {_OBJ_COLS} FROM objectives WHERE id = $1",
            objective_id,
        )
        return _row_to_objective(row) if row else None

    async def list(
        self,
        *,
        project_id: int | None = None,
        parent_id: int | None = None,
        type: ObjectiveType | None = None,
        status: ObjectiveStatus | None = None,
    ) -> list[Objective]:
        where: list[str] = []
        args: list[object] = []
        if project_id is not None:
            where.append(f"project_id = ${len(args) + 1}")
            args.append(project_id)
        if parent_id is not None:
            where.append(f"parent_id = ${len(args) + 1}")
            args.append(parent_id)
        if type is not None:
            where.append(f"type = ${len(args) + 1}")
            args.append(type)
        if status is not None:
            where.append(f"status = ${len(args) + 1}")
            args.append(status)
        sql = f"SELECT {_OBJ_COLS} FROM objectives"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY sort_order, id"
        rows = await self._pool.fetch(sql, *args)
        return [_row_to_objective(r) for r in rows]

    async def update(
        self,
        objective_id: int,
        *,
        title: str | None = None,
        type: ObjectiveType | None = None,
        status: ObjectiveStatus | None = None,
        sort_order: int | None = None,
        description: str | None = None,
        parent_id: int | None = None,
        clear_parent: bool = False,
    ) -> Objective | None:
        sets: list[str] = []
        args: list[object] = []
        if title is not None:
            if not title.strip():
                raise ObjectiveError("title must not be blank")
            sets.append(f"title = ${len(args) + 1}")
            args.append(title)
        if type is not None:
            if type not in _VALID_TYPES:
                raise ObjectiveError(f"invalid objective type: {type!r}")
            sets.append(f"type = ${len(args) + 1}")
            args.append(type)
        if status is not None:
            if status not in _VALID_STATUSES:
                raise ObjectiveError(f"invalid objective status: {status!r}")
            sets.append(f"status = ${len(args) + 1}")
            args.append(status)
        if sort_order is not None:
            sets.append(f"sort_order = ${len(args) + 1}")
            args.append(sort_order)
        if description is not None:
            sets.append(f"description = ${len(args) + 1}")
            args.append(description)
        if clear_parent:
            sets.append("parent_id = NULL")
        elif parent_id is not None:
            if parent_id == objective_id:
                raise ObjectiveError("objective cannot be its own parent")
            sets.append(f"parent_id = ${len(args) + 1}")
            args.append(parent_id)
        if not sets:
            return await self.get(objective_id)

        sets.append("updated_at = now()")
        args.append(objective_id)
        try:
            row = await self._pool.fetchrow(
                f"""
                UPDATE objectives SET {", ".join(sets)}
                WHERE id = ${len(args)}
                RETURNING {_OBJ_COLS}
                """,
                *args,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise ObjectiveError(f"new parent does not exist: {exc}") from exc
        return _row_to_objective(row) if row else None

    async def delete(self, objective_id: int) -> bool:
        """Hard-delete an objective. Children have parent_id set to NULL."""
        result = await self._pool.execute(
            "DELETE FROM objectives WHERE id = $1",
            objective_id,
        )
        return result.endswith(" 1")

    async def tree(self, *, project_id: int) -> list[ObjectiveNode]:
        """Return forest of objective trees rooted at NULL parent in ``project_id``.

        Issues a single recursive CTE, then assembles the tree in Python so the
        caller does not have to worry about cycles or ordering.
        """
        rows = await self._pool.fetch(
            f"""
            WITH RECURSIVE walk AS (
                SELECT {_OBJ_COLS}, 0 AS depth
                FROM objectives
                WHERE project_id = $1 AND parent_id IS NULL

                UNION ALL

                SELECT o.id, o.parent_id, o.project_id, o.title, o.type,
                       o.status, o.sort_order, o.description,
                       o.created_at, o.updated_at, w.depth + 1
                FROM objectives o
                JOIN walk w ON o.parent_id = w.id
                WHERE o.project_id = $1
            )
            SELECT * FROM walk
            ORDER BY depth, sort_order, id
            """,
            project_id,
        )

        nodes: dict[int, ObjectiveNode] = {}
        roots: list[ObjectiveNode] = []
        for row in rows:
            obj = _row_to_objective(row)
            node = ObjectiveNode(objective=obj, depth=row["depth"], children=[])
            nodes[obj.id] = node
            if obj.parent_id is None:
                roots.append(node)
            else:
                parent = nodes.get(obj.parent_id)
                if parent is not None:
                    parent.children.append(node)
                else:
                    # Orphan due to project filter -- treat as root in returned forest.
                    roots.append(node)
        return roots

    async def subtree_ids(self, root_id: int) -> list[int]:
        """Return ``root_id`` plus every descendant id, in BFS order."""
        rows = await self._pool.fetch(
            """
            WITH RECURSIVE walk AS (
                SELECT id, 0 AS depth FROM objectives WHERE id = $1
                UNION ALL
                SELECT o.id, w.depth + 1 FROM objectives o
                JOIN walk w ON o.parent_id = w.id
            )
            SELECT id FROM walk ORDER BY depth, id
            """,
            root_id,
        )
        return [r["id"] for r in rows]
