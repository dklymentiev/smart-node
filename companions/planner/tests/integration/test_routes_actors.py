"""REST: /api/v1/actors."""

from __future__ import annotations

import os

import httpx
import pytest_asyncio

from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo


@pytest_asyncio.fixture
async def client(pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    # DD-008: pin apikey mode so the X-API-Key header binds actor=system.
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    app = create_app()
    async with app.router.lifespan_context(app):
        # Always create our own key so the test can inject the header.
        repo = ApiKeysRepo(app.state.pool)
        _, plaintext = await repo.create(label="t", actor_id="system")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-API-Key": plaintext},
        ) as c:
            yield c


async def test_create_get_list_update_deactivate(client: httpx.AsyncClient) -> None:
    # Create
    r = await client.post(
        "/api/v1/actors",
        json={"id": "alice", "display_name": "Alice", "type": "human"},
    )
    assert r.status_code == 201
    assert r.json()["id"] == "alice"

    # Get
    r = await client.get("/api/v1/actors/alice")
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice"

    # List with filter
    r = await client.get("/api/v1/actors", params={"type": "human"})
    rows = r.json()
    assert any(x["id"] == "alice" for x in rows)

    # Patch
    r = await client.patch(
        "/api/v1/actors/alice",
        json={"display_name": "Alice K."},
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice K."

    # Deactivate
    r = await client.delete("/api/v1/actors/alice")
    assert r.status_code == 200
    assert r.json()["active"] is False


async def test_create_invalid_id_returns_400(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/actors",
        json={"id": "Capital", "display_name": "X", "type": "human"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == 400


async def test_get_missing_returns_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/actors/no-such")
    assert r.status_code == 404


async def test_create_duplicate_returns_400(client: httpx.AsyncClient) -> None:
    payload = {"id": "dup", "display_name": "Dup", "type": "human"}
    r1 = await client.post("/api/v1/actors", json=payload)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/actors", json=payload)
    assert r2.status_code == 400


async def test_no_api_key_blocks_actors_endpoints(pool, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/actors")
            assert r.status_code == 401
