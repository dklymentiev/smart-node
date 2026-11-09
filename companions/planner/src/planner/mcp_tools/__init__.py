"""MCP tool definitions.

Each tool is a thin wrapper over the repo layer that mirrors REST CRUD.
Tools return JSON-serialisable dicts so the MCP client can render them
directly. Errors are surfaced as ``{"error": "..."}`` payloads -- the MCP
runtime maps any raised exception into a tool error response.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from typing import Any

import asyncpg

from planner.repos.actors import ActorsRepo
from planner.repos.ancillary import (
    CommentsRepo,
    CommitsRepo,
    LinksRepo,
    LogRepo,
    NotificationsRepo,
    RelationsRepo,
    TagsRepo,
)
from planner.repos.objectives import ObjectivesRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


def _serialise(value: Any) -> Any:
    """Recursively turn dataclasses / datetimes / ObjectiveNode into plain JSON."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _serialise(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, list):
        return [_serialise(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialise(v) for k, v in value.items()}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------


def tool_catalogue() -> list[dict]:
    """Return the JSONSchema-shaped descriptors used by ``list_tools``."""
    return [
        # --- Actors ---
        {
            "name": "actor_create",
            "description": "Create an actor (agent / human / system).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "type": {"type": "string", "enum": ["agent", "human", "system"]},
                },
                "required": ["id", "display_name", "type"],
            },
        },
        {
            "name": "actor_get",
            "description": "Fetch an actor by id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "actor_list",
            "description": "List actors filtered by type and active flag.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["agent", "human", "system"]},
                    "active": {"type": "boolean"},
                },
            },
        },
        {
            "name": "actor_deactivate",
            "description": "Soft-delete an actor (sets active=false).",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        # --- Projects ---
        {
            "name": "project_create",
            "description": "Create a project with a slug + title.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "sort_order": {"type": "integer"},
                },
                "required": ["slug", "title"],
            },
        },
        {
            "name": "project_get",
            "description": "Fetch a project by id or slug.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "slug": {"type": "string"},
                },
            },
        },
        {
            "name": "project_list",
            "description": "List projects (optionally filtered by status).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["active", "archived"]},
                },
            },
        },
        {
            "name": "project_archive",
            "description": "Archive a project.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        # --- Objectives ---
        {
            "name": "objective_create",
            "description": (
                "Create an objective. Type can be strategy / roadmap / "
                "milestone / goal / initiative / campaign."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "type": {"type": "string"},
                    "parent_id": {"type": "integer"},
                    "description": {"type": "string"},
                    "sort_order": {"type": "integer"},
                    "status": {"type": "string"},
                },
                "required": ["project_id", "title", "type"],
            },
        },
        {
            "name": "objective_tree",
            "description": "Recursive objective tree for a project.",
            "inputSchema": {
                "type": "object",
                "properties": {"project_id": {"type": "integer"}},
                "required": ["project_id"],
            },
        },
        {
            "name": "objective_progress",
            "description": "Aggregated progress over an objective subtree.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        # --- Tasks ---
        {
            "name": "task_create",
            "description": "Create a task (no pipeline transitions enforced here).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "project_id": {"type": "integer"},
                    "objective_id": {"type": "integer"},
                    "assignee": {"type": "string"},
                    "requester": {"type": "string"},
                    "priority": {"type": "string"},
                    "status": {"type": "string"},
                    "due_date": {"type": "string", "format": "date"},
                    "complexity_hint": {"type": "string"},
                    "scope": {"type": "string"},
                    "eta": {"type": "string"},
                    "recur_interval": {
                        "type": "string",
                        "description": "make it recurring: '1 day', '2 weeks', "
                                       "'1 month' (or daily / weekly / monthly)",
                    },
                    "next_run": {
                        "type": "string",
                        "format": "date-time",
                        "description": "when the first cycle opens; default one interval from now",
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": "task_get",
            "description": "Fetch a task by id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        {
            "name": "task_list",
            "description": "List tasks with filters.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer"},
                    "objective_id": {"type": "integer"},
                    "assignee": {"type": "string"},
                    "status": {"type": "string"},
                    "priority": {"type": "string"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
            },
        },
        {
            "name": "task_update",
            "description": (
                "Generic task field update. recur_interval ('1 day', 'weekly', "
                "'off') and next_run set or end a recurrence."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "fields": {"type": "object"},
                },
                "required": ["id", "fields"],
            },
        },
        {
            "name": "task_delete",
            "description": "Hard-delete a task.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        # --- Ancillary ---
        {
            "name": "comment_add",
            "description": "Add a comment to a task. Author must be supplied.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "author": {"type": "string"},
                    "content": {"type": "string"},
                    "comment_type": {"type": "string"},
                },
                "required": ["task_id", "author", "content"],
            },
        },
        {
            "name": "comments_list",
            "description": "List comments on a task in chronological order.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "tag_add",
            "description": "Add a tag to a task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "tag": {"type": "string"},
                },
                "required": ["task_id", "tag"],
            },
        },
        {
            "name": "tag_remove",
            "description": "Remove a tag from a task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "tag": {"type": "string"},
                },
                "required": ["task_id", "tag"],
            },
        },
        {
            "name": "tags_list",
            "description": "List tags on a task.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "link_add",
            "description": "Attach an external link to a task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "kind": {"type": "string"},
                    "url": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["task_id", "kind", "url"],
            },
        },
        {
            "name": "links_list",
            "description": "List links on a task.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "log_list",
            "description": "Read the audit log for a task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["task_id"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def dispatch(pool: asyncpg.Pool, name: str, args: dict, *, actor_id: str | None = None) -> Any:
    """Execute a tool by name and return a JSON-serialisable result."""
    args = args or {}

    # Actors
    if name == "actor_create":
        repo = ActorsRepo(pool)
        return _serialise(await repo.create(args["id"], args["display_name"], args["type"]))
    if name == "actor_get":
        repo = ActorsRepo(pool)
        return _serialise(await repo.get(args["id"]))
    if name == "actor_list":
        repo = ActorsRepo(pool)
        return _serialise(await repo.list(type=args.get("type"), active=args.get("active")))
    if name == "actor_deactivate":
        repo = ActorsRepo(pool)
        return _serialise(await repo.deactivate(args["id"]))

    # Projects
    if name == "project_create":
        repo = ProjectsRepo(pool)
        return _serialise(
            await repo.create(
                args["slug"],
                args["title"],
                description=args.get("description"),
                sort_order=args.get("sort_order", 0),
            )
        )
    if name == "project_get":
        repo = ProjectsRepo(pool)
        if "id" in args:
            return _serialise(await repo.get(args["id"]))
        return _serialise(await repo.get_by_slug(args["slug"]))
    if name == "project_list":
        repo = ProjectsRepo(pool)
        return _serialise(await repo.list(status=args.get("status")))
    if name == "project_archive":
        repo = ProjectsRepo(pool)
        return _serialise(await repo.archive(args["id"]))

    # Objectives
    if name == "objective_create":
        repo = ObjectivesRepo(pool)
        return _serialise(
            await repo.create(
                project_id=args["project_id"],
                title=args["title"],
                type=args["type"],
                parent_id=args.get("parent_id"),
                description=args.get("description"),
                sort_order=args.get("sort_order", 0),
                status=args.get("status", "active"),
            )
        )
    if name == "objective_tree":
        repo = ObjectivesRepo(pool)
        return _serialise(await repo.tree(project_id=args["project_id"]))
    if name == "objective_progress":
        repo = ObjectivesRepo(pool)
        ids = await repo.subtree_ids(args["id"])
        rows = await pool.fetch(
            """
            SELECT stage AS status, COUNT(*) AS n FROM tasks
            WHERE objective_id = ANY($1::int[])
            GROUP BY stage
            """,
            ids,
        )
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        done = by_status.get("done", 0)
        pct = int(round(100 * done / total)) if total else 0
        return {
            "objective_id": args["id"],
            "subtree_objective_count": len(ids),
            "total_tasks": total,
            "tasks_by_status": by_status,
            "progress_pct": pct,
        }

    # Tasks
    if name == "task_create":
        repo = TasksRepo(pool)
        task_args = {k: v for k, v in args.items() if k != "actor_id"}
        return _serialise(await repo.create(actor_id=actor_id, **task_args))
    if name == "task_get":
        repo = TasksRepo(pool)
        return _serialise(await repo.get(args["id"]))
    if name == "task_list":
        repo = TasksRepo(pool)
        kwargs = {k: v for k, v in args.items() if k in {
            "project_id", "objective_id", "assignee", "status",
            "priority", "limit", "offset",
        }}
        return _serialise(await repo.list(**kwargs))
    if name == "task_update":
        repo = TasksRepo(pool)
        return _serialise(await repo.update(args["id"], actor_id=actor_id, **args["fields"]))
    if name == "task_delete":
        repo = TasksRepo(pool)
        return {"deleted": await repo.delete(args["id"])}

    # Ancillary
    if name == "comment_add":
        repo = CommentsRepo(pool)
        return _serialise(
            await repo.add(
                args["task_id"],
                author=args["author"],
                content=args["content"],
                comment_type=args.get("comment_type"),
            )
        )
    if name == "comments_list":
        repo = CommentsRepo(pool)
        return _serialise(await repo.list_for_task(args["task_id"]))
    if name == "tag_add":
        repo = TagsRepo(pool)
        return {"added": await repo.add(args["task_id"], args["tag"])}
    if name == "tag_remove":
        repo = TagsRepo(pool)
        return {"removed": await repo.remove(args["task_id"], args["tag"])}
    if name == "tags_list":
        repo = TagsRepo(pool)
        return await repo.list_for_task(args["task_id"])
    if name == "link_add":
        repo = LinksRepo(pool)
        return _serialise(
            await repo.add(
                args["task_id"],
                kind=args["kind"],
                url=args["url"],
                label=args.get("label"),
            )
        )
    if name == "links_list":
        repo = LinksRepo(pool)
        return _serialise(await repo.list_for_task(args["task_id"]))
    if name == "log_list":
        repo = LogRepo(pool)
        return _serialise(
            await repo.list_for_task(args["task_id"], limit=args.get("limit", 200))
        )

    raise ValueError(f"unknown MCP tool: {name!r}")
