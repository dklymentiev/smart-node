"""Integration tests for DD-008 auth modes.

Spins the ASGI app under each ``PLANNER_AUTH_MODE`` and verifies the
``/whoami`` endpoint, actor materialisation, and ``/ui/login`` redirect
behaviour. Existing ``test_auth.py`` covers the ``apikey`` lookup-by-key
path in depth; here we focus on the mode switch itself.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio

from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ENV_PROXY_HEADER


@pytest_asyncio.fixture
async def app_under_mode(pool, monkeypatch):
    """Build the app with a chosen auth mode; yields a callable client factory."""
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])

    @asynccontextmanager
    async def _build(mode: str, *, proxy_header: str | None = None):
        monkeypatch.setenv(ENV_AUTH_MODE, mode)
        if proxy_header is not None:
            monkeypatch.setenv(ENV_PROXY_HEADER, proxy_header)
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as c:
                yield c, app

    return _build


# ---------------------------------------------------------------------------
# dev mode
# ---------------------------------------------------------------------------


class TestDevMode:
    async def test_whoami_autologs_as_dev_actor(self, app_under_mode) -> None:
        async with app_under_mode("dev") as (client, _):
            r = await client.get("/whoami")
            assert r.status_code == 200
            body = r.json()
            assert body["actor_id"] == "dev"
            assert body["label"] == "dev-auto"
            assert body["key_id"] == 0  # synthetic key

    async def test_whoami_ignores_api_key_header(self, app_under_mode) -> None:
        async with app_under_mode("dev") as (client, _):
            r = await client.get("/whoami", headers={"X-API-Key": "pln_bogus"})
            assert r.status_code == 200
            assert r.json()["actor_id"] == "dev"

    async def test_login_page_redirects_to_dashboard(self, app_under_mode) -> None:
        async with app_under_mode("dev") as (client, _):
            r = await client.get("/ui/login", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/ui/"

    async def test_dev_actor_seeded_in_db(self, app_under_mode, pool) -> None:
        async with app_under_mode("dev") as (_, app):
            row = await app.state.pool.fetchrow(
                "SELECT id, display_name FROM actors WHERE id = 'dev'"
            )
            assert row is not None
            assert row["display_name"] == "Local Dev"


# ---------------------------------------------------------------------------
# proxy mode
# ---------------------------------------------------------------------------


class TestProxyMode:
    async def test_missing_remote_user_returns_401(self, app_under_mode) -> None:
        async with app_under_mode("proxy") as (client, _):
            r = await client.get("/whoami")
            assert r.status_code == 401
            assert "Remote-User" in r.json()["detail"]

    async def test_valid_remote_user_returns_200(self, app_under_mode) -> None:
        async with app_under_mode("proxy") as (client, _):
            r = await client.get("/whoami", headers={"Remote-User": "alice"})
            assert r.status_code == 200
            body = r.json()
            assert body["actor_id"] == "alice"
            assert body["label"] == "proxy:alice"
            assert body["key_id"] == 0

    async def test_actor_materialised_on_first_sight(
        self, app_under_mode, pool
    ) -> None:
        async with app_under_mode("proxy") as (client, app):
            existing = await app.state.pool.fetchval(
                "SELECT COUNT(*) FROM actors WHERE id = 'bob'"
            )
            assert existing == 0
            r = await client.get("/whoami", headers={"Remote-User": "bob"})
            assert r.status_code == 200
            after = await app.state.pool.fetchrow(
                "SELECT id, type FROM actors WHERE id = 'bob'"
            )
            assert after is not None
            assert after["type"] == "human"

    async def test_repeated_requests_do_not_duplicate_actor(
        self, app_under_mode, pool
    ) -> None:
        async with app_under_mode("proxy") as (client, app):
            for _ in range(3):
                r = await client.get("/whoami", headers={"Remote-User": "carol"})
                assert r.status_code == 200
            count = await app.state.pool.fetchval(
                "SELECT COUNT(*) FROM actors WHERE id = 'carol'"
            )
            assert count == 1

    async def test_remote_user_normalised(self, app_under_mode) -> None:
        async with app_under_mode("proxy") as (client, _):
            r = await client.get(
                "/whoami", headers={"Remote-User": "Alice@Example.com"}
            )
            assert r.status_code == 200
            assert r.json()["actor_id"] == "alice.example.com"

    async def test_invalid_remote_user_returns_401(self, app_under_mode) -> None:
        async with app_under_mode("proxy") as (client, _):
            r = await client.get("/whoami", headers={"Remote-User": "..."})
            assert r.status_code == 401

    async def test_custom_proxy_header(self, app_under_mode) -> None:
        async with app_under_mode(
            "proxy", proxy_header="X-Forwarded-User"
        ) as (client, _):
            # The default Remote-User header is ignored when a custom one is set.
            r = await client.get("/whoami", headers={"Remote-User": "alice"})
            assert r.status_code == 401
            r = await client.get(
                "/whoami", headers={"X-Forwarded-User": "dave"}
            )
            assert r.status_code == 200
            assert r.json()["actor_id"] == "dave"

    async def test_login_page_redirects_to_dashboard(self, app_under_mode) -> None:
        async with app_under_mode("proxy") as (client, _):
            r = await client.get("/ui/login", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/ui/"


# ---------------------------------------------------------------------------
# apikey mode (legacy, kept in parity with v0.1.0)
# ---------------------------------------------------------------------------


class TestApiKeyMode:
    async def test_missing_key_returns_401(self, app_under_mode) -> None:
        async with app_under_mode("apikey") as (client, _):
            r = await client.get("/whoami")
            assert r.status_code == 401
            assert "X-API-Key" in r.json()["detail"]

    async def test_invalid_key_returns_401(self, app_under_mode) -> None:
        async with app_under_mode("apikey") as (client, _):
            r = await client.get("/whoami", headers={"X-API-Key": "pln_bogus"})
            assert r.status_code == 401

    async def test_login_page_renders(self, app_under_mode) -> None:
        async with app_under_mode("apikey") as (client, _):
            r = await client.get("/ui/login", follow_redirects=False)
            assert r.status_code == 200
            assert "X-API-Key" in r.text or "API key" in r.text or "key" in r.text


# ---------------------------------------------------------------------------
# Mode mis-configuration is rejected at startup
# ---------------------------------------------------------------------------


class TestInvalidMode:
    async def test_invalid_mode_blows_up_at_lifespan(
        self, pool, monkeypatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
        monkeypatch.setenv(ENV_AUTH_MODE, "garbage")
        app = create_app()
        with pytest.raises(RuntimeError) as exc:
            async with app.router.lifespan_context(app):
                pass
        assert ENV_AUTH_MODE in str(exc.value)
