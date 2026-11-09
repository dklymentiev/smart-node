"""Integration tests for block / unblock."""

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


async def _task_in(pool, status, **kw) -> int:
    return (await TasksRepo(pool).create(title="t", status=status, **kw)).id


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------


async def test_block_task_dependency_snapshots_status(pool) -> None:
    await _seed(pool)
    blocker = await _task_in(pool, "in_progress")
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    task = await pipeline.block(
        target,
        actor="claude",
        reason="task_dependency",
        by_task_id=blocker,
    )
    assert task.status == "blocked"
    assert task.previous_status == "in_progress"
    assert task.blocked_by == blocker
    assert task.blocked_reason == "task_dependency"
    assert task.external_detail is None


async def test_block_external_approval_requires_detail(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "awaiting_approval")
    pipeline = Pipeline(pool)

    task = await pipeline.block(
        target,
        actor="alex",
        reason="external_approval",
        external_detail="Waiting for legal sign-off",
    )
    assert task.status == "blocked"
    assert task.blocked_reason == "external_approval"
    assert task.external_detail == "Waiting for legal sign-off"
    assert task.blocked_by is None


async def test_block_external_approval_without_detail_fails(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "awaiting_approval")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="requires external_detail"):
        await pipeline.block(
            target, actor="alex", reason="external_approval"
        )


async def test_block_task_dependency_without_blocker_fails(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="requires by_task_id"):
        await pipeline.block(
            target, actor="claude", reason="task_dependency"
        )


async def test_block_task_dependency_with_external_detail_fails(pool) -> None:
    await _seed(pool)
    blocker = await _task_in(pool, "in_progress")
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="does not take external_detail"):
        await pipeline.block(
            target,
            actor="claude",
            reason="task_dependency",
            by_task_id=blocker,
            external_detail="why",
        )


async def test_block_invalid_reason(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="invalid reason"):
        await pipeline.block(target, actor="claude", reason="bogus")


async def test_block_self_referential_fails(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="cannot block itself"):
        await pipeline.block(
            target,
            actor="claude",
            reason="task_dependency",
            by_task_id=target,
        )


async def test_block_already_blocked_fails(pool) -> None:
    await _seed(pool)
    blocker = await _task_in(pool, "in_progress")
    target = await _task_in(pool, "blocked")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="already blocked"):
        await pipeline.block(
            target,
            actor="claude",
            reason="task_dependency",
            by_task_id=blocker,
        )


async def test_block_cycle_detection(pool) -> None:
    """A blocks B blocks C; trying to block A by C must fail."""
    await _seed(pool)
    a = await _task_in(pool, "in_progress")
    b = await _task_in(pool, "in_progress")
    c = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    await pipeline.block(b, actor="claude", reason="task_dependency", by_task_id=a)
    await pipeline.block(c, actor="claude", reason="task_dependency", by_task_id=b)

    with pytest.raises(PipelineError, match="cycle"):
        await pipeline.block(
            a, actor="claude", reason="task_dependency", by_task_id=c
        )


async def test_block_unknown_blocker(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="blocker task .* not found"):
        await pipeline.block(
            target,
            actor="claude",
            reason="task_dependency",
            by_task_id=999,
        )


# ---------------------------------------------------------------------------
# unblock
# ---------------------------------------------------------------------------


async def test_unblock_restores_previous_status(pool) -> None:
    await _seed(pool)
    blocker = await _task_in(pool, "in_progress")
    target = await _task_in(pool, "awaiting_approval")
    pipeline = Pipeline(pool)

    await pipeline.block(
        target, actor="claude", reason="task_dependency", by_task_id=blocker
    )
    task = await pipeline.unblock(target, actor="claude")

    assert task.status == "awaiting_approval"
    assert task.previous_status is None
    assert task.blocked_by is None
    assert task.blocked_reason is None
    assert task.external_detail is None


async def test_unblock_falls_back_to_open(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "blocked")  # no previous_status snapshot
    pipeline = Pipeline(pool)

    task = await pipeline.unblock(target, actor="claude")
    assert task.status == "open"


async def test_unblock_requires_blocked_status(pool) -> None:
    await _seed(pool)
    target = await _task_in(pool, "in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="expected 'blocked'"):
        await pipeline.unblock(target, actor="claude")


async def test_unblock_missing_task(pool) -> None:
    await _seed(pool)
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="not found"):
        await pipeline.unblock(999, actor="claude")
