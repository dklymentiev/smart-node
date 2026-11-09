"""A trusted key may name the caller it acts for; an ordinary key may not.

These cover migration 0005 / the ``can_delegate`` branch in
``planner.auth.require_api_key``.

Every positive here has its negative built beside it on purpose. A gate that is
only ever handed the honest request passes whether or not it is a gate, and the
property being protected is precisely the dishonest case: a key WITHOUT
delegation sending a ``Remote-User`` must not have that name honoured. If the
delegation branch were deleted, the tests marked "the negative" below are the
ones that go red.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio

from planner.app import create_app
from planner.auth import ApiKeysRepo, ENV_AUTH_MODE, ENV_PROXY_HEADER


@pytest_asyncio.fixture
async def apikey_app(pool, monkeypatch):
    """The app in `apikey` mode, plus a factory for keys bound to real actors."""
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")

    @asynccontextmanager
    async def _build(*, proxy_header: str | None = None):
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


async def _actor(pool, actor_id: str) -> str:
    await pool.execute(
        "INSERT INTO actors (id, display_name, type) VALUES ($1, $1, 'system') "
        "ON CONFLICT (id) DO NOTHING",
        actor_id,
    )
    return actor_id


async def _key(pool, *, label: str, actor: str, delegate: bool) -> str:
    await _actor(pool, actor)
    _, plaintext = await ApiKeysRepo(pool).create(
        label=label, actor_id=actor, can_delegate=delegate
    )
    return plaintext


class TestDelegatingKey:
    async def test_names_the_caller_it_acts_for(self, apikey_app, pool) -> None:
        key = await _key(pool, label="front-door", actor="system", delegate=True)
        async with apikey_app() as (client, _):
            r = await client.get(
                "/whoami",
                headers={"X-API-Key": key, "Remote-User": "alice"},
            )
        assert r.status_code == 200
        assert r.json()["actor_id"] == "alice"

    async def test_materialises_an_unseen_actor(self, apikey_app, pool) -> None:
        key = await _key(pool, label="front-door", actor="system", delegate=True)
        async with apikey_app() as (client, app):
            await client.get(
                "/whoami", headers={"X-API-Key": key, "Remote-User": "carol"}
            )
            row = await app.state.pool.fetchrow(
                "SELECT id FROM actors WHERE id = 'carol'"
            )
        assert row is not None

    async def test_without_the_header_authors_as_itself(self, apikey_app, pool) -> None:
        """The door's own housekeeping calls are its own, not somebody's."""
        key = await _key(pool, label="front-door", actor="system", delegate=True)
        async with apikey_app() as (client, _):
            r = await client.get("/whoami", headers={"X-API-Key": key})
        assert r.status_code == 200
        assert r.json()["actor_id"] == "system"

    @pytest.mark.parametrize("raw", ["...", "@@@", "   "])
    async def test_unusable_header_is_refused_not_silently_ignored(
        self, apikey_app, pool, raw: str
    ) -> None:
        """Falling back to the key owner would launder an unusable name into a
        real one. A blank-ish value is the interesting case: it must not read as
        'no header supplied'."""
        key = await _key(pool, label="front-door", actor="system", delegate=True)
        async with apikey_app() as (client, _):
            r = await client.get(
                "/whoami", headers={"X-API-Key": key, "Remote-User": raw}
            )
        if raw.strip():
            assert r.status_code == 401
        else:
            # Whitespace only is "no header", so it authors as the key owner.
            assert r.status_code == 200
            assert r.json()["actor_id"] == "system"

    async def test_honours_a_renamed_proxy_header(self, apikey_app, pool) -> None:
        key = await _key(pool, label="front-door", actor="system", delegate=True)
        async with apikey_app(proxy_header="X-Forwarded-User") as (client, _):
            r = await client.get(
                "/whoami",
                headers={"X-API-Key": key, "X-Forwarded-User": "dave"},
            )
        assert r.status_code == 200
        assert r.json()["actor_id"] == "dave"


class TestOrdinaryKey:
    async def test_the_negative_header_is_ignored_entirely(
        self, apikey_app, pool
    ) -> None:
        """THE property. A key with no delegation sends Remote-User naming
        somebody else and is still recorded as itself."""
        key = await _key(pool, label="runner", actor="runner", delegate=False)
        async with apikey_app() as (client, _):
            r = await client.get(
                "/whoami",
                headers={"X-API-Key": key, "Remote-User": "admin"},
            )
        assert r.status_code == 200
        assert r.json()["actor_id"] == "runner"

    async def test_the_negative_unusable_header_is_not_an_error(
        self, apikey_app, pool
    ) -> None:
        """For a key that cannot delegate the header does not exist, so a
        malformed one is not its problem and must not fail the request."""
        key = await _key(pool, label="runner", actor="runner", delegate=False)
        async with apikey_app() as (client, _):
            r = await client.get(
                "/whoami", headers={"X-API-Key": key, "Remote-User": "..."}
            )
        assert r.status_code == 200
        assert r.json()["actor_id"] == "runner"

    async def test_default_is_not_delegating(self, pool) -> None:
        """A key created without asking cannot speak for anyone. The column
        default is the whole safety of an upgrade: every key that existed
        before migration 0005 lands here."""
        row, _ = await ApiKeysRepo(pool).create(
            label="plain", actor_id=await _actor(pool, "someone")
        )
        assert row.can_delegate is False


class TestTheDoorIsClosed:
    async def test_no_key_is_refused(self, apikey_app) -> None:
        async with apikey_app() as (client, _):
            r = await client.get("/whoami")
        assert r.status_code == 401

    async def test_a_bogus_key_is_refused(self, apikey_app) -> None:
        async with apikey_app() as (client, _):
            r = await client.get("/whoami", headers={"X-API-Key": "pln_nope"})
        assert r.status_code == 401


class TestAttributionReachesTheRecord:
    """whoami proves the resolution; these prove it lands on a real row.

    This is the property the held-reply gate enforces: brin releases a held reply only when
    a comment's author equals the approval owner, so an approval that arrives
    authored by the front door instead of the owner is an approval that never
    fires.
    """

    @staticmethod
    async def _task(client, key: str) -> int:
        r = await client.post(
            "/api/v1/tasks",
            headers={"X-API-Key": key},
            json={"title": "attribution probe"},
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def test_delegated_comment_is_authored_by_the_named_caller(
        self, apikey_app, pool
    ) -> None:
        key = await _key(pool, label="front-door", actor="system", delegate=True)
        async with apikey_app() as (client, _):
            tid = await self._task(client, key)
            r = await client.post(
                f"/api/v1/tasks/{tid}/comments",
                headers={"X-API-Key": key, "Remote-User": "admin"},
                json={"content": "approve", "comment_type": "approval"},
            )
        assert r.status_code == 201, r.text
        assert r.json()["author"] == "admin"

    async def test_the_negative_ordinary_key_authors_as_itself(
        self, apikey_app, pool
    ) -> None:
        key = await _key(pool, label="runner", actor="runner", delegate=False)
        async with apikey_app() as (client, _):
            tid = await self._task(client, key)
            r = await client.post(
                f"/api/v1/tasks/{tid}/comments",
                headers={"X-API-Key": key, "Remote-User": "admin"},
                json={"content": "approve", "comment_type": "approval"},
            )
        assert r.status_code == 201, r.text
        assert r.json()["author"] == "runner"
