"""Integration tests for the task detail page."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from planner.app import create_app
from planner.auth import ApiKeysRepo
from planner.repos.actors import ActorsRepo
from planner.repos.ancillary import CommentsRepo, TagsRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(pool: asyncpg.Pool):
    await ActorsRepo(pool).create(
        actor_id="claude", display_name="Claude", type="agent"
    )
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


async def test_detail_renders_basic_fields(env) -> None:
    client, pool, project = env
    task = await TasksRepo(pool).create(
        title="My task",
        description="What to do.",
        project_id=project.id,
        priority="high",
        eta="2h",
    )
    r = await client.get(f"/ui/tasks/{task.id}")
    assert r.status_code == 200
    body = r.text
    assert "My task" in body
    assert "What to do." in body
    assert "high" in body
    assert "2h" in body
    assert f"#{task.id}" in body


async def test_detail_unknown_task_404(env) -> None:
    client, _, _ = env
    r = await client.get("/ui/tasks/99999")
    assert r.status_code == 404


async def test_detail_shows_only_allowed_transitions(env) -> None:
    client, pool, project = env
    task = await TasksRepo(pool).create(
        title="t", project_id=project.id, status="open"
    )
    r = await client.get(f"/ui/tasks/{task.id}")
    body = r.text
    # From open: every other stage (no matrix, 0010); assessing is gone.
    assert 'value="assessing"' not in body
    assert 'value="in_progress"' in body
    assert 'value="rejected"' in body
    assert 'value="blocked"' in body
    # Every stage is offered; the guards decide at submit time (0010).
    assert 'value="pending_review"' in body
    assert 'value="done"' in body


async def test_add_comment_updates_detail(env) -> None:
    client, pool, project = env
    task = await TasksRepo(pool).create(
        title="t", project_id=project.id, status="open"
    )
    r = await client.post(
        f"/ui/tasks/{task.id}/comment", data={"content": "Looks good"}
    )
    assert r.status_code == 200
    assert "Looks good" in r.text
    # Persisted in db
    rows = await CommentsRepo(pool).list_for_task(task.id)
    assert [c.content for c in rows] == ["Looks good"]


async def test_add_tag_updates_detail(env) -> None:
    client, pool, project = env
    task = await TasksRepo(pool).create(
        title="t", project_id=project.id, status="open"
    )
    r = await client.post(
        f"/ui/tasks/{task.id}/tag", data={"tag": "needs-review"}
    )
    assert r.status_code == 200
    assert "needs-review" in r.text
    assert "needs-review" in await TagsRepo(pool).list_for_task(task.id)


async def test_add_link_updates_detail(env) -> None:
    client, pool, project = env
    task = await TasksRepo(pool).create(
        title="t", project_id=project.id, status="open"
    )
    r = await client.post(
        f"/ui/tasks/{task.id}/link",
        data={"kind": "pr", "url": "https://example.com/pr/1", "label": "PR #1"},
    )
    assert r.status_code == 200
    assert "https://example.com/pr/1" in r.text
    assert "PR #1" in r.text


async def test_transition_button_advances_status(env) -> None:
    client, pool, project = env
    task = await TasksRepo(pool).create(
        title="t",
        project_id=project.id,
        status="open",
        assignee="claude",
    )
    r = await client.post(
        f"/ui/tasks/{task.id}/transition", data={"to": "in_progress"}
    )
    assert r.status_code == 200
    new_task = await TasksRepo(pool).get(task.id)
    assert new_task is not None
    assert new_task.status == "in_progress"


async def test_invalid_transition_shows_error(env) -> None:
    client, pool, project = env
    task = await TasksRepo(pool).create(
        title="t", project_id=project.id, status="open"
    )
    # open -> done without a report is refused
    r = await client.post(
        f"/ui/tasks/{task.id}/transition", data={"to": "done"}
    )
    assert r.status_code == 200
    assert "comment is required" in r.text
    new_task = await TasksRepo(pool).get(task.id)
    assert new_task is not None
    assert new_task.status == "open"
