"""No task is in work without somebody to do it.

On a node in the field, in two weeks, 42 tasks sat in progress with nobody assigned:
nothing woke, nobody was answerable, and the owner kept finding them. A task that waits
in the queue with no doer named yet is harmless; a task that is being worked on with no
doer is not. The rule is enforced where the state changes, by a trigger in the database,
so that no door (REST, the Studio form, the planner's own MCP, a script, an approval
button, an agent moving a status) can bypass it, and an agent cannot forget it: a rule in
a prompt is one more thing to forget.

The properties:

  1. a task can be filed and can wait in the queue with no doer
  2. it cannot start (be created in progress, or moved to in_progress) with no doer,
     whichever way it is moved; the refusal says what to do
  3. the owner's approval of a task with no doer, named or proposed, is refused;
     with the filer's proposed doer it goes through and assigns that doer
  4. a task bound to a flow is the exception: the flow owns who works on it
  5. a working task cannot have its doer taken away
  6. a task that is already in progress with no doer (the 42) can still be edited and
     commented; only a change of state is refused
"""

from __future__ import annotations

import os

import asyncpg
import httpx
import pytest
import pytest_asyncio

from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo
from planner.pipeline import Pipeline
from planner.repos.actors import ActorsRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TaskError, TasksRepo

NEEDS = "needs a doer"


@pytest_asyncio.fixture(autouse=True)
async def _the_rule_is_on(pool):
    """conftest switches the rule off for the older tests; it is the subject here."""
    await pool.execute("ALTER TABLE tasks ENABLE TRIGGER trg_zz_require_doer")
    yield


@pytest_asyncio.fixture
async def client(pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    monkeypatch.setenv("PLANNER_OWNER_ACTOR_ID", "system")
    app = create_app()
    async with app.router.lifespan_context(app):
        _, plaintext = await ApiKeysRepo(app.state.pool).create(label="t", actor_id="system")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"X-API-Key": plaintext}) as c:
            yield c


async def _seed(pool) -> int:
    actors = ActorsRepo(pool)
    for name in ("filer", "doer"):
        await actors.create(actor_id=name, display_name=name.title(), type="agent")
    return (await ProjectsRepo(pool).create(slug="node", title="Node")).id


async def test_a_task_can_be_filed_and_wait_with_no_doer(pool) -> None:
    project = await _seed(pool)
    task = await TasksRepo(pool).create(title="t", project_id=project, requester="system")
    assert task.assignee is None and task.state == "queued"


async def test_a_task_cannot_be_created_in_progress_with_no_doer(pool) -> None:
    project = await _seed(pool)
    with pytest.raises(TaskError, match=NEEDS):
        await TasksRepo(pool).create(title="t", project_id=project, status="in_progress")


async def test_moving_it_to_in_progress_by_rest_is_refused_until_a_doer_is_named(client, pool) -> None:
    project = await _seed(pool)
    t = (await client.post("/api/v1/tasks", json={"title": "t", "project_id": project})).json()
    r = await client.patch(f"/api/v1/tasks/{t['id']}", json={"status": "in_progress"})
    assert r.status_code == 422, r.text
    assert NEEDS in r.json()["detail"] and "assignee" in r.json()["detail"]
    ok = await client.patch(f"/api/v1/tasks/{t['id']}", json={"status": "in_progress", "assignee": "doer"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["assignee"] == "doer"


async def test_the_pipeline_cannot_start_work_with_no_doer_either(pool) -> None:
    project = await _seed(pool)
    task = await TasksRepo(pool).create(title="t", project_id=project, requester="system")
    with pytest.raises(asyncpg.CheckViolationError, match=NEEDS):
        await Pipeline(pool).status_change(task.id, "in_progress", actor="system")


async def test_approval_needs_a_doer_named_or_proposed(pool) -> None:
    project = await _seed(pool)
    bare = await TasksRepo(pool).create(title="t", project_id=project, actor_id="filer")
    with pytest.raises(asyncpg.CheckViolationError, match=NEEDS):
        await Pipeline(pool).approve(bare.id, actor="system")
    proposed = await TasksRepo(pool).create(title="t", project_id=project, actor_id="filer", assignee="doer")
    approved = await Pipeline(pool).approve(proposed.id, actor="system")
    assert approved.state == "in_progress" and approved.assignee == "doer"


async def test_a_task_bound_to_a_flow_may_start_with_no_named_doer(pool) -> None:
    project = await _seed(pool)
    task = await TasksRepo(pool).create(title="page", project_id=project, workflow_id=1, status="in_progress")
    assert task.state == "in_progress" and task.assignee is None


async def test_a_working_task_cannot_lose_its_doer(client, pool) -> None:
    project = await _seed(pool)
    t = (await client.post("/api/v1/tasks", json={"title": "t", "project_id": project,
                                                  "assignee": "doer", "status": "in_progress"})).json()
    r = await client.patch(f"/api/v1/tasks/{t['id']}", json={"assignee": None})
    assert r.status_code in (422, 200)
    after = (await client.get(f"/api/v1/tasks/{t['id']}")).json()
    assert after["assignee"] == "doer", "the doer of a working task was taken away"


async def test_a_task_already_in_progress_with_no_doer_can_still_be_edited(pool) -> None:
    project = await _seed(pool)
    task = await TasksRepo(pool).create(title="old", project_id=project, workflow_id=1, status="in_progress")
    # the history is not rewritten: the task was already like this
    await pool.execute("UPDATE tasks SET workflow_id = NULL WHERE id = $1", task.id)
    await pool.execute("UPDATE tasks SET title = 'renamed' WHERE id = $1", task.id)
    assert await pool.fetchval("SELECT title FROM tasks WHERE id = $1", task.id) == "renamed"


async def test_a_recurring_task_with_no_doer_waits_instead_of_opening(pool) -> None:
    project = await _seed(pool)
    repo = TasksRepo(pool)
    task = await repo.create(title="weekly", project_id=project, requester="system",
                             recur_interval="1 week")
    await pool.execute(
        "UPDATE tasks SET status = 'closed', close_outcome = 'completed', next_run = now() - interval '1 hour' "
        "WHERE id = $1", task.id)
    result = await Pipeline(pool).recurrence_sweep()
    assert task.id in result.get("no_doer", [])
    assert await pool.fetchval("SELECT status FROM tasks WHERE id = $1", task.id) == "closed"
    note = await pool.fetchval("SELECT content FROM task_comments WHERE task_id = $1 ORDER BY id DESC LIMIT 1", task.id)
    assert "nobody is assigned" in note
