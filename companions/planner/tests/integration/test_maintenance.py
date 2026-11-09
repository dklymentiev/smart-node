"""Integration tests for POST /api/v1/maintenance/staleness-sweep.

Proves the endpoint actually runs the sweep and the sweep acts on real data --
not just that the function exists. Each test checks a status change in the DB,
not only the response body.
"""

from __future__ import annotations

import os

import httpx
import pytest_asyncio

from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo
from planner.repos.tasks import TasksRepo


pytestmark = __import__("pytest").mark.asyncio


@pytest_asyncio.fixture
async def client(pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    app = create_app()
    async with app.router.lifespan_context(app):
        repo = ApiKeysRepo(app.state.pool)
        _, plaintext = await repo.create(label="maint-test", actor_id="system")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-API-Key": plaintext},
        ) as c:
            yield c


async def test_sweep_endpoint_returns_ok_when_nothing_to_do(client, pool) -> None:
    """A fresh DB with no stale tasks returns ok=True and all buckets empty."""
    r = await client.post("/api/v1/maintenance/staleness-sweep")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["total"] == 0
    for bucket in ("approval_reminder", "approval_escalate",
                   "orphan_unblock", "verification_escalate"):
        assert body["actions"][bucket] == []


async def test_sweep_endpoint_escalates_stale_awaiting_approval(client, pool) -> None:
    """The endpoint actually moves tasks: a 50h-old awaiting_approval is escalated."""
    repo = TasksRepo(pool)
    task = await repo.create(title="stale-approval", status="awaiting_approval")
    await pool.execute(
        "UPDATE tasks SET updated_at = now() - INTERVAL '50 hours' WHERE id = $1",
        task.id,
    )

    r = await client.post("/api/v1/maintenance/staleness-sweep")
    assert r.status_code == 200
    body = r.json()
    assert task.id in body["actions"]["approval_escalate"]
    assert body["total"] >= 1

    # Confirm in the DB that the status actually changed.
    new_status = await pool.fetchval("SELECT stage AS status FROM tasks WHERE id = $1", task.id)
    assert new_status == "escalated"


async def test_sweep_endpoint_unblocks_orphaned_task(client, pool) -> None:
    """A task blocked by a done task is auto-unblocked when the sweep runs."""
    from planner.pipeline import Pipeline

    repo = TasksRepo(pool)
    blocker = await repo.create(title="blocker", status="in_progress")
    target = await repo.create(title="blocked-target", status="in_progress")
    pipeline = Pipeline(pool)
    await pipeline.block(
        target.id, actor="system", reason="task_dependency", by_task_id=blocker.id
    )
    # Manually push the blocker to done, bypassing cascade (simulates a missed cascade).
    await pool.execute("UPDATE tasks SET status = 'done' WHERE id = $1", blocker.id)

    r = await client.post("/api/v1/maintenance/staleness-sweep")
    assert r.status_code == 200
    body = r.json()
    assert target.id in body["actions"]["orphan_unblock"]

    new_status = await pool.fetchval("SELECT stage AS status FROM tasks WHERE id = $1", target.id)
    assert new_status == "in_progress"
