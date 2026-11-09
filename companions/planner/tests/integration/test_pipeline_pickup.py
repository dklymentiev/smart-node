"""Integration tests for pickup_next."""

from __future__ import annotations

import pytest

from planner.pipeline import Pipeline
from planner.repos.actors import ActorsRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


async def _seed(pool) -> None:
    actors = ActorsRepo(pool)
    await actors.create(actor_id="alex", display_name="Alex", type="human")
    await actors.create(actor_id="claude", display_name="Claude", type="agent")
    await actors.create(actor_id="scriber", display_name="Scriber", type="agent")


async def test_pickup_returns_none_when_nothing_to_do(pool) -> None:
    await _seed(pool)
    pipeline = Pipeline(pool)
    assert await pipeline.pickup_next(agent="claude") is None


async def test_pickup_resumes_own_in_progress_first(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    await repo.create(title="open-claude", assignee="claude", status="open")
    in_prog = await repo.create(
        title="in-progress-claude", assignee="claude", status="in_progress"
    )
    await repo.create(title="open-anyone", status="open")
    pipeline = Pipeline(pool)

    result = await pipeline.pickup_next(agent="claude")
    assert result is not None
    task, reason = result
    assert task.id == in_prog.id
    assert reason == "resume"


async def test_pickup_resumes_pending_review_too(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    pr = await repo.create(
        title="needs-review", assignee="claude", status="pending_review"
    )
    pipeline = Pipeline(pool)

    result = await pipeline.pickup_next(agent="claude")
    assert result is not None
    assert result[0].id == pr.id
    assert result[1] == "resume"


async def test_pickup_falls_back_to_assigned_open(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    assigned = await repo.create(
        title="assigned", assignee="claude", status="open"
    )
    await repo.create(title="unassigned", status="open")
    pipeline = Pipeline(pool)

    result = await pipeline.pickup_next(agent="claude")
    assert result is not None
    assert result[0].id == assigned.id
    assert result[1] == "assigned"


async def test_pickup_falls_back_to_unassigned_open(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    other = await repo.create(
        title="for-other", assignee="scriber", status="open"
    )
    free = await repo.create(title="free", status="open")
    pipeline = Pipeline(pool)

    result = await pipeline.pickup_next(agent="claude")
    assert result is not None
    assert result[0].id == free.id
    assert result[1] == "unassigned"
    # Don't accidentally pick up scriber's task.
    assert result[0].id != other.id


async def test_pickup_priority_desc_within_group(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    await repo.create(title="low", priority="low", status="open")
    await repo.create(title="medium", priority="medium", status="open")
    crit = await repo.create(title="critical", priority="critical", status="open")
    await repo.create(title="high", priority="high", status="open")
    pipeline = Pipeline(pool)

    result = await pipeline.pickup_next(agent="claude")
    assert result is not None
    assert result[0].id == crit.id


async def test_pickup_eta_asc_then_age_asc(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    # All same priority -- order by eta then age.
    long_eta = await repo.create(
        title="long", priority="medium", eta="4h", status="open"
    )
    short_eta = await repo.create(
        title="short", priority="medium", eta="30m", status="open"
    )
    no_eta = await repo.create(
        title="no-eta", priority="medium", status="open"
    )
    pipeline = Pipeline(pool)

    result = await pipeline.pickup_next(agent="claude")
    assert result is not None
    # short_eta wins (eta '30m' < '4h' < NULLs-last)
    assert result[0].id == short_eta.id

    # Different lexicographic note: '30m' < '4h' as strings, yes.
    assert long_eta.id != no_eta.id  # silence unused warnings


async def test_pickup_filters_by_project(pool) -> None:
    await _seed(pool)
    p1 = await ProjectsRepo(pool).create(slug="p1", title="P1")
    p2 = await ProjectsRepo(pool).create(slug="p2", title="P2")
    repo = TasksRepo(pool)
    await repo.create(title="t1", project_id=p1.id, status="open")
    target = await repo.create(title="t2", project_id=p2.id, status="open")
    pipeline = Pipeline(pool)

    result = await pipeline.pickup_next(agent="claude", project_id=p2.id)
    assert result is not None
    assert result[0].id == target.id


async def test_pickup_skips_blocked_and_done(pool) -> None:
    await _seed(pool)
    repo = TasksRepo(pool)
    await repo.create(title="blocked", status="blocked")
    await repo.create(title="done", status="done")
    pipeline = Pipeline(pool)

    assert await pipeline.pickup_next(agent="claude") is None
