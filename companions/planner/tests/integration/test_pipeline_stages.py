"""Integration tests for pipeline stage actions."""

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


async def _new_task(pool, **overrides) -> int:
    task = await TasksRepo(pool).create(title="t", **overrides)
    return task.id


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


async def test_claim_assigns_agent_and_sets_assessed_at(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    task = await pipeline.claim(task_id, agent="claude")
    assert task.status == "in_progress"
    assert task.assignee == "claude"
    assert task.assessed_at is not None


async def test_claim_only_from_open(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="in_progress")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="not open"):
        await pipeline.claim(task_id, agent="claude")


# ---------------------------------------------------------------------------
# submit_assessment
# ---------------------------------------------------------------------------


async def test_submit_assessment_writes_metadata(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="assessing")
    pipeline = Pipeline(pool)

    task = await pipeline.submit_assessment(
        task_id,
        actor="claude",
        scope="src/planner/x.py",
        eta="2h",
        complexity_hint="moderate",
    )
    assert task.status == "awaiting_approval"
    assert task.scope == "src/planner/x.py"
    assert task.eta == "2h"
    assert task.complexity_hint == "moderate"


@pytest.mark.parametrize(
    "scope,eta,complexity",
    [
        ("", "2h", "moderate"),
        ("x", "", "moderate"),
        ("x", "2h", ""),
        ("   ", "2h", "moderate"),
    ],
)
async def test_submit_assessment_rejects_blank_metadata(
    pool, scope, eta, complexity
) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="assessing")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="must not be blank"):
        await pipeline.submit_assessment(
            task_id,
            actor="claude",
            scope=scope,
            eta=eta,
            complexity_hint=complexity,
        )


async def test_submit_assessment_rejects_bad_complexity(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="assessing")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="invalid complexity_hint"):
        await pipeline.submit_assessment(
            task_id,
            actor="claude",
            scope="x",
            eta="2h",
            complexity_hint="ultra",
        )


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------


async def test_approve_sets_timestamps(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="awaiting_approval")
    pipeline = Pipeline(pool)

    task = await pipeline.approve(task_id, actor="alex")
    assert task.status == "in_progress"
    assert task.approved_at is not None
    assert task.execution_started_at is not None


async def test_approve_rejects_from_done(pool) -> None:
    # done -> in_progress is not a valid transition (only done -> incident).
    # Approving a done task must surface as a PipelineError.
    await _seed(pool)
    task_id = await _new_task(pool, status="done")
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="not awaiting_approval"):
        await pipeline.approve(task_id, actor="alex")


async def test_reject_from_assessing(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="assessing")
    pipeline = Pipeline(pool)

    task = await pipeline.reject(task_id, actor="alex", comment="not needed")
    assert task.status == "rejected"

    rows = await pool.fetch(
        "SELECT content FROM task_comments WHERE task_id = $1", task_id
    )
    assert [r["content"] for r in rows] == ["not needed"]


# ---------------------------------------------------------------------------
# review / verify
# ---------------------------------------------------------------------------


async def test_review_in_progress_to_pending_review(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="in_progress")
    pipeline = Pipeline(pool)

    task = await pipeline.review(task_id, actor="claude")
    assert task.status == "pending_review"


async def test_verify_sets_completed_at(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool, status="pending_review")
    pipeline = Pipeline(pool)

    task = await pipeline.verify(task_id, actor="alex", comment="accepted")
    assert task.status == "done"
    assert task.completed_at is not None


async def test_full_happy_path_logs_each_step(pool) -> None:
    await _seed(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    await pipeline.claim(task_id, agent="claude")
    await pipeline.submit_assessment(
        task_id,
        actor="claude",
        scope="x",
        eta="1h",
        complexity_hint="simple",
    )
    await pipeline.approve(task_id, actor="alex")
    await pipeline.review(task_id, actor="claude")
    await pipeline.verify(task_id, actor="alex", comment="accepted")

    log = await pool.fetch(
        "SELECT old_value, new_value, actor FROM task_log "
        "WHERE task_id = $1 AND action = 'status_changed' "
        "ORDER BY id",
        task_id,
    )
    assert [(r["old_value"], r["new_value"], r["actor"]) for r in log] == [
        ("open", "in_progress", "claude"),
        ("in_progress", "awaiting_approval", "claude"),
        ("awaiting_approval", "in_progress", "alex"),
        ("in_progress", "pending_review", "claude"),
        ("pending_review", "done", "alex"),
    ]
