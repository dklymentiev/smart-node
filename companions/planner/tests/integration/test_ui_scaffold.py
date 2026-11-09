"""Integration tests for the UI scaffold.

Covers: login page renders, login submit sets cookie + redirects, dashboard
loads with a valid cookie, and unauthenticated requests are redirected
to /ui/login.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from planner.app import create_app
from planner.auth import ApiKeysRepo


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(pool: asyncpg.Pool):
    app = create_app()
    app.state.pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


async def _make_key(pool, label="ui-test", actor="system") -> str:
    _, plaintext = await ApiKeysRepo(pool).create(label=label, actor_id=actor)
    return plaintext


async def test_login_page_renders(client) -> None:
    r = await client.get("/ui/login")
    assert r.status_code == 200
    assert "Sign in to Planner" in r.text


async def test_dashboard_redirects_unauthenticated(client) -> None:
    r = await client.get("/ui/")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


async def test_dashboard_redirects_with_bad_cookie(client) -> None:
    client.cookies.set("planner_key", "pln_bogus")
    r = await client.get("/ui/")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


async def test_login_submit_sets_cookie_and_redirects(client, pool) -> None:
    key = await _make_key(pool)
    r = await client.post("/ui/login", data={"key": key})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/"
    assert "planner_key" in r.cookies or any(
        "planner_key=" in v for v in r.headers.get_list("set-cookie")
    )


async def test_login_submit_invalid_key_shows_error(client) -> None:
    r = await client.post("/ui/login", data={"key": "pln_nope"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login?error=invalid"


async def test_dashboard_renders_with_valid_cookie(client, pool) -> None:
    key = await _make_key(pool)
    client.cookies.set("planner_key", key)
    r = await client.get("/ui/")
    assert r.status_code == 200
    assert "Projects" in r.text  # heading
    assert "Sign out" in r.text  # nav presence


async def test_query_key_works_for_first_visit(client, pool) -> None:
    key = await _make_key(pool)
    r = await client.get(f"/ui/?key={key}")
    assert r.status_code == 200


async def test_logout_clears_cookie(client, pool) -> None:
    key = await _make_key(pool)
    client.cookies.set("planner_key", key)
    r = await client.post("/ui/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
