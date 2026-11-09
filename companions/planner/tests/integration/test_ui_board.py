"""Integration tests for the kanban board."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from planner.app import create_app
from planner.auth import ApiKeysRepo
from planner.repos.ancillary import TagsRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client_and_pool(pool: asyncpg.Pool):
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        _, key = await ApiKeysRepo(pool).create(label="test", actor_id="system")
        c.cookies.set("planner_key", key)
        yield c, pool


async def test_board_page_renders_columns(client_and_pool) -> None:
    client, pool = client_and_pool
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    repo = TasksRepo(pool)
    open_t = await repo.create(
        title="Spec it", project_id=project.id, status="open"
    )
    in_p = await repo.create(
        title="Code it",
        project_id=project.id,
        status="in_progress",
        assignee="system",
        priority="high",
    )
    done = await repo.create(
        title="Ship it", project_id=project.id, status="done"
    )

    r = await client.get("/ui/projects/oss/board")
    assert r.status_code == 200
    body = r.text
    assert "OSS" in body
    # All six columns labelled.
    for label in (
        "Open",
        "Awaiting approval",
        "In progress",
        "Pending review",
        "Done",
    ):
        assert label in body
    # Cards present.
    assert "Spec it" in body
    assert "Code it" in body
    assert "Ship it" in body
    # Priority + assignee on the in-progress card.
    assert "high" in body
    assert "@system" in body


async def test_board_partial_returns_only_columns(client_and_pool) -> None:
    client, pool = client_and_pool
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    await TasksRepo(pool).create(
        title="X", project_id=project.id, status="open"
    )

    r = await client.get("/ui/projects/oss/_board")
    assert r.status_code == 200
    # Partial -- no <html>/<head>/<title>/nav.
    assert "<html" not in r.text.lower()
    assert "Open" in r.text
    assert "X" in r.text


async def test_board_renders_tags(client_and_pool) -> None:
    client, pool = client_and_pool
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    task = await TasksRepo(pool).create(
        title="With tags", project_id=project.id, status="open"
    )
    await TagsRepo(pool).add(task.id, "needs-review")
    await TagsRepo(pool).add(task.id, "urgent")

    r = await client.get("/ui/projects/oss/board")
    assert "needs-review" in r.text
    assert "urgent" in r.text


async def test_board_filters_by_project(client_and_pool) -> None:
    client, pool = client_and_pool
    p1 = await ProjectsRepo(pool).create(slug="p1", title="P1")
    p2 = await ProjectsRepo(pool).create(slug="p2", title="P2")
    await TasksRepo(pool).create(
        title="task-in-p1", project_id=p1.id, status="open"
    )
    await TasksRepo(pool).create(
        title="task-in-p2", project_id=p2.id, status="open"
    )

    r = await client.get("/ui/projects/p1/board")
    assert "task-in-p1" in r.text
    assert "task-in-p2" not in r.text


async def test_board_unknown_project_404(client_and_pool) -> None:
    client, _ = client_and_pool
    r = await client.get("/ui/projects/nope/board")
    assert r.status_code == 404
    assert "could not be found" in r.text.lower()


async def test_board_skips_terminal_statuses(client_and_pool) -> None:
    client, pool = client_and_pool
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    await TasksRepo(pool).create(
        title="rejected one", project_id=project.id, status="rejected"
    )
    await TasksRepo(pool).create(
        title="incident one", project_id=project.id, status="rejected"
    )

    r = await client.get("/ui/projects/oss/board")
    assert r.status_code == 200
    assert "rejected one" not in r.text
    assert "incident one" not in r.text
