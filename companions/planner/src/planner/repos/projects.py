"""Projects repository.

Projects are the top-level container for tasks and objectives. Slug is the
human-readable handle used in CLI/REST/MCP -- enforced unique. Numeric id is
the FK target.

Like actors, projects are not hard-deleted -- archive instead. Tasks pinned
to an archived project remain queryable but should not appear in default
filters at the API layer.
"""

from __future__ import annotations

import re

import asyncpg

from planner.models import Project, ProjectStatus

PROJECT_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class ProjectError(ValueError):
    """Raised on invalid project input or constraint violations."""


def _row_to_project(row: asyncpg.Record) -> Project:
    return Project(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_slug(slug: str) -> None:
    if not PROJECT_SLUG_RE.match(slug):
        raise ProjectError(
            f"project slug must match {PROJECT_SLUG_RE.pattern!r}, got {slug!r}"
        )


_PROJECT_COLS = "id, slug, title, description, status, sort_order, created_at, updated_at"


class ProjectsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        slug: str,
        title: str,
        *,
        description: str | None = None,
        sort_order: int = 0,
    ) -> Project:
        _validate_slug(slug)
        if not title.strip():
            raise ProjectError("title must not be blank")
        try:
            row = await self._pool.fetchrow(
                f"""
                INSERT INTO projects (slug, title, description, sort_order)
                VALUES ($1, $2, $3, $4)
                RETURNING {_PROJECT_COLS}
                """,
                slug,
                title,
                description,
                sort_order,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ProjectError(f"project slug {slug!r} already exists") from exc
        assert row is not None
        return _row_to_project(row)

    async def get(self, project_id: int) -> Project | None:
        row = await self._pool.fetchrow(
            f"SELECT {_PROJECT_COLS} FROM projects WHERE id = $1",
            project_id,
        )
        return _row_to_project(row) if row else None

    async def get_by_slug(self, slug: str) -> Project | None:
        row = await self._pool.fetchrow(
            f"SELECT {_PROJECT_COLS} FROM projects WHERE slug = $1",
            slug,
        )
        return _row_to_project(row) if row else None

    async def list(
        self,
        *,
        status: ProjectStatus | None = None,
    ) -> list[Project]:
        sql = f"SELECT {_PROJECT_COLS} FROM projects"
        args: list[object] = []
        if status is not None:
            sql += " WHERE status = $1"
            args.append(status)
        sql += " ORDER BY sort_order, slug"
        rows = await self._pool.fetch(sql, *args)
        return [_row_to_project(r) for r in rows]

    async def update(
        self,
        project_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        status: ProjectStatus | None = None,
        sort_order: int | None = None,
    ) -> Project | None:
        sets: list[str] = []
        args: list[object] = []
        if title is not None:
            if not title.strip():
                raise ProjectError("title must not be blank")
            sets.append(f"title = ${len(args) + 1}")
            args.append(title)
        if description is not None:
            sets.append(f"description = ${len(args) + 1}")
            args.append(description)
        if status is not None:
            if status not in ("active", "archived"):
                raise ProjectError(f"invalid status: {status!r}")
            sets.append(f"status = ${len(args) + 1}")
            args.append(status)
        if sort_order is not None:
            sets.append(f"sort_order = ${len(args) + 1}")
            args.append(sort_order)
        if not sets:
            return await self.get(project_id)

        sets.append("updated_at = now()")
        args.append(project_id)
        row = await self._pool.fetchrow(
            f"""
            UPDATE projects
               SET {", ".join(sets)}
             WHERE id = ${len(args)}
            RETURNING {_PROJECT_COLS}
            """,
            *args,
        )
        return _row_to_project(row) if row else None

    async def archive(self, project_id: int) -> Project | None:
        return await self.update(project_id, status="archived")
