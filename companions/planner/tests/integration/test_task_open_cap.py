"""What the agent task cap counts, and who it does not apply to.

The open-task cap stops an agent filling the board with proposals nobody has read. Two
things were wrong with it as shipped in 1.12.42, and both are fixed here.

1. It counted every live task the agent had ever filed, including the ones the
   owner had already approved, taken himself or handed to someone else. So the
   better an agent worked, the less it was allowed to file. On example-node one
   agent held eleven live tasks, ten of them approved, and could file nothing.
   The quota is now what the agent has PROPOSED and the owner has not answered,
   which is exactly `awaiting_approval`.

2. It applied to the runner, which is not an agent but the service that files
   on the operator's behalf. Its key already held 120+ tasks, so the node had
   no filing door left.

The exemption is keyed on the runner ACTOR and nothing wider: the cap counts by
the resolved actor_id and a delegating key records the action under the person
it acts for, so a class-wide exemption would lift the cap from every delegated
agent call. The last test holds that line.
"""

from __future__ import annotations

import pytest

from planner.repos import tasks as tasks_mod
from planner.repos.tasks import TaskError, TasksRepo

# Read through getattr so this same file runs against the released code, where
# the constant does not exist yet and the cap was the literal 2. A test that
# cannot even import on the baseline proves nothing: it has to fail on
# BEHAVIOUR there, which is what the open-task cap requires.
_OPEN_TASK_CAP = getattr(tasks_mod, "_OPEN_TASK_CAP", 2)


async def _seed_actor(pool, actor_id: str, actor_type: str) -> None:
    await pool.execute(
        "INSERT INTO actors (id, display_name, type) VALUES ($1, $1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        actor_id, actor_type,
    )


async def _propose(repo, actor_id: str, n: int) -> list:
    return [await repo.create(title=f"{actor_id} proposal {i}", actor_id=actor_id)
            for i in range(n)]


@pytest.mark.asyncio
async def test_approved_work_does_not_count(pool) -> None:
    """RED on v1.12.42: this agent is refused because its approved tasks count.

    Reproduces the example-node case: everything it filed was answered by the owner,
    and it still could not file the next one.
    """
    await _seed_actor(pool, "busy-agent", "agent")
    repo = TasksRepo(pool)

    # Answered one at a time, the way it happens: the agent proposes, the owner
    # takes it into work, the agent proposes the next. Twelve, well past any cap.
    # in_progress rather than open because the schema allows awaiting_approval to
    # move only to blocked/done/escalated/in_progress/rejected.
    for i in range(12):
        task = await repo.create(title=f"busy-agent proposal {i}", actor_id="busy-agent")
        await pool.execute("UPDATE tasks SET status='in_progress' WHERE id=$1", task.id)

    nxt = await repo.create(title="the thirteenth", actor_id="busy-agent")
    assert nxt.status == "awaiting_approval"
    assert nxt.requester == "busy-agent"


@pytest.mark.asyncio
async def test_unanswered_proposals_are_capped(pool) -> None:
    await _seed_actor(pool, "proposer", "agent")
    repo = TasksRepo(pool)

    await _propose(repo, "proposer", _OPEN_TASK_CAP)

    with pytest.raises(TaskError) as refused:
        await repo.create(title="one too many", actor_id="proposer")
    message = str(refused.value)
    assert "awaiting" in message
    # The refusal has to name what is pending, or the agent can only guess which
    # of its proposals is in the way.
    assert "proposer proposal 0" in message


@pytest.mark.asyncio
async def test_a_refused_creation_is_recorded(pool) -> None:
    """The refusal stays loud: it is a notification, not a silent 409."""
    await _seed_actor(pool, "loud-agent", "agent")
    repo = TasksRepo(pool)
    await _propose(repo, "loud-agent", _OPEN_TASK_CAP)
    with pytest.raises(TaskError):
        await repo.create(title="refused", actor_id="loud-agent")

    rows = await pool.fetch(
        "SELECT payload FROM task_notifications "
        "WHERE event_type='task_creation_refused' AND notify_target=$1",
        "loud-agent",
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_runner_files_past_the_cap(pool) -> None:
    """RED on v1.12.42: the runner is capped like an agent and the node loses
    its filing door."""
    await _seed_actor(pool, "runner", "system")
    repo = TasksRepo(pool)

    for n in range(_OPEN_TASK_CAP + 2):
        task = await repo.create(title=f"runner task {n}", actor_id="runner")
        # Exempt from the COUNT only. What the runner files is still inert and
        # unassigned, so the exemption cannot be used to push work at anybody.
        assert task.status == "awaiting_approval"
        assert task.assignee is None
        assert task.requester == "runner"


@pytest.mark.asyncio
async def test_the_exemption_does_not_leak_to_other_service_actors(pool) -> None:
    """A second service actor is NOT exempt. Without this, "service keys are
    exempt" would read as a class and take the cap off delegated agent calls."""
    await _seed_actor(pool, "voice", "system")
    repo = TasksRepo(pool)

    await _propose(repo, "voice", _OPEN_TASK_CAP)
    with pytest.raises(TaskError):
        await repo.create(title="voice one too many", actor_id="voice")
