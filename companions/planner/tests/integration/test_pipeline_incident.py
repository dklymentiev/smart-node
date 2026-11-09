"""Integration tests for incident workflow."""

from __future__ import annotations

import pytest

from planner.pipeline import Pipeline, PipelineError
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


async def test_report_incident_creates_hotfix_and_postmortem(pool) -> None:
    project_id = await _seed(pool)
    parent = await TasksRepo(pool).create(
        title="Original delivery",
        status="done",
        assignee="claude",
        project_id=project_id,
    )
    pipeline = Pipeline(pool)

    incident, hotfix_id, pm_id = await pipeline.report_incident(
        parent.id,
        actor="alex",
        description="Lost messages during rollover",
    )

    assert incident.status == "in_progress"

    rows = await pool.fetch(
        "SELECT id, title, stage AS status, assignee, priority, project_id "
        "FROM tasks WHERE id IN ($1, $2) ORDER BY id",
        hotfix_id,
        pm_id,
    )
    by_id = {r["id"]: r for r in rows}

    hotfix = by_id[hotfix_id]
    assert hotfix["title"] == "Fix: Lost messages during rollover"
    assert hotfix["status"] == "open"
    assert hotfix["assignee"] == "claude"
    assert hotfix["priority"] == "high"
    assert hotfix["project_id"] == project_id

    pm = by_id[pm_id]
    assert pm["title"].startswith("Post-mortem:")
    assert pm["status"] == "open"
    assert pm["assignee"] is None  # unassigned to avoid self-review
    assert pm["priority"] == "medium"
    assert pm["project_id"] == project_id


async def test_report_incident_links_relations_both_ways(pool) -> None:
    project_id = await _seed(pool)
    parent = await TasksRepo(pool).create(
        title="X",
        status="done",
        assignee="claude",
        project_id=project_id,
    )
    pipeline = Pipeline(pool)

    _, hotfix_id, pm_id = await pipeline.report_incident(
        parent.id, actor="alex", description="bug"
    )

    rels = await pool.fetch(
        "SELECT task_id, related_id, relation FROM task_relations "
        "WHERE task_id = $1 OR related_id = $1 "
        "ORDER BY task_id, related_id, relation",
        parent.id,
    )
    triples = {(r["task_id"], r["related_id"], r["relation"]) for r in rels}
    assert triples == {
        (parent.id, hotfix_id, "hotfix"),
        (parent.id, pm_id, "postmortem"),
        (hotfix_id, parent.id, "parent"),
        (pm_id, parent.id, "parent"),
    }


async def test_report_incident_writes_summary_comment(pool) -> None:
    project_id = await _seed(pool)
    parent = await TasksRepo(pool).create(
        title="X", status="done", assignee="claude", project_id=project_id
    )
    pipeline = Pipeline(pool)

    _, hotfix_id, pm_id = await pipeline.report_incident(
        parent.id, actor="alex", description="symptoms"
    )

    rows = await pool.fetch(
        "SELECT author, content, comment_type FROM task_comments "
        "WHERE task_id = $1 ORDER BY id",
        parent.id,
    )
    # The 'status_changed' done->incident transition does NOT add a comment
    # by itself (no `comment` arg); only the incident summary.
    assert len(rows) == 1
    assert rows[0]["author"] == "alex"
    assert rows[0]["comment_type"] == "incident_followup"
    assert f"#{hotfix_id}" in rows[0]["content"]
    assert f"#{pm_id}" in rows[0]["content"]


async def test_report_incident_unassigned_parent_keeps_unassigned_hotfix(pool) -> None:
    project_id = await _seed(pool)
    parent = await TasksRepo(pool).create(
        title="X", status="done", project_id=project_id
    )
    pipeline = Pipeline(pool)

    _, hotfix_id, _ = await pipeline.report_incident(
        parent.id, actor="alex", description="x"
    )
    hf_assignee = await pool.fetchval(
        "SELECT assignee FROM tasks WHERE id = $1", hotfix_id
    )
    assert hf_assignee is None


async def test_report_incident_requires_done_parent(pool) -> None:
    project_id = await _seed(pool)
    parent = await TasksRepo(pool).create(
        title="X", status="in_progress", project_id=project_id
    )
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="not done"):
        await pipeline.report_incident(
            parent.id, actor="alex", description="x"
        )


async def test_report_incident_requires_description(pool) -> None:
    project_id = await _seed(pool)
    parent = await TasksRepo(pool).create(
        title="X", status="done", project_id=project_id
    )
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="description must not be blank"):
        await pipeline.report_incident(parent.id, actor="alex", description="")


async def test_report_incident_missing_task(pool) -> None:
    await _seed(pool)
    pipeline = Pipeline(pool)

    with pytest.raises(PipelineError, match="not found"):
        await pipeline.report_incident(999, actor="alex", description="x")
