"""The reviewer defaulting on ``-> pending_review``.

The default exists so a task submitted for review still reaches somebody. It
must never overwrite a reviewer that was appointed in advance: the reviewer is
the only person the planner lets close a task, so defaulting over an appointed
one moves that authority to whoever filed the task -- silently, with no log
line and no error. That is what happened on
"""

from __future__ import annotations

import pytest

from planner.pipeline import Pipeline
from planner.repos.actors import ActorsRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


async def _seed(pool) -> int:
    actors = ActorsRepo(pool)
    await actors.create(actor_id="admin", display_name="Admin", type="human")
    await actors.create(actor_id="developer", display_name="Developer", type="agent")
    await actors.create(actor_id="architect", display_name="Architect", type="agent")
    await actors.create(actor_id="pm", display_name="PM", type="agent")
    return (await ProjectsRepo(pool).create(slug="node", title="Node")).id


async def _notify_targets(pool, task_id: int) -> list[tuple[str, str | None]]:
    rows = await pool.fetch(
        "SELECT event_type, notify_target FROM task_notifications "
        "WHERE task_id = $1 ORDER BY id",
        task_id,
    )
    return [(r["event_type"], r["notify_target"]) for r in rows]


async def test_appointed_reviewer_survives_the_transition(pool) -> None:
    """the reviewer set before the transition is the reviewer after it."""
    project_id = await _seed(pool)
    repo = TasksRepo(pool)
    task = await repo.create(
        title="identity 1/15",
        project_id=project_id,
        status="in_progress",
        assignee="developer",
        requester="admin",
    )
    await repo.update(task.id, reviewer="architect")

    before = await repo.get(task.id)
    assert before is not None and before.reviewer == "architect"

    after = await Pipeline(pool).review(task.id, actor="developer")

    assert after.reviewer == "architect"
    # And the wake goes to the appointed reviewer, not to the requester.
    assert ("status:pending_review", "architect") in await _notify_targets(pool, task.id)


async def test_requester_is_the_default_when_no_reviewer_is_set(pool) -> None:
    """Unchanged behaviour: a task with no reviewer still gets the requester."""
    project_id = await _seed(pool)
    task = await TasksRepo(pool).create(
        title="no reviewer anywhere",
        project_id=project_id,
        status="in_progress",
        assignee="developer",
        requester="admin",
    )

    after = await Pipeline(pool).review(task.id, actor="developer")

    assert after.reviewer == "admin"


async def test_reviewer_in_the_transition_wins_over_both(pool) -> None:
    """An explicit reviewer in the transition payload still overrides the task's."""
    project_id = await _seed(pool)
    repo = TasksRepo(pool)
    task = await repo.create(
        title="reassigned mid-flight",
        project_id=project_id,
        status="in_progress",
        assignee="developer",
        requester="admin",
    )
    await repo.update(task.id, reviewer="architect")

    after = await Pipeline(pool).status_change(
        task.id,
        "pending_review",
        actor="developer",
        extra_fields={"reviewer": "pm"},
    )

    assert after.reviewer == "pm"
