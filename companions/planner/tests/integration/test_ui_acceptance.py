"""R3 acceptance: end-to-end UI walkthroughs.

Each scenario clicks through the UI as a real operator would: login, create
state via REST, then walk the rendered pages and confirm the data lands.

Note on Phase 4.6 L1: the original task list mentions a 40+ test
agent-adequacy benchmark (greetings, secrets not echoed, etc.). Those
checks are aimed at AI-agent behavior, not a server-rendered web UI;
they belong to a separate workstream and are recorded in the R4 worklog
for the Phase 4.6 task. This file covers the user-facing UI flows.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from planner.app import create_app
from planner.auth import ApiKeysRepo
from planner.repos.actors import ActorsRepo
from planner.repos.objectives import ObjectivesRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def setup(pool: asyncpg.Pool):
    await ActorsRepo(pool).create(
        actor_id="claude", display_name="Claude", type="agent"
    )
    await ActorsRepo(pool).create(
        actor_id="alex", display_name="Alex", type="human"
    )
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        _, key = await ApiKeysRepo(pool).create(label="root", actor_id="system")
        yield c, pool, key


# ---------------------------------------------------------------------------
# UC-001: First-time login -> dashboard -> project board
# ---------------------------------------------------------------------------


async def test_uc001_login_to_board(setup) -> None:
    client, pool, key = setup
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    await TasksRepo(pool).create(
        title="Task A", project_id=project.id, status="open"
    )

    # 1. Hit dashboard cold -> redirected to login.
    r = await client.get("/ui/")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"

    # 2. Submit the login form.
    r = await client.post("/ui/login", data={"key": key})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/"
    cookie_header = r.headers.get("set-cookie", "")
    assert "planner_key=" in cookie_header

    # The cookie is now part of the AsyncClient cookie jar; subsequent
    # GETs include it. Hit the dashboard partial for projects.
    r = await client.get("/ui/_partials/projects")
    assert r.status_code == 200
    assert "OSS" in r.text
    # Dashboard now links to the project overview; board is one tab in.
    assert 'href="/ui/projects/oss"' in r.text

    # 3. Click into the board.
    r = await client.get("/ui/projects/oss/board")
    assert r.status_code == 200
    assert "Task A" in r.text


# ---------------------------------------------------------------------------
# UC-002: Pick up a task, move it through the pipeline, finish it
# ---------------------------------------------------------------------------


async def test_uc002_full_lifecycle_via_ui(setup) -> None:
    client, pool, key = setup
    await client.post("/ui/login", data={"key": key})
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    task = await TasksRepo(pool).create(
        title="Spec it", project_id=project.id, status="open"
    )

    # open -> assessing -> awaiting_approval requires extra fields
    # which the UI does not collect on the current detail page. We use
    # the bare transition button to walk open -> in_progress -> ...
    # That path is allowed by the matrix.
    for to in ("in_progress", "pending_review", "done"):
        r = await client.post(
            f"/ui/tasks/{task.id}/transition", data={"to": to, "comment": "ok"}
        )
        assert r.status_code == 200, f"transition to {to} failed"

    final = await TasksRepo(pool).get(task.id)
    assert final is not None
    assert final.status == "done"


# ---------------------------------------------------------------------------
# UC-003: Add comment + tag + link via UI
# ---------------------------------------------------------------------------


async def test_uc003_collaboration_actions(setup) -> None:
    client, pool, key = setup
    await client.post("/ui/login", data={"key": key})
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    task = await TasksRepo(pool).create(
        title="Triage", project_id=project.id, status="open"
    )

    r = await client.post(
        f"/ui/tasks/{task.id}/comment", data={"content": "Looks like spec drift"}
    )
    assert r.status_code == 200
    assert "Looks like spec drift" in r.text

    r = await client.post(
        f"/ui/tasks/{task.id}/tag", data={"tag": "spec-drift"}
    )
    assert r.status_code == 200
    assert "spec-drift" in r.text

    r = await client.post(
        f"/ui/tasks/{task.id}/link",
        data={"kind": "doc", "url": "https://example.com/x", "label": "Spec"},
    )
    assert r.status_code == 200
    assert "https://example.com/x" in r.text


# ---------------------------------------------------------------------------
# UC-004: Operator views objective tree to plan a release
# ---------------------------------------------------------------------------


async def test_uc004_objective_tree_with_progress(setup) -> None:
    client, pool, key = setup
    await client.post("/ui/login", data={"key": key})

    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    obj_repo = ObjectivesRepo(pool)
    strat = await obj_repo.create(
        project_id=project.id, title="v0.1.0 strategy", type="strategy"
    )
    mile = await obj_repo.create(
        project_id=project.id,
        title="R0 foundation",
        type="milestone",
        parent_id=strat.id,
    )
    repo = TasksRepo(pool)
    await repo.create(
        title="t1", project_id=project.id, objective_id=mile.id, status="done"
    )
    await repo.create(
        title="t2", project_id=project.id, objective_id=mile.id, status="done"
    )
    await repo.create(
        title="t3", project_id=project.id, objective_id=mile.id, status="open"
    )

    r = await client.get("/ui/projects/oss/objectives")
    assert r.status_code == 200
    body = r.text
    assert "v0.1.0 strategy" in body
    assert "R0 foundation" in body
    # 2 of 3 done -> 67%.
    assert "2 / 3" in body
    assert "67%" in body


# ---------------------------------------------------------------------------
# UC-005: Operator manages API keys
# ---------------------------------------------------------------------------


async def test_uc005_keys_lifecycle(setup) -> None:
    client, _, key = setup
    await client.post("/ui/login", data={"key": key})

    # See the root key in the list.
    r = await client.get("/ui/keys")
    assert r.status_code == 200
    assert "root" in r.text

    # Create a new one bound to claude.
    r = await client.post(
        "/ui/keys/create",
        data={"label": "ci-bot", "actor_id": "claude"},
    )
    assert r.status_code == 200
    assert "ci-bot" in r.text
    assert "pln_" in r.text  # plaintext shown once

    # Reload -- plaintext is gone, key is in the table.
    r = await client.get("/ui/keys")
    assert "ci-bot" in r.text
    # plaintext block (the success callout) should not be on the listing page
    assert "Copy this now" not in r.text


# ---------------------------------------------------------------------------
# Regression: every nav link on the dashboard resolves (no 404).
# ---------------------------------------------------------------------------


async def test_nav_links_all_resolve(setup) -> None:
    client, _, key = setup
    await client.post("/ui/login", data={"key": key})

    # The nav links: /ui/ , /ui/objectives , /ui/keys , /docs
    for path in ("/ui/", "/ui/objectives", "/ui/keys", "/docs"):
        r = await client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"


# ---------------------------------------------------------------------------
# Regression: invalid transition does not break the page.
# ---------------------------------------------------------------------------


async def test_invalid_transition_does_not_crash(setup) -> None:
    client, pool, key = setup
    await client.post("/ui/login", data={"key": key})
    project = await ProjectsRepo(pool).create(slug="oss", title="OSS")
    task = await TasksRepo(pool).create(
        title="x", project_id=project.id, status="open"
    )

    # open -> done without a report is refused; UI must surface the error and
    # keep status at 'open'.
    r = await client.post(
        f"/ui/tasks/{task.id}/transition", data={"to": "done"}
    )
    assert r.status_code == 200
    assert "comment is required" in r.text
    final = await TasksRepo(pool).get(task.id)
    assert final is not None
    assert final.status == "open"
