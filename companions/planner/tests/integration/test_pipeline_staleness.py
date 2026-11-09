"""Integration tests for check_conflicts and staleness_sweep."""

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


# ---------------------------------------------------------------------------
# check_conflicts
# ---------------------------------------------------------------------------


async def test_check_conflicts_finds_overlap_in_active_tasks(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    await repo.create(
        title="A",
        scope="src/planner/pipeline.py, tests/test_pipeline.py",
        status="in_progress",
    )
    await repo.create(
        title="B",
        scope="docs/README.md",
        status="in_progress",
    )
    pipeline = Pipeline(pool)

    conflicts = await pipeline.check_conflicts("src/planner/pipeline.py")
    assert len(conflicts) == 1
    assert conflicts[0].title == "A"


async def test_check_conflicts_substring_overlap(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    await repo.create(
        title="A",
        scope="src/planner/repos/tasks.py",
        status="in_progress",
    )
    pipeline = Pipeline(pool)

    # querying for the directory should overlap with the file.
    conflicts = await pipeline.check_conflicts("src/planner/repos")
    assert len(conflicts) == 1


async def test_check_conflicts_ignores_terminal_tasks(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    await repo.create(
        title="finished", scope="src/planner/x.py", status="done"
    )
    await repo.create(
        title="rejected", scope="src/planner/x.py", status="rejected"
    )
    pipeline = Pipeline(pool)

    conflicts = await pipeline.check_conflicts("src/planner/x.py")
    assert conflicts == []


async def test_check_conflicts_excludes_self(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    own = await repo.create(
        title="own", scope="src/x.py", status="in_progress"
    )
    pipeline = Pipeline(pool)

    conflicts = await pipeline.check_conflicts(
        "src/x.py", exclude_task_id=own.id
    )
    assert conflicts == []


async def test_check_conflicts_blank_scope_returns_empty(pool) -> None:
    await _seed(pool)
    pipeline = Pipeline(pool)
    assert await pipeline.check_conflicts("") == []
    assert await pipeline.check_conflicts("   ,  ,") == []


# ---------------------------------------------------------------------------
# staleness_sweep
# ---------------------------------------------------------------------------


async def test_sweep_idle_returns_empty_buckets(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    await repo.create(title="fresh", status="awaiting_approval")
    pipeline = Pipeline(pool)

    out = await pipeline.staleness_sweep()
    assert out == {
        "approval_reminder": [],
        "approval_escalate": [],
        "orphan_unblock": [],
        "verification_escalate": [],
    }


async def test_sweep_does_not_remind_or_escalate_human_owned_in_progress(pool) -> None:
    await _seed(pool)
    task = await TasksRepo(pool).create(
        title="Waiting for signed HITL decision", status="in_progress", assignee="alex"
    )
    await pool.execute(
        "UPDATE tasks SET updated_at = now() - INTERVAL '72 hours' WHERE id = $1",
        task.id,
    )

    out = await Pipeline(pool).staleness_sweep()

    assert all(not ids for ids in out.values())
    row = await pool.fetchrow("SELECT stage AS status, assignee FROM tasks WHERE id = $1", task.id)
    assert row["status"] == "in_progress"
    assert row["assignee"] == "alex"


async def test_sweep_reminder_after_24h(pool) -> None:
    await _seed(pool)
    task = await TasksRepo(pool).create(title="aging", status="awaiting_approval")
    # Backdate updated_at by 30h.
    await pool.execute(
        "UPDATE tasks SET updated_at = now() - INTERVAL '30 hours' "
        "WHERE id = $1",
        task.id,
    )
    pipeline = Pipeline(pool)

    out = await pipeline.staleness_sweep()
    assert out["approval_reminder"] == [task.id]
    assert out["approval_escalate"] == []

    # Idempotent: running again should not re-add the reminder.
    out2 = await pipeline.staleness_sweep()
    assert out2["approval_reminder"] == []


async def test_sweep_escalate_after_48h(pool) -> None:
    await _seed(pool)
    task = await TasksRepo(pool).create(title="stale", status="awaiting_approval")
    await pool.execute(
        "UPDATE tasks SET updated_at = now() - INTERVAL '50 hours' "
        "WHERE id = $1",
        task.id,
    )
    pipeline = Pipeline(pool)

    out = await pipeline.staleness_sweep()
    assert out["approval_escalate"] == [task.id]

    new_status = await pool.fetchval(
        "SELECT stage AS status FROM tasks WHERE id = $1", task.id
    )
    assert new_status == "escalated"


async def test_sweep_orphan_unblock(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    blocker = await repo.create(title="b", status="in_progress")
    target = await repo.create(title="t", status="in_progress")
    pipeline = Pipeline(pool)

    await pipeline.block(
        target.id, actor="claude", reason="task_dependency", by_task_id=blocker.id
    )
    # Manually transition blocker to done bypassing the cascade
    # (simulate the case where cascade missed for some reason).
    await pool.execute(
        "UPDATE tasks SET status = 'done' WHERE id = $1", blocker.id
    )

    out = await pipeline.staleness_sweep()
    assert out["orphan_unblock"] == [target.id]

    new_status = await pool.fetchval(
        "SELECT stage AS status FROM tasks WHERE id = $1", target.id
    )
    assert new_status == "in_progress"


async def test_sweep_verification_loop_fallback(pool) -> None:
    await _seed(pool)
    task = await TasksRepo(pool).create(
        title="stuck", status="pending_review"
    )
    await pool.execute(
        "UPDATE tasks SET verification_attempts = 2 WHERE id = $1", task.id
    )
    pipeline = Pipeline(pool)

    out = await pipeline.staleness_sweep()
    assert out["verification_escalate"] == [task.id]

    new_status = await pool.fetchval(
        "SELECT stage AS status FROM tasks WHERE id = $1", task.id
    )
    assert new_status == "escalated"
