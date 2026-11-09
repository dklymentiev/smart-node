"""Integration tests for the project list partial."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from planner.app import create_app
from planner.auth import ApiKeysRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(pool: asyncpg.Pool):
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        _, key = await ApiKeysRepo(pool).create(label="test", actor_id="system")
        c.cookies.set("planner_key", key)
        yield c


async def test_projects_partial_empty_state(client) -> None:
    r = await client.get("/ui/_partials/projects")
    assert r.status_code == 200
    assert "No active projects yet" in r.text


async def test_projects_partial_lists_active_projects(client, pool) -> None:
    repo = ProjectsRepo(pool)
    await repo.create(slug="alpha", title="Alpha", description="First.")
    beta = await repo.create(slug="beta", title="Beta")
    # Archived project should not appear.
    archived = await repo.create(slug="gamma", title="Gamma")
    await repo.update(archived.id, status="archived")

    r = await client.get("/ui/_partials/projects")
    assert r.status_code == 200
    assert "Alpha" in r.text
    assert "Beta" in r.text
    assert "Gamma" not in r.text
    assert beta.slug in r.text


async def test_projects_partial_progress_calc(client, pool) -> None:
    project = await ProjectsRepo(pool).create(slug="x", title="X")
    repo = TasksRepo(pool)
    # 3 tasks, 1 done -> 33%.
    await repo.create(title="t1", project_id=project.id, status="open")
    await repo.create(title="t2", project_id=project.id, status="in_progress")
    await repo.create(title="t3", project_id=project.id, status="done")

    r = await client.get("/ui/_partials/projects")
    assert "1/3" in r.text
    assert "33%" in r.text


async def test_projects_partial_zero_total_no_division_error(
    client, pool
) -> None:
    await ProjectsRepo(pool).create(slug="empty", title="Empty")
    r = await client.get("/ui/_partials/projects")
    assert r.status_code == 200
    assert "0/0" in r.text
    assert "0%" in r.text


async def test_projects_partial_links_to_overview(client, pool) -> None:
    await ProjectsRepo(pool).create(slug="myproj", title="My Project")
    r = await client.get("/ui/_partials/projects")
    # Dashboard now links each project to its overview page; from there
    # users tab into Board / Objectives.
    assert 'href="/ui/projects/myproj"' in r.text


async def test_projects_partial_redirects_unauth(pool) -> None:
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        r = await c.get("/ui/_partials/projects")
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login"
