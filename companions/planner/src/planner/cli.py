"""Planner CLI -- talks to the REST API.

Configuration via environment::

    PLANNER_API_URL  http://127.0.0.1:8080  (default)
    PLANNER_API_KEY  required for everything except `version` / `health`

The CLI is intentionally a thin wrapper. Behaviour parity is enforced by
``tests/integration/test_parity.py`` (R1 acceptance).

One exception: the ``key`` group manages API keys and therefore cannot go
through the API those keys authenticate. It talks to the database directly and
reads ``DATABASE_URL`` (and ``PLANNER_SCHEMA``) like the server does.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import click
import httpx

from planner import __version__

DEFAULT_URL = "http://127.0.0.1:8080"


def _client() -> httpx.Client:
    url = os.environ.get("PLANNER_API_URL", DEFAULT_URL)
    headers: dict[str, str] = {}
    api_key = os.environ.get("PLANNER_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    return httpx.Client(base_url=url, headers=headers, timeout=10.0)


def _print(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def _check(response: httpx.Response) -> Any:
    """Pretty-print body and exit non-zero on error."""
    try:
        body = response.json()
    except Exception:
        body = {"detail": response.text}
    if response.status_code >= 400:
        click.echo(json.dumps(body, indent=2), err=True)
        sys.exit(1)
    return body


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Planner -- self-hostable task management."""


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@main.command()
def health() -> None:
    """Probe the API health endpoint."""
    with _client() as c:
        _print(_check(c.get("/health")))


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


@main.group()
def actor() -> None:
    """Manage actors (agents / humans / system)."""


@actor.command("create")
@click.argument("actor_id")
@click.argument("display_name")
@click.option("--type", "type_", type=click.Choice(["agent", "human", "system"]), required=True)
def actor_create(actor_id: str, display_name: str, type_: str) -> None:
    with _client() as c:
        _print(_check(c.post("/api/v1/actors", json={
            "id": actor_id, "display_name": display_name, "type": type_,
        })))


@actor.command("list")
@click.option("--type", "type_", type=click.Choice(["agent", "human", "system"]))
@click.option("--active/--all", default=None)
def actor_list(type_: str | None, active: bool | None) -> None:
    params: dict[str, Any] = {}
    if type_:
        params["type"] = type_
    if active is not None:
        params["active"] = "true" if active else "false"
    with _client() as c:
        _print(_check(c.get("/api/v1/actors", params=params)))


@actor.command("get")
@click.argument("actor_id")
def actor_get(actor_id: str) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/actors/{actor_id}")))


@actor.command("deactivate")
@click.argument("actor_id")
def actor_deactivate(actor_id: str) -> None:
    with _client() as c:
        _print(_check(c.delete(f"/api/v1/actors/{actor_id}")))


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@main.group()
def project() -> None:
    """Manage projects."""


@project.command("create")
@click.argument("slug")
@click.argument("title")
@click.option("--description")
@click.option("--sort-order", type=int, default=0)
def project_create(slug: str, title: str, description: str | None, sort_order: int) -> None:
    payload: dict[str, Any] = {"slug": slug, "title": title, "sort_order": sort_order}
    if description:
        payload["description"] = description
    with _client() as c:
        _print(_check(c.post("/api/v1/projects", json=payload)))


@project.command("list")
@click.option("--status", "status_", type=click.Choice(["active", "archived"]))
def project_list(status_: str | None) -> None:
    params: dict[str, Any] = {}
    if status_:
        params["status_filter"] = status_
    with _client() as c:
        _print(_check(c.get("/api/v1/projects", params=params)))


@project.command("get")
@click.argument("identifier")
def project_get(identifier: str) -> None:
    """Fetch by numeric id or slug."""
    with _client() as c:
        if identifier.isdigit():
            _print(_check(c.get(f"/api/v1/projects/{identifier}")))
        else:
            _print(_check(c.get(f"/api/v1/projects/by-slug/{identifier}")))


@project.command("archive")
@click.argument("project_id", type=int)
def project_archive(project_id: int) -> None:
    with _client() as c:
        _print(_check(c.delete(f"/api/v1/projects/{project_id}")))


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------


_OBJECTIVE_TYPES = ("strategy", "roadmap", "milestone", "goal", "initiative", "campaign")


@main.group()
def objective() -> None:
    """Manage objectives (strategy/roadmap/milestone/goal/initiative/campaign)."""


@objective.command("create")
@click.argument("title")
@click.option("--project-id", type=int, required=True)
@click.option("--type", "type_", type=click.Choice(_OBJECTIVE_TYPES), required=True)
@click.option("--parent-id", type=int)
@click.option("--description")
def objective_create(
    title: str, project_id: int, type_: str, parent_id: int | None, description: str | None
) -> None:
    payload: dict[str, Any] = {"project_id": project_id, "title": title, "type": type_}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    if description:
        payload["description"] = description
    with _client() as c:
        _print(_check(c.post("/api/v1/objectives", json=payload)))


@objective.command("tree")
@click.option("--project-id", type=int, required=True)
def objective_tree(project_id: int) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/objectives/projects/{project_id}/tree")))


@objective.command("progress")
@click.argument("objective_id", type=int)
def objective_progress(objective_id: int) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/objectives/{objective_id}/progress")))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@main.group()
def task() -> None:
    """Manage tasks."""


@task.command("create")
@click.argument("title")
@click.option("--project-id", type=int)
@click.option("--objective-id", type=int)
@click.option("--assignee")
@click.option("--priority", type=click.Choice(["low", "medium", "high", "critical"]))
@click.option("--description")
@click.option("--recur", "recur_interval",
              help="make it recurring: '1 day', '2 weeks', '1 month', daily, weekly, monthly")
@click.option("--next-run", help="when the first cycle opens (ISO timestamp); default one interval from now")
def task_create(
    title: str,
    project_id: int | None,
    objective_id: int | None,
    assignee: str | None,
    priority: str | None,
    description: str | None,
    recur_interval: str | None,
    next_run: str | None,
) -> None:
    payload: dict[str, Any] = {"title": title}
    if project_id is not None:
        payload["project_id"] = project_id
    if objective_id is not None:
        payload["objective_id"] = objective_id
    if assignee:
        payload["assignee"] = assignee
    if priority:
        payload["priority"] = priority
    if description:
        payload["description"] = description
    if recur_interval:
        payload["recur_interval"] = recur_interval
    if next_run:
        payload["next_run"] = next_run
    with _client() as c:
        _print(_check(c.post("/api/v1/tasks", json=payload)))


@task.command("list")
@click.option("--project-id", type=int)
@click.option("--assignee")
@click.option("--status", "status_")
def task_list(project_id: int | None, assignee: str | None, status_: str | None) -> None:
    params: dict[str, Any] = {}
    if project_id is not None:
        params["project_id"] = project_id
    if assignee:
        params["assignee"] = assignee
    if status_:
        params["status_filter"] = status_
    with _client() as c:
        _print(_check(c.get("/api/v1/tasks", params=params)))


@task.command("get")
@click.argument("task_id", type=int)
def task_get(task_id: int) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/tasks/{task_id}")))


@task.command("update")
@click.argument("task_id", type=int)
@click.option("--field", "-f", multiple=True, help="key=value, repeat for multiple")
def task_update(task_id: int, field: tuple[str, ...]) -> None:
    if not field:
        click.echo("supply at least one --field key=value", err=True)
        sys.exit(2)
    payload: dict[str, Any] = {}
    for kv in field:
        if "=" not in kv:
            click.echo(f"bad --field {kv!r}; expected key=value", err=True)
            sys.exit(2)
        k, v = kv.split("=", 1)
        payload[k] = v
    with _client() as c:
        _print(_check(c.patch(f"/api/v1/tasks/{task_id}", json=payload)))


@task.command("delete")
@click.argument("task_id", type=int)
def task_delete(task_id: int) -> None:
    with _client() as c:
        r = c.delete(f"/api/v1/tasks/{task_id}")
        if r.status_code == 204:
            click.echo("deleted")
        else:
            _check(r)


# ---------------------------------------------------------------------------
# Comments / tags / links / log
# ---------------------------------------------------------------------------


@main.group()
def comment() -> None:
    """Comment on a task."""


@comment.command("add")
@click.argument("task_id", type=int)
@click.argument("content")
@click.option("--type", "comment_type")
def comment_add(task_id: int, content: str, comment_type: str | None) -> None:
    payload: dict[str, Any] = {"content": content}
    if comment_type:
        payload["comment_type"] = comment_type
    with _client() as c:
        _print(_check(c.post(f"/api/v1/tasks/{task_id}/comments", json=payload)))


@comment.command("list")
@click.argument("task_id", type=int)
def comment_list(task_id: int) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/tasks/{task_id}/comments")))


@main.group()
def tag() -> None:
    """Tag a task."""


@tag.command("add")
@click.argument("task_id", type=int)
@click.argument("tag_value")
def tag_add(task_id: int, tag_value: str) -> None:
    with _client() as c:
        r = c.post(f"/api/v1/tasks/{task_id}/tags", json={"tag": tag_value})
        if r.status_code == 204:
            click.echo("ok")
        else:
            _check(r)


@tag.command("list")
@click.argument("task_id", type=int)
def tag_list(task_id: int) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/tasks/{task_id}/tags")))


@tag.command("remove")
@click.argument("task_id", type=int)
@click.argument("tag_value")
def tag_remove(task_id: int, tag_value: str) -> None:
    with _client() as c:
        r = c.delete(f"/api/v1/tasks/{task_id}/tags/{tag_value}")
        if r.status_code == 204:
            click.echo("ok")
        else:
            _check(r)


@main.group()
def link() -> None:
    """Attach a link to a task."""


@link.command("add")
@click.argument("task_id", type=int)
@click.argument("url")
@click.option("--kind", required=True)
@click.option("--label")
def link_add(task_id: int, url: str, kind: str, label: str | None) -> None:
    payload: dict[str, Any] = {"kind": kind, "url": url}
    if label:
        payload["label"] = label
    with _client() as c:
        _print(_check(c.post(f"/api/v1/tasks/{task_id}/links", json=payload)))


@link.command("list")
@click.argument("task_id", type=int)
def link_list(task_id: int) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/tasks/{task_id}/links")))


@main.group()
def log() -> None:
    """Audit log for a task."""


@log.command("show")
@click.argument("task_id", type=int)
@click.option("--limit", type=int, default=200)
def log_show(task_id: int, limit: int) -> None:
    with _client() as c:
        _print(_check(c.get(f"/api/v1/tasks/{task_id}/log", params={"limit": limit})))


# ---------------------------------------------------------------------------
# API keys -- operator commands, and the one part of this CLI that does NOT go
# through the REST API.
#
# A key is what the REST API authenticates with, so minting the first one over
# that same API is a bootstrap with no beginning. These commands talk to the
# database directly and are meant to run where it is reachable: on the host, or
# through `docker exec` into the planner container. They read the same
# DATABASE_URL (and PLANNER_SCHEMA) the server does.
# ---------------------------------------------------------------------------


def _run_with_pool(coro_fn: Any) -> Any:
    """Open a short-lived pool from the server's own env, run, close."""

    async def _run() -> Any:
        from planner.db import create_pool

        pool = await create_pool()
        try:
            return await coro_fn(pool)
        finally:
            await pool.close()

    try:
        return asyncio.run(_run())
    except Exception as exc:  # missing DATABASE_URL, unreachable DB, unknown actor
        click.echo(f"{type(exc).__name__}: {exc}", err=True)
        sys.exit(1)


@main.group()
def key() -> None:
    """API key management (operator: needs DATABASE_URL, not the REST API)."""


@key.command("create")
@click.option("--label", required=True, help="What this key is for, e.g. 'core-api'.")
@click.option("--actor", required=True, help="Existing actor id the key authors as.")
@click.option(
    "--delegate",
    is_flag=True,
    default=False,
    help=(
        "Let this key name the caller it acts for, via the proxy-user header. "
        "Grant only to a component that authenticates its own callers: with "
        "this, whatever holds the key can author work under any name."
    ),
)
def key_create(label: str, actor: str, delegate: bool) -> None:
    """Create a key and print the plaintext ONCE. It is never recoverable."""
    from planner.auth import ApiKeysRepo

    async def _create(pool: Any) -> Any:
        return await ApiKeysRepo(pool).create(
            label=label, actor_id=actor, can_delegate=delegate
        )

    row, plaintext = _run_with_pool(_create)
    _print(
        {
            "id": row.id,
            "label": row.label,
            "actor_id": row.actor_id,
            "can_delegate": row.can_delegate,
            "key": plaintext,
            "note": "shown once and not stored -- save it now",
        }
    )


@key.command("list")
@click.option("--all", "include_revoked", is_flag=True, default=False)
def key_list(include_revoked: bool) -> None:
    """List keys. Plaintext is never shown; only hashes are stored."""
    from planner.auth import ApiKeysRepo

    async def _list(pool: Any) -> Any:
        return await ApiKeysRepo(pool).list(include_revoked=include_revoked)

    rows = _run_with_pool(_list)
    _print(
        [
            {
                "id": r.id,
                "label": r.label,
                "actor_id": r.actor_id,
                "can_delegate": r.can_delegate,
                "created_at": r.created_at,
                "last_used_at": r.last_used_at,
                "revoked_at": r.revoked_at,
            }
            for r in rows
        ]
    )


@key.command("revoke")
@click.argument("key_id", type=int)
def key_revoke(key_id: int) -> None:
    """Revoke a key. The row stays for audit; the key stops working."""
    from planner.auth import ApiKeysRepo

    async def _revoke(pool: Any) -> Any:
        return await ApiKeysRepo(pool).revoke(key_id)

    if _run_with_pool(_revoke):
        _print({"id": key_id, "revoked": True})
    else:
        click.echo(f"key {key_id} not found or already revoked", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Maintenance -- operator / cron commands that talk to the DB directly so they
# do not need an API key and do not depend on the HTTP stack being healthy.
# The cron line for an installed node:
#   /opt/planner/venv/bin/planner maintenance sweep >> /var/log/planner.log 2>&1
# ---------------------------------------------------------------------------


@main.group()
def maintenance() -> None:
    """Maintenance sweeps (cron-safe; talks to the DB directly)."""


@maintenance.command("sweep")
def maintenance_sweep() -> None:
    """Run the staleness sweep once and print what it did.

    Actions are written to task_comments / task_notifications by the pipeline.
    This command is safe to call from cron: it reads DATABASE_URL, opens a
    short-lived pool, and exits -- no API key or running server needed.
    """
    from planner.pipeline import Pipeline

    async def _sweep(pool: Any) -> dict:
        pipeline = Pipeline(pool)
        return await pipeline.staleness_sweep()

    result = _run_with_pool(_sweep)
    total = sum(len(v) for v in result.values())
    _print({"total_actions": total, "actions": result})
    if total:
        click.echo(f"staleness_sweep: {total} action(s) taken", err=True)


@maintenance.command("recur")
def maintenance_recur() -> None:
    """Open the cycles of recurring tasks that came due, and print what it did.

    A recurring task closed as completed is reopened and its assignee woken; one
    still open gets a comment and a wake. Safe to call from cron every few
    minutes: a cycle is opened once, next_run moves past now in the same
    transaction.
    """
    from planner.pipeline import Pipeline

    async def _recur(pool: Any) -> dict:
        return await Pipeline(pool).recurrence_sweep()

    result = _run_with_pool(_recur)
    total = sum(len(v) for v in result.values())
    _print({"total_actions": total, "actions": result})
    if total:
        click.echo(f"recurrence_sweep: {total} action(s) taken", err=True)


if __name__ == "__main__":
    main()
