"""Phase 4.5 live integration: full pipeline through real REST stack.

Drives an HTTP client against the uvicorn subprocess. Verifies the most
common lifecycle: create project -> create task -> walk pipeline -> done.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_health_reports_ok(live_client) -> None:
    r = await live_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["migrations_applied"] >= 1


async def test_full_pipeline_via_rest(live_client) -> None:
    # 1. Create project
    r = await live_client.post(
        "/api/v1/projects", json={"slug": "live", "title": "Live"}
    )
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]

    # 2. Create task
    r = await live_client.post(
        "/api/v1/tasks",
        json={"title": "Live task", "project_id": project_id},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    # 3. Walk via direct status updates (CRUD path, not pipeline transition).
    # Pipeline endpoints are not yet exposed via REST -- the in-process
    # Pipeline class handles transitions, but the live test verifies the
    # CRUD slice that REST currently surfaces.
    r = await live_client.patch(
        f"/api/v1/tasks/{task_id}", json={"status": "in_progress"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    r = await live_client.patch(
        f"/api/v1/tasks/{task_id}", json={"status": "done"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "done"


async def test_concurrent_create_and_read(live_client) -> None:
    """Two concurrent creates from the same client must both land cleanly."""
    import asyncio

    r = await live_client.post(
        "/api/v1/projects", json={"slug": "race", "title": "Race"}
    )
    assert r.status_code == 201
    pid = r.json()["id"]

    async def make_task(n: int):
        return await live_client.post(
            "/api/v1/tasks",
            json={"title": f"t{n}", "project_id": pid},
        )

    results = await asyncio.gather(*(make_task(i) for i in range(10)))
    assert all(r.status_code == 201 for r in results)

    r = await live_client.get(
        f"/api/v1/tasks?project_id={pid}&limit=200"
    )
    assert r.status_code == 200
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    titles = {t["title"] for t in items}
    assert {f"t{i}" for i in range(10)}.issubset(titles)


async def test_unauth_request_rejected(live_client) -> None:
    """Stripping the API key header must yield 401, not 500."""
    import httpx

    async with httpx.AsyncClient(
        base_url=str(live_client.base_url), timeout=5.0
    ) as anon:
        r = await anon.get("/api/v1/projects")
        assert r.status_code == 401


async def test_migrations_idempotent_on_running_db(live_client) -> None:
    """A second invocation of the migration runner must be a no-op.

    The fixture already ran migrations once. We hit /health to confirm the
    server still answers, and inspect the response again -- if a re-apply
    had failed, /health would have flagged degraded.
    """
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "DATABASE_URL": os.environ["PLANNER_TEST_DSN"],
        "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src"),
    }
    res = subprocess.run(
        [sys.executable, "-m", "planner.install"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    # Server still healthy after re-apply.
    r = await live_client.get("/health")
    assert r.json()["status"] == "ok"
