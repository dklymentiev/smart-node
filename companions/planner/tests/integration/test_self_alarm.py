"""A repeating task an agent sets for ITSELF starts at once; everything else an agent
files still waits for the owner.

An agent's filing lands inert (awaiting approval, nobody assigned) because it can put
work on a colleague or spend someone else's time. An alarm for oneself does neither,
so "set yourself an alarm" must not need a click. The limits keep it from running away.
"""

from __future__ import annotations

import pytest

from planner.repos.actors import ActorsRepo
from planner.repos.tasks import TaskError, TasksRepo


pytestmark = pytest.mark.asyncio


async def _seed(pool) -> TasksRepo:
    actors = ActorsRepo(pool)
    await actors.create(actor_id="agent-a", display_name="Agent A", type="agent")
    await actors.create(actor_id="agent-b", display_name="Agent B", type="agent")
    return TasksRepo(pool)


async def test_an_alarm_for_oneself_starts_without_approval(pool) -> None:
    repo = await _seed(pool)
    t = await repo.create(
        title="check the build", assignee="agent-a", actor_id="agent-a",
        recur_interval="15 minutes",
    )
    assert t.assignee == "agent-a"
    assert t.requester == "agent-a"
    assert t.status == "in_progress"
    assert t.approved_at is None, "no owner approved it, so it must not claim one"
    assert t.proposed_assignee is None
    assert t.recur_interval is not None and t.next_run is not None


async def test_the_start_is_explained_in_the_task(pool) -> None:
    repo = await _seed(pool)
    t = await repo.create(title="x", assignee="agent-a", actor_id="agent-a",
                          recur_interval="1 hour")
    note = await pool.fetchval(
        "SELECT content FROM task_comments WHERE task_id = $1 ORDER BY id LIMIT 1", t.id)
    assert note and "without the owner's approval" in note


async def test_an_alarm_for_someone_else_still_waits_for_the_owner(pool) -> None:
    repo = await _seed(pool)
    t = await repo.create(
        title="check the build", assignee="agent-b", actor_id="agent-a",
        recur_interval="15 minutes",
    )
    assert t.status == "awaiting_approval"
    assert t.assignee is None
    assert t.proposed_assignee == "agent-b"


async def test_a_task_for_oneself_without_repeating_still_waits_for_the_owner(pool) -> None:
    repo = await _seed(pool)
    t = await repo.create(title="do a thing", assignee="agent-a", actor_id="agent-a")
    assert t.status == "awaiting_approval"
    assert t.assignee is None
    assert t.proposed_assignee == "agent-a"


async def test_an_alarm_tighter_than_the_limit_is_refused_in_words(pool) -> None:
    repo = await _seed(pool)
    with pytest.raises(TaskError, match="at most every 15 minutes"):
        await repo.create(title="x", assignee="agent-a", actor_id="agent-a",
                          recur_interval="5 minutes")
    assert await pool.fetchval("SELECT count(*) FROM tasks") == 0


async def test_the_limit_itself_is_allowed(pool) -> None:
    repo = await _seed(pool)
    t = await repo.create(title="x", assignee="agent-a", actor_id="agent-a",
                          recur_interval="15 minutes")
    assert t.status == "in_progress"


async def test_a_fourth_open_alarm_is_refused_and_a_closed_one_frees_a_place(pool) -> None:
    repo = await _seed(pool)
    made = [
        await repo.create(title=f"alarm {i}", assignee="agent-a", actor_id="agent-a",
                          recur_interval="1 hour")
        for i in range(3)
    ]
    with pytest.raises(TaskError, match="limit of 3"):
        await repo.create(title="alarm 4", assignee="agent-a", actor_id="agent-a",
                          recur_interval="1 hour")
    # another agent has its own three
    other = await repo.create(title="mine", assignee="agent-b", actor_id="agent-b",
                              recur_interval="1 hour")
    assert other.status == "in_progress"
    # ending one (closed) frees a place
    await pool.execute("UPDATE tasks SET status = 'closed', close_outcome = 'completed' "
                       "WHERE id = $1", made[0].id)
    again = await repo.create(title="alarm 4", assignee="agent-a", actor_id="agent-a",
                              recur_interval="1 hour")
    assert again.status == "in_progress"


async def test_the_owner_filing_is_unchanged(pool) -> None:
    repo = await _seed(pool)
    t = await repo.create(title="by the owner", assignee="agent-a", actor_id="admin",
                          recur_interval="5 minutes", status="in_progress")
    assert t.assignee == "agent-a" and t.status == "in_progress"


async def test_a_name_with_other_case_still_counts_as_oneself(pool) -> None:
    repo = await _seed(pool)
    t = await repo.create(title="x", assignee="Agent-A", actor_id="agent-a",
                          recur_interval="1 hour")
    assert t.assignee == "agent-a" and t.status == "in_progress"
