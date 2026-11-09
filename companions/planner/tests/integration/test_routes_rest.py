"""REST end-to-end coverage for projects, objectives, tasks, ancillary."""

from __future__ import annotations

import os

import httpx
import pytest_asyncio

from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo


@pytest_asyncio.fixture
async def client(pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    # DD-008: pin apikey mode so the X-API-Key header is honoured and the
    # actor on each request is `system`, not the dev fallback.
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    # The caller is the owner here: a non-owner's filing is held awaiting
    # approval with its assignee cleared, which is not what these tests probe.
    monkeypatch.setenv("PLANNER_OWNER_ACTOR_ID", "system")
    app = create_app()
    async with app.router.lifespan_context(app):
        repo = ApiKeysRepo(app.state.pool)
        _, plaintext = await repo.create(label="t", actor_id="system")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-API-Key": plaintext},
        ) as c:
            yield c


# ---------------------------------------------------------------------------
# Reading less than the whole log of a task
# ---------------------------------------------------------------------------


async def test_comments_one_by_id_newest_and_by_text(client: httpx.AsyncClient) -> None:
    """A reader with a bounded window can reach any comment of a long task."""
    p = (await client.post("/api/v1/projects", json={"slug": "log-reading", "title": "log reading"})).json()
    t = (await client.post("/api/v1/tasks", json={"title": "long log", "project_id": p["id"]})).json()
    base = f"/api/v1/tasks/{t['id']}/comments"
    ids = []
    for text in ("first, a very long opening", "second", "third mentions 100% of it", "fourth, the call"):
        r = await client.post(base, json={"content": text})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])

    everything = (await client.get(base)).json()
    assert [c["id"] for c in everything] == ids

    one = (await client.get(base, params={"comment_id": ids[3]})).json()
    assert [c["content"] for c in one] == ["fourth, the call"]

    newest = (await client.get(base, params={"last": 2})).json()
    assert [c["id"] for c in newest] == ids[2:], "the newest two, still oldest first"

    by_text = (await client.get(base, params={"q": "MENTIONS"})).json()
    assert [c["id"] for c in by_text] == [ids[2]]
    literal = (await client.get(base, params={"q": "100%"})).json()
    assert [c["id"] for c in literal] == [ids[2]], "a percent sign is text, not a wildcard"
    assert (await client.get(base, params={"q": "%"})).json() == literal

    # A comment of another task is not reachable through this one.
    other = (await client.post("/api/v1/tasks", json={"title": "other", "project_id": p["id"]})).json()
    assert (await client.get(f"/api/v1/tasks/{other['id']}/comments", params={"comment_id": ids[0]})).json() == []
    assert (await client.get(base, params={"last": 0})).status_code == 422


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


async def test_projects_crud(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/projects",
        json={"slug": "demo", "title": "Demo project"},
    )
    assert r.status_code == 201, r.text
    proj = r.json()
    pid = proj["id"]

    r = await client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 200

    r = await client.get("/api/v1/projects/by-slug/demo")
    assert r.status_code == 200 and r.json()["id"] == pid

    r = await client.patch(
        f"/api/v1/projects/{pid}",
        json={"title": "Demo v2", "sort_order": 5},
    )
    assert r.status_code == 200 and r.json()["title"] == "Demo v2"

    r = await client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 200 and r.json()["status"] == "archived"

    r = await client.post("/api/v1/projects", json={"slug": "demo", "title": "x"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------


async def test_objectives_tree_and_progress(client: httpx.AsyncClient) -> None:
    proj_id = (await client.post(
        "/api/v1/projects", json={"slug": "p2", "title": "p2"}
    )).json()["id"]

    strat = (await client.post(
        "/api/v1/objectives",
        json={"project_id": proj_id, "title": "S", "type": "strategy"},
    )).json()
    rdm = (await client.post(
        "/api/v1/objectives",
        json={
            "project_id": proj_id, "title": "R", "type": "roadmap",
            "parent_id": strat["id"],
        },
    )).json()
    mls = (await client.post(
        "/api/v1/objectives",
        json={
            "project_id": proj_id, "title": "M", "type": "milestone",
            "parent_id": rdm["id"],
        },
    )).json()

    # Tree
    r = await client.get(f"/api/v1/objectives/projects/{proj_id}/tree")
    assert r.status_code == 200
    tree = r.json()
    assert len(tree) == 1 and tree[0]["objective"]["id"] == strat["id"]
    assert tree[0]["depth"] == 0
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["children"][0]["objective"]["id"] == mls["id"]

    # Add 3 tasks under the milestone, mark 1 done
    t_ids = []
    for i in range(3):
        t = await client.post(
            "/api/v1/tasks",
            json={
                "title": f"t{i}", "project_id": proj_id,
                "objective_id": mls["id"],
            },
        )
        t_ids.append(t.json()["id"])
    # open -> in_progress -> done (the DB trigger enforces the matrix)
    r = await client.patch(f"/api/v1/tasks/{t_ids[0]}", json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/api/v1/tasks/{t_ids[0]}", json={"status": "done", "comment": "done"})
    assert r.status_code == 200, r.text

    # Progress at the strategy level (subtree includes everything)
    r = await client.get(f"/api/v1/objectives/{strat['id']}/progress")
    assert r.status_code == 200
    body = r.json()
    assert body["total_tasks"] == 3
    assert body["tasks_by_status"]["done"] == 1
    assert body["progress_pct"] == 33
    # ... and in the three statuses
    assert body["tasks_by_state"] == {"queued": 2, "closed": 1}
    assert body["queued_by_reason"] == {"none": 2}
    assert body["closed_by_outcome"] == {"completed": 1}

    # Update / delete objective
    r = await client.patch(
        f"/api/v1/objectives/{mls['id']}", json={"status": "completed"}
    )
    assert r.status_code == 200 and r.json()["status"] == "completed"

    # Listing with filter
    r = await client.get(
        "/api/v1/objectives", params={"project_id": proj_id, "type": "milestone"}
    )
    assert r.status_code == 200 and len(r.json()) == 1

    r = await client.delete(f"/api/v1/objectives/{mls['id']}")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


async def test_tasks_crud_and_filters(client: httpx.AsyncClient) -> None:
    proj_id = (await client.post(
        "/api/v1/projects", json={"slug": "p3", "title": "p3"}
    )).json()["id"]

    await client.post(
        "/api/v1/actors",
        json={"id": "carol", "display_name": "Carol", "type": "human"},
    )

    t1 = (await client.post(
        "/api/v1/tasks",
        json={
            "title": "first", "project_id": proj_id,
            "assignee": "carol", "priority": "high",
        },
    )).json()

    t2 = (await client.post(
        "/api/v1/tasks",
        json={"title": "second", "project_id": proj_id, "priority": "low"},
    )).json()

    # Get
    r = await client.get(f"/api/v1/tasks/{t1['id']}")
    assert r.status_code == 200 and r.json()["title"] == "first"

    # Filter by assignee
    r = await client.get("/api/v1/tasks", params={"assignee": "carol"})
    assert {t["id"] for t in r.json()} == {t1["id"]}

    # Patch with extra=forbid -- bogus field rejected with 422
    r = await client.patch(f"/api/v1/tasks/{t1['id']}", json={"bogus": True})
    assert r.status_code == 422

    # approved_at is a known-but-protected field: reject it with the stable
    # route contract instead of leaking Pydantic's generic 422 response.
    r = await client.patch(
        f"/api/v1/tasks/{t1['id']}",
        json={"approved_at": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "approved_at can only be set by the owner decision endpoint"
    )

    # Patch with valid status change
    r = await client.patch(
        f"/api/v1/tasks/{t1['id']}",
        json={"status": "in_progress", "scope": "small"},
    )
    assert r.status_code == 200 and r.json()["status"] == "in_progress"

    # Invalid status -> 400. status is no longer an enum on the wire: the
    # dictionaries decide what a name means, so the route answers, not pydantic.
    r = await client.patch(f"/api/v1/tasks/{t1['id']}", json={"status": "bogus"})
    assert r.status_code == 400 and "unknown status" in r.text

    r = await client.delete(f"/api/v1/tasks/{t2['id']}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/tasks/{t2['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Status-change audit (PATCH routes through Pipeline)
# ---------------------------------------------------------------------------


async def test_status_change_creates_audit_log(client: httpx.AsyncClient) -> None:
    """PATCH with status must write a task_log row (Pipeline path)."""
    t = (await client.post("/api/v1/tasks", json={"title": "audit-test"})).json()
    tid = t["id"]

    r = await client.patch(f"/api/v1/tasks/{tid}", json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress"

    log_r = await client.get(f"/api/v1/tasks/{tid}/log")
    assert log_r.status_code == 200, log_r.text
    entries = log_r.json()
    assert any(
        e["action"] == "status_changed" and e["new_value"] == "in_progress"
        for e in entries
    ), f"no status_changed log entry found; log={entries}"


async def test_blocked_requires_comment(client: httpx.AsyncClient) -> None:
    """Transition to blocked must be refused without a comment."""
    t = (await client.post("/api/v1/tasks", json={"title": "comment-guard"})).json()
    tid = t["id"]

    # Advance to in_progress first (only valid source for blocked)
    r = await client.patch(f"/api/v1/tasks/{tid}", json={"status": "in_progress"})
    assert r.status_code == 200, r.text

    # Missing comment -> 400
    r = await client.patch(f"/api/v1/tasks/{tid}", json={"status": "blocked"})
    assert r.status_code == 400, r.text
    assert "comment" in r.json().get("detail", "").lower()

    # With comment -> 200, comment appears on task
    r = await client.patch(
        f"/api/v1/tasks/{tid}",
        json={"status": "blocked", "comment": "waiting on external dependency"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "blocked"

    comments_r = await client.get(f"/api/v1/tasks/{tid}/comments")
    assert comments_r.status_code == 200, comments_r.text
    comments = comments_r.json()
    assert any(
        "waiting on external dependency" in c["content"] for c in comments
    ), f"status-change comment not found; comments={comments}"


async def test_status_with_pipeline_field(client: httpx.AsyncClient) -> None:
    """Status + whitelisted extra_fields in one PATCH is allowed."""
    t = (await client.post("/api/v1/tasks", json={"title": "mixed-ok"})).json()
    tid = t["id"]

    r = await client.patch(
        f"/api/v1/tasks/{tid}",
        json={"status": "in_progress", "scope": "small"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress"
    assert r.json()["scope"] == "small"


async def test_status_with_non_pipeline_field_rejected(client: httpx.AsyncClient) -> None:
    """Status + title (not in whitelist) in one PATCH must be refused."""
    t = (await client.post("/api/v1/tasks", json={"title": "mixed-bad"})).json()
    tid = t["id"]

    r = await client.patch(
        f"/api/v1/tasks/{tid}",
        json={"status": "in_progress", "title": "new name"},
    )
    assert r.status_code == 400, r.text
    assert "title" in r.json().get("detail", "")


# ---------------------------------------------------------------------------
# Ancillary
# ---------------------------------------------------------------------------


async def test_ancillary_round_trip(client: httpx.AsyncClient) -> None:
    proj_id = (await client.post(
        "/api/v1/projects", json={"slug": "p4", "title": "p4"}
    )).json()["id"]
    t1 = (await client.post(
        "/api/v1/tasks", json={"title": "anc-1", "project_id": proj_id}
    )).json()
    t2 = (await client.post(
        "/api/v1/tasks", json={"title": "anc-2", "project_id": proj_id}
    )).json()

    # Comments -- author from API key
    r = await client.post(
        f"/api/v1/tasks/{t1['id']}/comments",
        json={"content": "hello"},
    )
    assert r.status_code == 201
    assert r.json()["author"] == "system"

    r = await client.get(f"/api/v1/tasks/{t1['id']}/comments")
    assert len(r.json()) == 1

    # Relations
    r = await client.post(
        f"/api/v1/tasks/{t1['id']}/relations",
        json={"related_id": t2["id"], "relation": "child"},
    )
    assert r.status_code == 201
    rel_id = r.json()["id"]
    r = await client.delete(f"/api/v1/tasks/{t1['id']}/relations/{rel_id}")
    assert r.status_code == 204

    # Tags
    r = await client.post(f"/api/v1/tasks/{t1['id']}/tags", json={"tag": "bug"})
    assert r.status_code == 204
    r = await client.get(f"/api/v1/tasks/{t1['id']}/tags")
    assert r.json() == ["bug"]
    r = await client.delete(f"/api/v1/tasks/{t1['id']}/tags/bug")
    assert r.status_code == 204

    # Links
    r = await client.post(
        f"/api/v1/tasks/{t1['id']}/links",
        json={"kind": "pr", "url": "https://example.com/pr/1"},
    )
    assert r.status_code == 201
    link_id = r.json()["id"]

    # Commits
    r = await client.post(
        f"/api/v1/tasks/{t1['id']}/commits",
        json={"repo": "planner", "commit_hash": "deadbeef", "message": "init"},
    )
    assert r.status_code == 201
    r = await client.get(f"/api/v1/tasks/{t1['id']}/commits")
    assert len(r.json()) == 1

    # Log -- empty initially (no transitions wired yet, but endpoint must work)
    r = await client.get(f"/api/v1/tasks/{t1['id']}/log")
    assert r.status_code == 200 and isinstance(r.json(), list)

    r = await client.delete(f"/api/v1/tasks/{t1['id']}/links/{link_id}")
    assert r.status_code == 204
