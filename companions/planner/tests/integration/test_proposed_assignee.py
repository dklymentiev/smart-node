"""The doer an agent names when it files a task survives until the owner approves it.

A node in the field (2026-10-21): nine tasks an agent filed for colleagues, each
carrying "Assignee=<name>" in its description because the assignee field was thrown
away at creation (on purpose: an agent must not hand work to a colleague on its own
authority). The owner approved them one by one and nobody woke: with no assignee
there was nobody to wake, and the doer in the description was text nothing could
read. They stood nine in_progress with no assignee until a person assigned each.

The properties:

  1. a task an agent files for a colleague waits unassigned (nobody is woken), and
     the doer it named is kept as `proposed_assignee`
  2. the owner's approval hands the task to that doer, who is woken the usual way
  3. the owner's own choice at approval wins over the proposal
  4. a proposal that names nobody alive is ignored: the task stays unassigned
  5. a task the owner files himself is assigned at once, as before, with no proposal
  6. an agent that names no doer files an ordinary unassigned task
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
    for name in ("filer", "doer", "other"):
        await actors.create(actor_id=name, display_name=name.title(), type="agent")
    await actors.create(actor_id="retired", display_name="Retired", type="agent")
    await pool.execute("UPDATE actors SET active = false WHERE id = 'retired'")
    return (await ProjectsRepo(pool).create(slug="node", title="Node")).id


async def _wake_targets(pool, task_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT notify_target FROM task_notifications WHERE task_id = $1 AND notify_target IS NOT NULL",
        task_id)
    return [r["notify_target"] for r in rows]


async def _filed(pool, project_id: int, **kw):
    return await TasksRepo(pool).create(
        title="audit", description="do it", project_id=project_id, actor_id="filer", **kw)


async def test_an_agent_filed_task_keeps_the_named_doer_but_wakes_nobody(pool) -> None:
    project = await _seed(pool)
    task = await _filed(pool, project, assignee="doer")
    assert task.status == "awaiting_approval"
    assert task.assignee is None
    assert task.proposed_assignee == "doer"
    assert "doer" not in await _wake_targets(pool, task.id)


async def test_approval_hands_the_task_to_the_named_doer_and_wakes_it(pool) -> None:
    project = await _seed(pool)
    task = await _filed(pool, project, assignee="doer")
    approved = await Pipeline(pool).approve(task.id, actor="admin")
    assert approved.status == "in_progress"
    assert approved.assignee == "doer"
    assert approved.approved_at is not None
    assert "doer" in await _wake_targets(pool, task.id)


async def test_the_decision_route_does_the_same(pool) -> None:
    project = await _seed(pool)
    task = await _filed(pool, project, assignee="doer")
    approved = await Pipeline(pool).decide(task.id, "approve", actor="admin", comment="DECISION: approve")
    assert approved.assignee == "doer"


async def test_the_owners_own_choice_at_approval_wins(pool) -> None:
    project = await _seed(pool)
    task = await _filed(pool, project, assignee="doer")
    approved = await Pipeline(pool).decide(
        task.id, "approve", actor="admin", comment="DECISION: approve", assignee="other")
    assert approved.assignee == "other"


async def test_a_proposal_naming_nobody_alive_is_ignored(pool) -> None:
    project = await _seed(pool)
    for name in ("retired", "nobody-by-that-name"):
        task = await _filed(pool, project, assignee=name)
        approved = await Pipeline(pool).approve(task.id, actor="admin")
        assert approved.status == "in_progress"
        assert approved.assignee is None, "approval assigned %r" % name


async def test_a_task_the_owner_files_is_assigned_at_once(pool) -> None:
    project = await _seed(pool)
    task = await TasksRepo(pool).create(
        title="audit", description="do it", project_id=project, actor_id="admin", assignee="doer")
    assert task.assignee == "doer"
    assert task.proposed_assignee is None


async def test_an_agent_that_names_no_doer_files_an_ordinary_task(pool) -> None:
    project = await _seed(pool)
    task = await _filed(pool, project)
    assert task.assignee is None and task.proposed_assignee is None
    approved = await Pipeline(pool).approve(task.id, actor="admin")
    assert approved.assignee is None
