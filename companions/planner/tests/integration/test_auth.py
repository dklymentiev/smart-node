"""API key auth tests."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import pytest_asyncio

from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo


@pytest_asyncio.fixture
async def app_with_pool(pool, monkeypatch):
    """Build app and trigger lifespan; return (client, root_plaintext)."""
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    # DD-008: this file exercises the X-API-Key path; pin to apikey mode.
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    app = create_app()

    @asynccontextmanager
    async def _captured():
        # Capture stdout output from lifespan to discover the root key plaintext.
        # We can't easily intercept print() here; instead we rely on the fact
        # that no keys exist (truncated) and create our own via the repo.
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c, app

    return _captured


async def test_health_does_not_require_key(app_with_pool) -> None:
    async with app_with_pool() as (client, _):
        r = await client.get("/health")
        assert r.status_code == 200


async def test_whoami_requires_key(app_with_pool) -> None:
    async with app_with_pool() as (client, _):
        r = await client.get("/whoami")
        assert r.status_code == 401
        body = r.json()
        assert body["status"] == 401
        assert "X-API-Key" in body["detail"] or "API-Key" in body["detail"]


async def test_whoami_rejects_unknown_key(app_with_pool) -> None:
    async with app_with_pool() as (client, _):
        r = await client.get("/whoami", headers={"X-API-Key": "pln_bogus"})
        assert r.status_code == 401


async def test_whoami_accepts_valid_key(app_with_pool, pool) -> None:
    async with app_with_pool() as (client, app):
        repo = ApiKeysRepo(app.state.pool)
        _, plaintext = await repo.create(label="test", actor_id="system")
        r = await client.get("/whoami", headers={"X-API-Key": plaintext})
        assert r.status_code == 200
        body = r.json()
        assert body["actor_id"] == "system"
        assert body["label"] == "test"


async def test_revoked_key_is_rejected(app_with_pool, pool) -> None:
    async with app_with_pool() as (client, app):
        repo = ApiKeysRepo(app.state.pool)
        row, plaintext = await repo.create(label="r", actor_id="system")
        ok = await repo.revoke(row.id)
        assert ok is True
        r = await client.get("/whoami", headers={"X-API-Key": plaintext})
        assert r.status_code == 401


async def test_inactive_actor_invalidates_key(app_with_pool, pool) -> None:
    async with app_with_pool() as (client, app):
        # Create a fresh actor + key, deactivate the actor, expect 401.
        async with app.state.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO actors (id, display_name, type) VALUES ('eve', 'Eve', 'human')"
            )
        repo = ApiKeysRepo(app.state.pool)
        _, plaintext = await repo.create(label="eve-key", actor_id="eve")
        async with app.state.pool.acquire() as conn:
            await conn.execute("UPDATE actors SET active=false WHERE id='eve'")
        r = await client.get("/whoami", headers={"X-API-Key": plaintext})
        assert r.status_code == 401
