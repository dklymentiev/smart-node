"""R0 acceptance: schema up, every repo round-trips against a real DB."""

from __future__ import annotations

import asyncpg
import pytest

from planner.repos.actors import ActorError, ActorsRepo
from planner.repos.ancillary import (
    AncillaryError,
    CommentsRepo,
    CommitsRepo,
    LinksRepo,
    LogRepo,
    NotificationsRepo,
    RelationsRepo,
    TagsRepo,
)
from planner.repos.objectives import ObjectiveError, ObjectivesRepo
from planner.repos.projects import ProjectError, ProjectsRepo
from planner.repos.tasks import TaskError, TasksRepo


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


async def test_schema_has_eleven_tables(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name NOT LIKE '\\_%' ESCAPE '\\'
          AND table_type = 'BASE TABLE'
        """
    )
    names = {r["table_name"] for r in rows}
    expected = {
        "actors", "projects", "objectives", "tasks",
        "task_comments", "task_relations", "task_commits", "task_log",
        "task_tags", "task_links", "task_notifications",
    }
    assert expected.issubset(names), f"missing tables: {expected - names}"


async def test_seed_system_actor_present(pool: asyncpg.Pool) -> None:
    repo = ActorsRepo(pool)
    actor = await repo.get("system")
    assert actor is not None
    assert actor.type == "system"
    assert actor.active is True


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


async def test_actors_create_get_list_update(pool: asyncpg.Pool) -> None:
    repo = ActorsRepo(pool)
    a = await repo.create("claude", "Claude", "agent")
    assert a.id == "claude" and a.active is True

    got = await repo.get("claude")
    assert got is not None and got.display_name == "Claude"

    listed = await repo.list(type="agent")
    assert [x.id for x in listed] == ["claude"]

    updated = await repo.update("claude", display_name="Claude Code")
    assert updated and updated.display_name == "Claude Code"

    deactivated = await repo.deactivate("claude")
    assert deactivated and deactivated.active is False


async def test_actors_duplicate_id_rejected(pool: asyncpg.Pool) -> None:
    repo = ActorsRepo(pool)
    await repo.create("dup", "Dup", "human")
    with pytest.raises(ActorError, match="already exists"):
        await repo.create("dup", "Other", "human")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


async def test_projects_round_trip(pool: asyncpg.Pool) -> None:
    repo = ProjectsRepo(pool)
    p = await repo.create("planner-oss", "Planner OSS", description="self-hostable")
    assert p.slug == "planner-oss"

    got = await repo.get_by_slug("planner-oss")
    assert got and got.id == p.id

    archived = await repo.archive(p.id)
    assert archived and archived.status == "archived"

    only_active = await repo.list(status="active")
    assert all(x.status == "active" for x in only_active)
    assert p.id not in [x.id for x in only_active]


async def test_projects_duplicate_slug_rejected(pool: asyncpg.Pool) -> None:
    repo = ProjectsRepo(pool)
    await repo.create("acme", "Acme")
    with pytest.raises(ProjectError, match="already exists"):
        await repo.create("acme", "Acme 2")


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------


async def test_objectives_tree_query(pool: asyncpg.Pool) -> None:
    proj_repo = ProjectsRepo(pool)
    proj = await proj_repo.create("p-tree", "Tree project")

    obj_repo = ObjectivesRepo(pool)
    strategy = await obj_repo.create(
        project_id=proj.id, title="Strategy", type="strategy"
    )
    roadmap = await obj_repo.create(
        project_id=proj.id, title="Roadmap", type="roadmap", parent_id=strategy.id
    )
    m1 = await obj_repo.create(
        project_id=proj.id, title="M1", type="milestone", parent_id=roadmap.id
    )
    m2 = await obj_repo.create(
        project_id=proj.id, title="M2", type="milestone", parent_id=roadmap.id, sort_order=1
    )

    forest = await obj_repo.tree(project_id=proj.id)
    assert len(forest) == 1
    root = forest[0]
    assert root.objective.id == strategy.id
    assert root.depth == 0
    assert len(root.children) == 1
    rmap = root.children[0]
    assert rmap.objective.id == roadmap.id
    assert rmap.depth == 1
    assert [c.objective.id for c in rmap.children] == [m1.id, m2.id]
    assert all(c.depth == 2 for c in rmap.children)

    descendants = await obj_repo.subtree_ids(strategy.id)
    assert set(descendants) == {strategy.id, roadmap.id, m1.id, m2.id}


async def test_objectives_self_parent_rejected(pool: asyncpg.Pool) -> None:
    proj_repo = ProjectsRepo(pool)
    proj = await proj_repo.create("p-sp", "P SP")
    obj_repo = ObjectivesRepo(pool)
    o = await obj_repo.create(project_id=proj.id, title="O", type="goal")
    with pytest.raises(ObjectiveError, match="own parent"):
        await obj_repo.update(o.id, parent_id=o.id)


# ---------------------------------------------------------------------------
# Tasks (CRUD)
# ---------------------------------------------------------------------------


async def test_tasks_crud_and_filters(pool: asyncpg.Pool) -> None:
    actors = ActorsRepo(pool)
    await actors.create("alice", "Alice", "human")
    projects = ProjectsRepo(pool)
    proj = await projects.create("p-tasks", "P Tasks")

    tasks = TasksRepo(pool)
    t1 = await tasks.create(
        title="t1", project_id=proj.id, assignee="alice", priority="high"
    )
    t2 = await tasks.create(
        title="t2", project_id=proj.id, priority="low", status="blocked"
    )

    got = await tasks.get(t1.id)
    assert got and got.title == "t1" and got.priority == "high"

    by_assignee = await tasks.list(assignee="alice")
    assert [t.id for t in by_assignee] == [t1.id]

    by_status = await tasks.list(status="blocked")
    assert [t.id for t in by_status] == [t2.id]

    updated = await tasks.update(t1.id, status="in_progress", scope="walking-skeleton")
    assert updated and updated.status == "in_progress" and updated.scope == "walking-skeleton"

    counts = await tasks.count_by_status(project_id=proj.id)
    assert counts["in_progress"] == 1 and counts["blocked"] == 1

    with pytest.raises(TaskError, match="invalid status"):
        await tasks.update(t1.id, status="bogus")

    deleted = await tasks.delete(t2.id)
    assert deleted is True
    assert await tasks.get(t2.id) is None


async def test_tasks_unknown_assignee_rejected_at_db(pool: asyncpg.Pool) -> None:
    tasks = TasksRepo(pool)
    with pytest.raises(TaskError, match="FK violation"):
        await tasks.create(title="orphan", assignee="ghost")


# ---------------------------------------------------------------------------
# Ancillary repos
# ---------------------------------------------------------------------------


async def test_ancillary_round_trip(pool: asyncpg.Pool) -> None:
    actors = ActorsRepo(pool)
    await actors.create("bob", "Bob", "human")
    projects = ProjectsRepo(pool)
    proj = await projects.create("p-anc", "P Anc")
    tasks = TasksRepo(pool)
    t1 = await tasks.create(title="anc-1", project_id=proj.id)
    t2 = await tasks.create(title="anc-2", project_id=proj.id)

    # Comments
    cmts = CommentsRepo(pool)
    c = await cmts.add(t1.id, author="bob", content="first comment")
    assert c.content == "first comment"
    assert len(await cmts.list_for_task(t1.id)) == 1

    # Relations
    rels = RelationsRepo(pool)
    r = await rels.link(t1.id, t2.id, "child")
    assert r.relation == "child"
    with pytest.raises(AncillaryError, match="already exists"):
        await rels.link(t1.id, t2.id, "child")
    with pytest.raises(AncillaryError, match="itself"):
        await rels.link(t1.id, t1.id, "child")

    # Commits
    com = CommitsRepo(pool)
    cm = await com.add(t1.id, repo="planner-oss", commit_hash="abc123", message="init")
    assert cm.commit_hash == "abc123"

    # Log
    log = LogRepo(pool)
    e = await log.append(t1.id, "status_changed", old_value="open", new_value="in_progress", actor="bob")
    assert e.action == "status_changed"
    entries = await log.list_for_task(t1.id)
    assert any(x.action == "status_changed" for x in entries)

    # Tags
    tags = TagsRepo(pool)
    assert await tags.add(t1.id, "bug") is True
    assert await tags.add(t1.id, "bug") is False  # dup
    assert await tags.list_for_task(t1.id) == ["bug"]
    assert await tags.tasks_with_tag("bug") == [t1.id]
    assert await tags.remove(t1.id, "bug") is True

    # Links
    links = LinksRepo(pool)
    lk = await links.add(t1.id, kind="pr", url="https://example.com/pr/1", label="PR #1")
    assert lk.url.startswith("https://")
    assert len(await links.list_for_task(t1.id)) == 1

    # Notifications
    notify = NotificationsRepo(pool)
    n = await notify.enqueue(
        t1.id,
        event_type="task.created",
        notify_target="email:bob",
        payload={"task_id": t1.id, "title": "anc-1"},
    )
    assert n.payload == {"task_id": t1.id, "title": "anc-1"}
    pending = await notify.pending()
    assert any(x.id == n.id for x in pending)
    assert await notify.mark_fired(n.id) is True
    assert await notify.mark_fired(n.id) is False  # already fired
