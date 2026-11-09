"""Integration tests for bounce-back + verification limit."""

from __future__ import annotations

import pytest

from planner.pipeline import Pipeline, PipelineError
from planner.repos.actors import ActorsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


async def _seed(pool) -> None:
    actors = ActorsRepo(pool)
    await actors.create(actor_id="alex", display_name="Alex", type="human")
    await actors.create(actor_id="claude", display_name="Claude", type="agent")


async def _task_in(pool, status, **kwargs) -> int:
    task = await TasksRepo(pool).create(title="t", status=status, **kwargs)
    return task.id


async def test_first_bounce_back_increments_and_returns_to_in_progress(pool) -> None:
    await _seed(pool)
    task_id = await _task_in(pool, "pending_review")
    pipeline = Pipeline(pool)

    task = await pipeline.bounce_back(
        task_id, actor="alex", reason="Edge case missed"
    )
    assert task.status == "in_progress"
    assert task.verification_attempts == 1

    rows = await pool.fetch(
        "SELECT content, author FROM task_comments WHERE task_id = $1", task_id
    )
    assert [(r["content"], r["author"]) for r in rows] == [
        ("Edge case missed", "alex")
    ]


async def test_second_bounce_back_auto_escalates(pool) -> None:
    await _seed(pool)
    task_id = await _task_in(pool, "pending_review")
    pipeline = Pipeline(pool)

    # First bounce -> in_progress with attempts=1
    await pipeline.bounce_back(
        task_id, actor="alex", reason="missing tests"
    )
    # Worker resubmits
    await TasksRepo(pool).update(task_id, status="pending_review")
    # Second bounce -> auto-escalate
    task = await pipeline.bounce_back(
        task_id, actor="alex", reason="still missing"
    )

    assert task.status == "escalated"
    assert task.verification_attempts == 2

    # Last status_change must be authored by 'system' (auto-escalation).
    last_log = await pool.fetchrow(
        "SELECT old_value, new_value, actor FROM task_log "
        "WHERE task_id = $1 AND action = 'status_changed' "
        "ORDER BY id DESC LIMIT 1",
        task_id,
    )
    assert last_log["old_value"] == "pending_review"
    assert last_log["new_value"] == "escalated"
    assert last_log["actor"] == "system"

    # Comment exists from system.
    rows = await pool.fetch(
        "SELECT author, content FROM task_comments WHERE task_id = $1 "
        "ORDER BY id DESC LIMIT 1",
        task_id,
    )
    assert rows[0]["author"] == "system"
    assert "Auto-escalated" in rows[0]["content"]
    assert "still missing" in rows[0]["content"]


async def test_bounce_back_requires_pending_review(pool) -> None:
    await _seed(pool)
    task_id = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="expected 'pending_review'"):
        await pipeline.bounce_back(task_id, actor="alex", reason="x")


async def test_bounce_back_requires_reason(pool) -> None:
    await _seed(pool)
    task_id = await _task_in(pool, "pending_review")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="reason must not be blank"):
        await pipeline.bounce_back(task_id, actor="alex", reason="")


async def test_bounce_back_missing_task(pool) -> None:
    await _seed(pool)
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="not found"):
        await pipeline.bounce_back(999, actor="alex", reason="x")
