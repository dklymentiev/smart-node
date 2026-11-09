"""Integration tests for recurring tasks (migration 0011).

Covers the fields on create / update / read, and Pipeline.recurrence_sweep:
a completed cycle is reopened and its assignee woken, an open one gets a
comment, a cancelled series ends, an unapproved one wakes nobody, and
next_run moves past now in whole intervals. Also the REST and CLI doors.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from planner import pipeline as pipeline_mod
from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo
from planner.pipeline import Pipeline
from planner.repos.actors import ActorsRepo
from planner.repos.tasks import TaskError, TasksRepo


pytestmark = pytest.mark.asyncio


@pytest.fixture
def wakes(monkeypatch):
    """Record the wakes the sweep sends instead of posting them."""
    sent: list[tuple[str, dict]] = []

    async def fake_status(pool, **kw):
        sent.append(("status", kw))

    async def fake_post(payload):
        sent.append(("post", payload))
        return 200

    monkeypatch.setattr(pipeline_mod, "_do_status_notify", fake_status)
    monkeypatch.setattr(pipeline_mod, "_post_notify", fake_post)
    return sent


async def _seed(pool) -> None:
    await ActorsRepo(pool).create(actor_id="sam", display_name="Sam", type="agent")


async def _due(pool, task_id: int, ago: str = "1 minute") -> None:
    await pool.execute(
        f"UPDATE tasks SET next_run = now() - INTERVAL '{ago}' WHERE id = $1", task_id
    )


async def _close(pool, task_id: int, outcome: str = "completed") -> None:
    await Pipeline(pool).status_change(
        task_id, "closed", actor="system", close_outcome=outcome,
        comment="Cycle report: checked, all green.",
    )


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


async def test_create_recurring_defaults_next_run_one_interval_ahead(pool) -> None:
    before = datetime.now(timezone.utc)
    task = await TasksRepo(pool).create(title="daily check", recur_interval="daily")
    assert task.recur_interval == "1 day"
    assert task.next_run is not None
    assert before + timedelta(hours=23) < task.next_run < before + timedelta(hours=25)


async def test_create_with_explicit_next_run(pool) -> None:
    when = datetime(2030, 1, 1, 7, 0, tzinfo=timezone.utc)
    task = await TasksRepo(pool).create(title="t", recur_interval="1 month", next_run=when)
    assert task.recur_interval == "1 mon"
    assert task.next_run == when


async def test_one_off_task_has_no_recurrence(pool) -> None:
    task = await TasksRepo(pool).create(title="once")
    assert task.recur_interval is None and task.next_run is None


async def test_next_run_without_interval_refused(pool) -> None:
    with pytest.raises(TaskError):
        await TasksRepo(pool).create(title="t", next_run="2030-01-01T00:00:00Z")
    task = await TasksRepo(pool).create(title="t")
    with pytest.raises(TaskError):
        await TasksRepo(pool).update(task.id, next_run="2030-01-01T00:00:00Z")


async def test_bad_interval_refused(pool) -> None:
    with pytest.raises(TaskError):
        await TasksRepo(pool).create(title="t", recur_interval="every so often")
    with pytest.raises(TaskError):
        await TasksRepo(pool).create(title="t", recur_interval="-1 day")


async def test_update_sets_changes_and_ends_recurrence(pool) -> None:
    repo = TasksRepo(pool)
    task = await repo.create(title="t")
    task = await repo.update(task.id, recur_interval="2 weeks")
    assert task.recur_interval == "14 days" and task.next_run is not None
    first = task.next_run
    # A new interval keeps the next cycle that is already scheduled.
    task = await repo.update(task.id, recur_interval="weekly")
    assert task.recur_interval == "7 days" and task.next_run == first
    task = await repo.update(task.id, recur_interval="off")
    assert task.recur_interval is None and task.next_run is None


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


async def test_sweep_reopens_completed_cycle_and_wakes_assignee(pool, wakes) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    task = await repo.create(title="daily", assignee="sam", status="in_progress",
                             recur_interval="1 day")
    await pool.execute("UPDATE tasks SET approved_at = now() WHERE id = $1", task.id)
    await _close(pool, task.id)
    await _due(pool, task.id)

    result = await Pipeline(pool).recurrence_sweep()

    assert result["reopened"] == [task.id]
    row = await pool.fetchrow(
        "SELECT status, close_outcome, completed_at, approved_at, next_run > now() AS ahead "
        "FROM tasks WHERE id = $1", task.id)
    assert row["status"] == "in_progress"
    assert row["close_outcome"] is None
    assert row["completed_at"] is None
    assert row["approved_at"] is not None
    assert row["ahead"] is True
    comments = await pool.fetch(
        "SELECT content FROM task_comments WHERE task_id = $1 ORDER BY id", task.id)
    assert comments[-1]["content"].startswith("Recurring cycle ")
    assert wakes == [("status", {"notify_target": "sam", "task_id": task.id,
                                 "to_status": "in_progress", "task_title": "daily"})]


async def test_sweep_comments_and_wakes_when_previous_cycle_open(pool, wakes) -> None:
    await _seed(pool)
    task = await TasksRepo(pool).create(title="daily", assignee="sam",
                                        status="in_progress", recur_interval="1 day")
    await _due(pool, task.id)

    result = await Pipeline(pool).recurrence_sweep()

    assert result["still_open"] == [task.id]
    content = await pool.fetchval(
        "SELECT content FROM task_comments WHERE task_id = $1 AND comment_type = 'recurrence'",
        task.id)
    assert "still open" in content
    assert wakes[0][0] == "post"
    assert wakes[0][1]["assignee"] == "sam"
    assert wakes[0][1]["event"] == "recurring_due"


async def test_sweep_ends_series_closed_as_cancelled(pool, wakes) -> None:
    task = await TasksRepo(pool).create(title="t", status="in_progress", recur_interval="1 day")
    await _close(pool, task.id, outcome="cancelled")
    await _due(pool, task.id)

    result = await Pipeline(pool).recurrence_sweep()

    assert result["ended"] == [task.id]
    row = await pool.fetchrow(
        "SELECT status, recur_interval, next_run FROM tasks WHERE id = $1", task.id)
    assert row["status"] == "closed"
    assert row["recur_interval"] is None and row["next_run"] is None
    assert wakes == []


async def test_sweep_skips_unapproved_task(pool, wakes) -> None:
    task = await TasksRepo(pool).create(title="t", status="awaiting_approval",
                                        recur_interval="1 day")
    await _due(pool, task.id)

    result = await Pipeline(pool).recurrence_sweep()

    assert result["unapproved"] == [task.id]
    assert await pool.fetchval("SELECT stage FROM tasks WHERE id = $1", task.id) == "awaiting_approval"
    assert await pool.fetchval("SELECT next_run > now() FROM tasks WHERE id = $1", task.id)
    assert wakes == []


async def test_sweep_after_downtime_opens_one_cycle_on_the_grid(pool, wakes) -> None:
    await _seed(pool)
    task = await TasksRepo(pool).create(title="t", assignee="sam", status="in_progress",
                                        recur_interval="1 hour")
    await _close(pool, task.id)
    # Seven and a half intervals missed.
    await pool.execute(
        "UPDATE tasks SET next_run = date_trunc('minute', now()) - INTERVAL '7 hours 30 minutes' "
        "WHERE id = $1", task.id)
    old = await pool.fetchval("SELECT next_run FROM tasks WHERE id = $1", task.id)

    result = await Pipeline(pool).recurrence_sweep()
    again = await Pipeline(pool).recurrence_sweep()

    assert result["reopened"] == [task.id]
    assert sum(len(v) for v in again.values()) == 0
    new = await pool.fetchval("SELECT next_run FROM tasks WHERE id = $1", task.id)
    assert new - old == timedelta(hours=8)
    n = await pool.fetchval(
        "SELECT count(*) FROM task_comments WHERE task_id = $1 AND content LIKE 'Recurring cycle %'",
        task.id)
    assert n == 1


async def test_sweep_leaves_tasks_not_yet_due(pool, wakes) -> None:
    await TasksRepo(pool).create(title="t", status="in_progress", recur_interval="1 day")
    await TasksRepo(pool).create(title="one-off", status="in_progress")
    result = await Pipeline(pool).recurrence_sweep()
    assert sum(len(v) for v in result.values()) == 0
    assert wakes == []


async def test_reopened_cycle_needs_a_report_to_close_again(pool, wakes) -> None:
    from planner.pipeline import PipelineError

    task = await TasksRepo(pool).create(title="t", status="in_progress", recur_interval="1 day")
    await _close(pool, task.id)
    await _due(pool, task.id)
    await Pipeline(pool).recurrence_sweep()
    with pytest.raises(PipelineError):
        await Pipeline(pool).status_change(task.id, "closed", actor="system",
                                           close_outcome="completed")
    await _close(pool, task.id)
    assert await pool.fetchval("SELECT completed_at IS NOT NULL FROM tasks WHERE id = $1", task.id)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    app = create_app()
    async with app.router.lifespan_context(app):
        _, plaintext = await ApiKeysRepo(app.state.pool).create(
            label="recur-test", actor_id="system")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"X-API-Key": plaintext}) as c:
            yield c


async def test_rest_create_patch_read_and_sweep(client, pool, wakes) -> None:
    r = await client.post("/api/v1/tasks", json={"title": "weekly report",
                                                 "recur_interval": "weekly"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["recur_interval"] == "7 days" and body["next_run"]
    tid = body["id"]

    r = await client.patch(f"/api/v1/tasks/{tid}",
                           json={"next_run": "2020-01-01T00:00:00+00:00"})
    assert r.status_code == 200, r.text
    assert r.json()["next_run"].startswith("2020-01-01")

    # Filed by a non-owner key: it waits for approval, so the cycle wakes nobody.
    r = await client.post("/api/v1/maintenance/recurrence-sweep")
    assert r.status_code == 200
    assert r.json()["actions"]["unapproved"] == [tid]
    assert wakes == []

    r = await client.patch(f"/api/v1/tasks/{tid}", json={"recur_interval": None})
    assert r.status_code == 200
    assert r.json()["recur_interval"] is None and r.json()["next_run"] is None

    r = await client.patch(f"/api/v1/tasks/{tid}", json={"recur_interval": "sometimes"})
    assert r.status_code in (400, 409, 422)
