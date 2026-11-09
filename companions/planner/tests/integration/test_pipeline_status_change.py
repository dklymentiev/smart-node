"""Integration tests for :class:`planner.pipeline.Pipeline.status_change`.

Covers the atomic core for R2-1: validation, audit log, notification queue,
optional comment, no-op idempotency, force bypass, locking semantics.
"""

from __future__ import annotations

import pytest

from planner.pipeline import Pipeline, PipelineError
from planner.repos.actors import ActorsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


async def _seed_actors(pool) -> None:
    actors = ActorsRepo(pool)
    await actors.create(actor_id="alex", display_name="Alex", type="human")
    await actors.create(actor_id="claude", display_name="Claude", type="agent")


async def _new_task(pool, **overrides) -> int:
    repo = TasksRepo(pool)
    task = await repo.create(title="t", **overrides)
    return task.id


async def test_status_change_open_to_assessing(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    # "assessing" is a retired name: accepted, and it means in_progress (0010).
    task = await pipeline.status_change(task_id, "assessing", actor="claude")
    assert task.status == "in_progress"

    log_rows = await pool.fetch(
        "SELECT action, old_value, new_value, actor FROM task_log "
        "WHERE task_id = $1 ORDER BY id",
        task_id,
    )
    assert [dict(r) for r in log_rows] == [
        {
            "action": "status_changed",
            "old_value": "open",
            "new_value": "in_progress",
            "actor": "claude",
        }
    ]

    notif_rows = await pool.fetch(
        "SELECT event_type, notify_target FROM task_notifications "
        "WHERE task_id = $1",
        task_id,
    )
    # Task has no assignee, so notify_target is NULL (no one to wake for this
    # transition). The row is still written for audit.
    assert [dict(r) for r in notif_rows] == [
        {"event_type": "status:in_progress", "notify_target": None}
    ]


async def test_status_change_invalid_transition_raises(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    # No matrix any more (0010): what is refused is a name nobody knows.
    with pytest.raises(PipelineError, match="unknown status"):
        await pipeline.status_change(task_id, "finished", actor="claude")

    # Nothing got written.
    log_count = await pool.fetchval(
        "SELECT count(*) FROM task_log WHERE task_id = $1", task_id
    )
    notif_count = await pool.fetchval(
        "SELECT count(*) FROM task_notifications WHERE task_id = $1", task_id
    )
    assert log_count == 0
    assert notif_count == 0


async def test_status_change_force_bypasses_matrix(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    task = await pipeline.status_change(
        task_id, "done", actor="system", force=True
    )
    assert task.status == "done"


async def test_status_change_idempotent_noop(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    task = await pipeline.status_change(task_id, "open", actor="claude")
    assert task.status == "open"

    log_count = await pool.fetchval(
        "SELECT count(*) FROM task_log WHERE task_id = $1", task_id
    )
    notif_count = await pool.fetchval(
        "SELECT count(*) FROM task_notifications WHERE task_id = $1", task_id
    )
    assert log_count == 0
    assert notif_count == 0


async def test_status_change_writes_comment_when_given(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    await pipeline.status_change(
        task_id,
        "assessing",
        actor="claude",
        comment="Picked up; complexity looks moderate.",
    )

    rows = await pool.fetch(
        "SELECT author, content, comment_type FROM task_comments "
        "WHERE task_id = $1",
        task_id,
    )
    assert [dict(r) for r in rows] == [
        {
            "author": "claude",
            "content": "Picked up; complexity looks moderate.",
            "comment_type": "status_change",
        }
    ]


async def test_status_change_extra_fields_patches_columns(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    task = await pipeline.status_change(
        task_id,
        "in_progress",
        actor="claude",
        extra_fields={"complexity_hint": "moderate", "eta": "2h"},
    )
    assert task.complexity_hint == "moderate"
    assert task.eta == "2h"


async def test_status_change_extra_fields_whitelist(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="extra_fields not allowed"):
        await pipeline.status_change(
            task_id,
            "in_progress",
            actor="claude",
            extra_fields={"title": "new title"},
        )


async def test_status_change_missing_task_raises(pool) -> None:
    await _seed_actors(pool)
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="not found"):
        await pipeline.status_change(99999, "assessing", actor="claude")


async def test_status_change_unknown_status_raises(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="unknown status"):
        await pipeline.status_change(task_id, "nope", actor="claude")  # type: ignore[arg-type]


async def test_status_change_payload_records_transition(pool) -> None:
    await _seed_actors(pool)
    task_id = await _new_task(pool)
    pipeline = Pipeline(pool)

    await pipeline.status_change(task_id, "assessing", actor="claude")

    payload = await pool.fetchval(
        "SELECT payload FROM task_notifications WHERE task_id = $1", task_id
    )
    # asyncpg returns jsonb as a Python dict via json codec; if it returned a
    # string the check below also works after json.loads.
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    assert payload == {"from": "open", "to": "in_progress", "actor": "claude"}
