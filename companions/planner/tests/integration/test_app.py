"""End-to-end tests for the FastAPI app skeleton.

httpx ASGITransport does not trigger ASGI lifespan events on its own, so we
enter the FastAPI ``router.lifespan_context`` manually before each request
batch. This mirrors what uvicorn does in production.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import pytest_asyncio

from planner.app import create_app


@pytest_asyncio.fixture
async def client(pool, monkeypatch):
    """Builds the app and lets its lifespan open a separate pool.

    The ``pool`` fixture is requested only for its side effect: running
    migrations and truncating the data tables so the health check reports
    a primed database.
    """
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_root(client: httpx.AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Planner"
    assert "version" in body


async def test_health_reports_db_and_migrations(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["migrations_applied"] >= 1


async def test_openapi_is_served(client: httpx.AsyncClient) -> None:
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "Planner"


async def test_unknown_path_returns_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/no-such-thing")
    assert r.status_code == 404
