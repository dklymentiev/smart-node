"""Integration tests for the objective tree page."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from planner.app import create_app
from planner.auth import ApiKeysRepo
from planner.repos.objectives import ObjectivesRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(pool: asyncpg.Pool):
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        _, key = await ApiKeysRepo(pool).create(label="t", actor_id="system")
        c.cookies.set("planner_key", key)
        yield c, pool, project


async def test_objectives_empty_state(env) -> None:
    client, _, _ = env
    r = await client.get("/ui/projects/oss/objectives")
    assert r.status_code == 200
    assert "No objectives yet" in r.text


async def test_objectives_indented_tree_with_progress(env) -> None:
    client, pool, project = env
    obj_repo = ObjectivesRepo(pool)
    strat = await obj_repo.create(
        project_id=project.id, title="Top strategy", type="strategy"
    )
    road = await obj_repo.create(
        project_id=project.id, title="Phase 1", type="roadmap", parent_id=strat.id
    )
    mile = await obj_repo.create(
        project_id=project.id,
        title="First milestone",
        type="milestone",
        parent_id=road.id,
    )

    repo = TasksRepo(pool)
    await repo.create(
        title="t1", project_id=project.id, objective_id=mile.id, status="done"
    )
    await repo.create(
        title="t2", project_id=project.id, objective_id=mile.id, status="open"
    )

    r = await client.get("/ui/projects/oss/objectives")
    body = r.text
    assert r.status_code == 200
    assert "Top strategy" in body
    assert "Phase 1" in body
    assert "First milestone" in body
    # Each level should appear; cumulative progress on roots should be 1/2.
    assert "1 / 2" in body
    assert "50%" in body
    # Type labels rendered
    assert "strategy" in body
    assert "roadmap" in body
    assert "milestone" in body


async def test_objectives_unknown_project_404(env) -> None:
    client, _, _ = env
    r = await client.get("/ui/projects/nope/objectives")
    assert r.status_code == 404


async def test_objectives_progress_zero_no_div(env) -> None:
    client, pool, project = env
    await ObjectivesRepo(pool).create(
        project_id=project.id, title="Untouched", type="goal"
    )
    r = await client.get("/ui/projects/oss/objectives")
    assert "0 / 0" in r.text
    assert "0%" in r.text


async def test_objectives_redirects_unauth(pool, env) -> None:
    # Reuse fixture-built project but skip the auth cookie.
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        r = await c.get("/ui/projects/oss/objectives")
        assert r.status_code == 303
