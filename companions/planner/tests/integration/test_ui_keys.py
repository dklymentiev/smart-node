"""Integration tests for the API key management page."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from planner.app import create_app
from planner.auth import ApiKeysRepo
from planner.repos.actors import ActorsRepo


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(pool: asyncpg.Pool):
    await ActorsRepo(pool).create(
        actor_id="claude", display_name="Claude", type="agent"
    )
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        _, key = await ApiKeysRepo(pool).create(label="root", actor_id="system")
        c.cookies.set("planner_key", key)
        yield c, pool


async def test_keys_page_lists_existing(env) -> None:
    client, _ = env
    r = await client.get("/ui/keys")
    assert r.status_code == 200
    assert "root" in r.text
    assert "active" in r.text


async def test_keys_create_shows_plaintext_once(env) -> None:
    client, _ = env
    r = await client.post(
        "/ui/keys/create", data={"label": "ci-bot", "actor_id": "claude"}
    )
    assert r.status_code == 200
    assert "ci-bot" in r.text
    # Plaintext shown in the success block (planner_ prefix from auth.py).
    assert "pln_" in r.text


async def test_keys_create_validates_input(env) -> None:
    client, _ = env
    r = await client.post(
        "/ui/keys/create", data={"label": "", "actor_id": "claude"}
    )
    assert r.status_code == 200
    assert "label and actor are required" in r.text


async def test_keys_create_unknown_actor_shows_error(env) -> None:
    client, _ = env
    r = await client.post(
        "/ui/keys/create", data={"label": "x", "actor_id": "nobody"}
    )
    assert r.status_code == 200
    assert "unknown actor_id" in r.text or "FK" in r.text


async def test_keys_revoke_marks_revoked(env) -> None:
    client, pool = env
    repo = ApiKeysRepo(pool)
    new_row, _ = await repo.create(label="temp", actor_id="claude")
    r = await client.post(f"/ui/keys/{new_row.id}/revoke")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/keys"
    rows = await repo.list(include_revoked=True)
    revoked = [k for k in rows if k.id == new_row.id]
    assert revoked[0].revoked_at is not None


async def test_keys_redirects_unauth(pool) -> None:
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        r = await c.get("/ui/keys")
        assert r.status_code == 303
