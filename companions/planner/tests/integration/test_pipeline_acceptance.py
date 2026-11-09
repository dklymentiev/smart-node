"""R2 acceptance: full pipeline scenarios end-to-end.

Each scenario walks a task through a representative life-cycle and asserts
that ``task_log`` and ``task_notifications`` capture every transition. This
is the exit criterion for R2.
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
    await actors.create(actor_id="alex", display_name="Alex", type="human")
    await actors.create(actor_id="claude", display_name="Claude", type="agent")
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    return project.id


async def _transitions(pool, task_id: int) -> list[tuple[str, str, str]]:
    rows = await pool.fetch(
        "SELECT old_value, new_value, actor FROM task_log "
        "WHERE task_id = $1 AND action = 'status_changed' "
        "ORDER BY id",
        task_id,
    )
    return [(r["old_value"], r["new_value"], r["actor"]) for r in rows]


async def _notifications(pool, task_id: int) -> list[tuple[str, str | None]]:
    rows = await pool.fetch(
        "SELECT event_type, notify_target FROM task_notifications "
        "WHERE task_id = $1 ORDER BY id",
        task_id,
    )
    return [(r["event_type"], r["notify_target"]) for r in rows]


# ---------------------------------------------------------------------------
# Scenario 1: open -> done full path
# ---------------------------------------------------------------------------


async def test_scenario_full_happy_path(pool) -> None:
    project_id = await _seed(pool)
    task = await TasksRepo(pool).create(title="ship feature", project_id=project_id)
    pipeline = Pipeline(pool)

    await pipeline.claim(task.id, agent="claude")
    await pipeline.submit_assessment(
        task.id,
        actor="claude",
        scope="src/feature.py",
        eta="3h",
        complexity_hint="moderate",
    )
    await pipeline.approve(task.id, actor="alex")
    await pipeline.review(task.id, actor="claude")
    final = await pipeline.verify(task.id, actor="alex", comment="accepted")

    assert final.status == "done"
    assert final.completed_at is not None
    assert final.approved_at is not None
    assert final.execution_started_at is not None
    assert final.assessed_at is not None

    assert await _transitions(pool, task.id) == [
        ("open", "in_progress", "claude"),
        ("in_progress", "awaiting_approval", "claude"),
        ("awaiting_approval", "in_progress", "alex"),
        ("in_progress", "pending_review", "claude"),
        ("pending_review", "done", "alex"),
    ]
    # notify_target reflects the task's assignee (or reviewer for pending_review).
    # claim() sets assignee=claude; reviewer is not set at creation so the
    # pending_review transition defaults: requester=None -> reviewer=None -> None.
    assert await _notifications(pool, task.id) == [
        ("status:in_progress", "claude"),
        ("status:awaiting_approval", "claude"),
        ("status:in_progress", "claude"),
        ("status:pending_review", None),
        ("status:done", "claude"),
    ]


# ---------------------------------------------------------------------------
# Scenario 2: bounce x2 -> auto-escalate
# ---------------------------------------------------------------------------


async def test_scenario_bounce_twice_auto_escalates(pool) -> None:
    project_id = await _seed(pool)
    task = await TasksRepo(pool).create(
        title="needs refining", status="pending_review", project_id=project_id
    )
    pipeline = Pipeline(pool)

    await pipeline.bounce_back(task.id, actor="alex", reason="missing X")
    await pipeline.review(task.id, actor="claude")  # back to pending_review
    await pipeline.bounce_back(task.id, actor="alex", reason="still missing X")

    final = await TasksRepo(pool).get(task.id)
    assert final is not None
    assert final.status == "escalated"
    assert final.verification_attempts == 2

    transitions = await _transitions(pool, task.id)
    assert transitions == [
        ("pending_review", "in_progress", "alex"),  # first bounce
        ("in_progress", "pending_review", "claude"),  # resubmit
        ("pending_review", "escalated", "system"),  # second bounce -> escalate
    ]


# ---------------------------------------------------------------------------
# Scenario 3: block -> blocker done -> cascading unblock
# ---------------------------------------------------------------------------


async def test_scenario_block_then_blocker_done_cascades(pool) -> None:
    project_id = await _seed(pool)
    repo = TasksRepo(pool)
    blocker = await repo.create(
        title="upstream", status="pending_review", project_id=project_id
    )
    target = await repo.create(
        title="downstream", status="in_progress", project_id=project_id
    )
    pipeline = Pipeline(pool)

    await pipeline.block(
        target.id,
        actor="claude",
        reason="task_dependency",
        by_task_id=blocker.id,
    )
    await pipeline.verify(blocker.id, actor="alex", comment="accepted")

    final = await repo.get(target.id)
    assert final is not None
    assert final.status == "in_progress"
    assert final.blocked_by is None

    target_log = await _transitions(pool, target.id)
    assert target_log == [
        ("in_progress", "blocked", "claude"),
        ("blocked", "in_progress", "system"),  # cascade
    ]


# ---------------------------------------------------------------------------
# Scenario 4: done -> incident creates 2 children
# ---------------------------------------------------------------------------


async def test_scenario_done_to_incident_creates_children(pool) -> None:
    project_id = await _seed(pool)
    parent = await TasksRepo(pool).create(
        title="shipped feature",
        status="done",
        assignee="claude",
        project_id=project_id,
    )
    pipeline = Pipeline(pool)

    incident, hotfix_id, pm_id = await pipeline.report_incident(
        parent.id, actor="alex", description="users reporting null pointer"
    )

    assert incident.status == "in_progress"
    rels = await pool.fetch(
        "SELECT task_id, related_id, relation FROM task_relations "
        "WHERE task_id IN ($1, $2, $3) OR related_id IN ($1, $2, $3) "
        "ORDER BY relation, task_id",
        parent.id,
        hotfix_id,
        pm_id,
    )
    triples = {(r["task_id"], r["related_id"], r["relation"]) for r in rels}
    assert triples == {
        (parent.id, hotfix_id, "hotfix"),
        (parent.id, pm_id, "postmortem"),
        (hotfix_id, parent.id, "parent"),
        (pm_id, parent.id, "parent"),
    }
    children = await pool.fetch(
        "SELECT id, status, assignee, priority FROM tasks "
        "WHERE id IN ($1, $2) ORDER BY id",
        hotfix_id,
        pm_id,
    )
    by_id = {r["id"]: r for r in children}
    assert by_id[hotfix_id]["assignee"] == "claude"
    assert by_id[hotfix_id]["priority"] == "high"
    assert by_id[pm_id]["assignee"] is None
    assert by_id[pm_id]["priority"] == "medium"
