"""Dashboard aggregations -- read-models for the dashboard.

Bespoke aggregations (time series, progress rollups, task queues) that do
not belong to any single resource repo. Consumed by both the JSON API
(``routers/dashboard.py``) and the web UI (``ui/router.py``), so the two
never diverge.
"""

from __future__ import annotations

import asyncpg

from planner.repos.objectives import ObjectivesRepo
from planner.repos.projects import ProjectsRepo

#: Statuses that count a task as finished -- excluded from "active" counts.
_TERMINAL_STATUSES = ("done", "rejected")

#: Statuses that surface a task in the "needs attention" queue, worst first.
_ATTENTION_STATUSES = ("escalated", "blocked", "rejected")


class DashboardRepo:
    """Aggregated read-models for the dashboard."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- time series --------------------------------------------------------

    async def task_flow_series(self, days: int) -> list[dict]:
        """Per-day counts of tasks opened and closed for the last ``days``
        calendar days (oldest first), with missing days zero-filled."""
        rows = await self._pool.fetch(
            """
            WITH days AS (
              SELECT (current_date - n)::date AS day
                FROM generate_series(0, $1 - 1) AS n
            ),
            opened AS (
              SELECT created_at::date AS day, COUNT(*)::int AS n
                FROM tasks
               WHERE created_at >= current_date - ($1 - 1) * interval '1 day'
               GROUP BY 1
            ),
            closed AS (
              SELECT completed_at::date AS day, COUNT(*)::int AS n
                FROM tasks
               WHERE completed_at IS NOT NULL
                 AND completed_at >= current_date - ($1 - 1) * interval '1 day'
               GROUP BY 1
            )
            SELECT d.day,
                   COALESCE(o.n, 0) AS opened,
                   COALESCE(c.n, 0) AS closed
              FROM days d
              LEFT JOIN opened o ON o.day = d.day
              LEFT JOIN closed c ON c.day = d.day
             ORDER BY d.day ASC
            """,
            days,
        )
        return [
            {"day": r["day"], "opened": r["opened"], "closed": r["closed"]}
            for r in rows
        ]

    # -- progress rollups ---------------------------------------------------

    async def project_progress(self) -> list[dict]:
        """Active projects with task counts and done percentage."""
        terminal = ", ".join("'" + s + "'" for s in _TERMINAL_STATUSES)
        rows = await self._pool.fetch(
            f"""
            SELECT p.id, p.slug, p.title, p.description, p.status,
                   COUNT(t.id) AS total,
                   COUNT(t.id) FILTER (WHERE t.stage = 'done') AS done_count,
                   COUNT(t.id) FILTER (WHERE t.stage NOT IN ({terminal}))
                       AS active_count
              FROM projects p
              LEFT JOIN tasks t ON t.project_id = p.id
             WHERE p.status = 'active'
             GROUP BY p.id
             ORDER BY p.sort_order, p.slug
            """
        )
        out: list[dict] = []
        for r in rows:
            total = r["total"] or 0
            done = r["done_count"] or 0
            out.append(
                {
                    "slug": r["slug"],
                    "title": r["title"],
                    "description": r["description"],
                    "total": total,
                    "done": done,
                    "active": r["active_count"] or 0,
                    "pct": int(round(100 * done / total)) if total else 0,
                }
            )
        return out

    async def objective_progress(self, obj_type: str) -> list[dict]:
        """One row per non-cancelled objective of ``obj_type``. Progress =
        done tasks / all tasks in the objective's subtree."""
        obj_repo = ObjectivesRepo(self._pool)
        objectives = await obj_repo.list(type=obj_type)
        projects = {p.id: p for p in await ProjectsRepo(self._pool).list()}
        out: list[dict] = []
        for o in objectives:
            if o.status == "cancelled":
                continue
            ids = await obj_repo.subtree_ids(o.id)
            rows = await self._pool.fetch(
                """
                SELECT stage AS status, COUNT(*)::int AS n
                  FROM tasks
                 WHERE objective_id = ANY($1::int[])
                 GROUP BY stage
                """,
                ids,
            )
            by_status = {r["status"]: r["n"] for r in rows}
            total = sum(by_status.values())
            done = by_status.get("done", 0)
            proj = projects.get(o.project_id)
            out.append(
                {
                    "id": o.id,
                    "title": o.title,
                    "status": o.status,
                    "project_slug": proj.slug if proj else None,
                    "total": total,
                    "done": done,
                    "pct": int(round(100 * done / total)) if total else 0,
                }
            )
        return out

    # -- task queues --------------------------------------------------------

    async def _task_rows(self, sql: str, *args: object) -> list[dict]:
        rows = await self._pool.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def recent_tasks(self, limit: int) -> list[dict]:
        """Most recently created tasks, newest first."""
        return await self._task_rows(
            """
            SELECT t.id, t.title, t.stage, t.priority, t.assignee,
                   t.updated_at, p.slug AS project_slug
              FROM tasks t
              LEFT JOIN projects p ON p.id = t.project_id
             ORDER BY t.created_at DESC, t.id DESC
             LIMIT $1
            """,
            limit,
        )

    async def attention_tasks(self) -> list[dict]:
        """Tasks that need attention -- incident/escalated/blocked/rejected,
        worst status first."""
        return await self._task_rows(
            """
            SELECT t.id, t.title, t.stage, t.priority, t.assignee,
                   t.blocked_reason, t.updated_at, p.slug AS project_slug
              FROM tasks t
              LEFT JOIN projects p ON p.id = t.project_id
             WHERE t.stage = ANY($1::text[])
             ORDER BY array_position($1::text[], t.stage), t.updated_at DESC
            """,
            list(_ATTENTION_STATUSES),
        )

    async def tasks_for_actor(self, actor_id: str) -> list[dict]:
        """Open tasks assigned to ``actor_id``, by priority then due date."""
        return await self._task_rows(
            """
            SELECT t.id, t.title, t.stage, t.priority, t.assignee,
                   t.due_date, t.updated_at, p.slug AS project_slug
              FROM tasks t
              LEFT JOIN projects p ON p.id = t.project_id
             WHERE t.assignee = $1
               AND t.stage NOT IN ('done', 'rejected')
             ORDER BY
               CASE t.priority
                 WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                 WHEN 'medium' THEN 2 ELSE 3 END,
               t.due_date NULLS LAST,
               t.updated_at DESC
            """,
            actor_id,
        )

    async def tasks_in_review(self) -> list[dict]:
        """Tasks in the review queue, longest-waiting first."""
        return await self._task_rows(
            """
            SELECT t.id, t.title, t.stage, t.priority, t.assignee,
                   t.updated_at, p.slug AS project_slug
              FROM tasks t
              LEFT JOIN projects p ON p.id = t.project_id
             WHERE t.stage = 'pending_review'
             ORDER BY t.updated_at ASC
            """,
        )

    async def agent_tasks(self) -> list[dict]:
        """In-progress tasks held by an actor of type ``agent``."""
        return await self._task_rows(
            """
            SELECT t.id, t.title, t.stage, t.priority, t.assignee,
                   t.updated_at, p.slug AS project_slug
              FROM tasks t
              JOIN actors a ON a.id = t.assignee
              LEFT JOIN projects p ON p.id = t.project_id
             WHERE t.stage = 'in_progress' AND a.type = 'agent'
             ORDER BY t.updated_at DESC
            """,
        )
