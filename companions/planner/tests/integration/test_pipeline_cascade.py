"""Integration tests for cascading unblock."""

from __future__ import annotations

import pytest

from planner.pipeline import Pipeline
from planner.repos.actors import ActorsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


async def _seed(pool) -> None:
    actors = ActorsRepo(pool)
    await actors.create(actor_id="alex", display_name="Alex", type="human")
    await actors.create(actor_id="claude", display_name="Claude", type="agent")


async def _task_in(pool, status, **kw) -> int:
    return (await TasksRepo(pool).create(title="t", status=status, **kw)).id


async def test_done_cascades_to_blockees(pool) -> None:
    """When a blocker reaches 'done', every task blocked_by it is unblocked."""
    await _seed(pool)
    blocker = await _task_in(pool, "pending_review")
    a = await _task_in(pool, "in_progress")
    b = await _task_in(pool, "awaiting_approval")
    pipeline = Pipeline(pool)

    await pipeline.block(
        a, actor="claude", reason="task_dependency", by_task_id=blocker
    )
    await pipeline.block(
        b, actor="alex", reason="task_dependency", by_task_id=blocker
    )

    # Blocker completes -> A and B should auto-unblock to their previous_status.
    await pipeline.verify(blocker, actor="alex", comment="accepted")

    a_row = await pool.fetchrow(
        "SELECT stage AS status, blocked_by, blocked_reason, previous_status "
        "FROM tasks WHERE id = $1",
        a,
    )
    b_row = await pool.fetchrow(
        "SELECT stage AS status, blocked_by, blocked_reason, previous_status "
        "FROM tasks WHERE id = $1",
        b,
    )
    assert a_row["status"] == "in_progress"
    assert a_row["blocked_by"] is None
    assert a_row["blocked_reason"] is None
    assert a_row["previous_status"] is None
    assert b_row["status"] == "awaiting_approval"
    assert b_row["blocked_by"] is None


async def test_cascade_logs_with_system_actor(pool) -> None:
    await _seed(pool)
    blocker = await _task_in(pool, "in_progress")
    a = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    await pipeline.block(
        a, actor="claude", reason="task_dependency", by_task_id=blocker
    )
    await pipeline.status_change(blocker, "done", actor="alex", force=True)

    last = await pool.fetchrow(
        "SELECT old_value, new_value, actor FROM task_log "
        "WHERE task_id = $1 AND action = 'status_changed' "
        "ORDER BY id DESC LIMIT 1",
        a,
    )
    assert last["old_value"] == "blocked"
    assert last["new_value"] == "in_progress"
    assert last["actor"] == "system"

    comments = await pool.fetch(
        "SELECT author, content FROM task_comments "
        "WHERE task_id = $1 ORDER BY id DESC LIMIT 1",
        a,
    )
    assert comments[0]["author"] == "system"
    assert f"#{blocker}" in comments[0]["content"]


async def test_rejected_blocker_cascades(pool) -> None:
    await _seed(pool)
    blocker = await _task_in(pool, "assessing")
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    await pipeline.block(
        target, actor="claude", reason="task_dependency", by_task_id=blocker
    )
    await pipeline.reject(blocker, actor="alex")

    row = await pool.fetchrow(
        "SELECT stage AS status FROM tasks WHERE id = $1", target
    )
    assert row["status"] == "in_progress"


async def test_escalated_blocker_cascades(pool) -> None:
    await _seed(pool)
    blocker = await _task_in(pool, "in_progress")
    target = await _task_in(pool, "awaiting_approval")
    pipeline = Pipeline(pool)

    await pipeline.block(
        target, actor="claude", reason="task_dependency", by_task_id=blocker
    )
    await pipeline.status_change(
        blocker, "escalated", actor="claude"
    )

    row = await pool.fetchrow(
        "SELECT stage AS status FROM tasks WHERE id = $1", target
    )
    assert row["status"] == "awaiting_approval"


async def test_cascade_recursive_chain(pool) -> None:
    """A blocked by B, B blocked by C. C done -> B unblocked, but B back to
    its previous status (in_progress, say). A stays blocked because B is no
    longer blocked but A's blocked_by still points at B (not yet done).

    Note: cascade is one level -- only direct blockees are unblocked, not
    transitive ones. This is intentional; a task is blocked because its
    blocker isn't done. Resolving B's blocker doesn't make B done.
    """
    await _seed(pool)
    c = await _task_in(pool, "in_progress")
    b = await _task_in(pool, "in_progress")
    a = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    await pipeline.block(b, actor="claude", reason="task_dependency", by_task_id=c)
    await pipeline.block(a, actor="claude", reason="task_dependency", by_task_id=b)

    await pipeline.status_change(c, "done", actor="alex", force=True)

    b_row = await pool.fetchrow("SELECT stage AS status FROM tasks WHERE id = $1", b)
    a_row = await pool.fetchrow("SELECT stage AS status FROM tasks WHERE id = $1", a)
    assert b_row["status"] == "in_progress"  # auto-unblocked
    assert a_row["status"] == "blocked"  # B isn't done yet, A stays blocked
