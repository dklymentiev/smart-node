"""Role-axis chokepoint on routes/events.py.

Per the 4a catalogue the handler-to-permission mapping is:

- GET  /api/events            list_events       -> events.read
- GET  /api/events/histogram  events_histogram  -> events.read
- POST /api/events            post_event        -> internal.events.write

internal.events.write is held by no role and admitted by trusted origin: a
node service that proves itself and is listed for it, or the owner. The
structural check asserts all three declarations; the last tests ask the real
chain, with real identity and the seeded catalogue, for a real refusal and a
real admission of the write.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from permission_db import OWNER, PLAIN, permission_db, session  # noqa: F401


EXPECTED = {
    ("GET", "/api/events"): "events.read",
    ("GET", "/api/events/histogram"): "events.read",
    ("POST", "/api/events"): "internal.events.write",
}

UNDECLARED: set = set()


def test_events_router_declares_catalogue_permissions():
    """Structural: GETs carry events.read, POST carries internal.events.write."""
    from routes import events
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    undeclared: set[tuple[str, str]] = set()
    for route in events.router.routes:
        for method in route.methods:
            if method == "HEAD":
                continue
            # The router mounts under /api/events; route.path is relative.
            full = "/api/events" + route.path
            declared = route_permission(route)
            if declared is None:
                undeclared.add((method, full))
                continue
            seen[(method, full)] = declared[0]
            assert declared[1] is False, (
                f"{method} {full} declares an object fetcher but the "
                "catalogue places this permission on object_axis='none'"
            )
    assert seen == EXPECTED, (
        "handler-to-permission mapping diverged from the catalogue; "
        f"got {sorted(seen.items())}"
    )
    assert undeclared == UNDECLARED, (
        "unexpected set of undeclared routes on events.router; "
        f"got {sorted(undeclared)}"
    )


class _StubCur:
    def execute(self, *a, **k):
        return None
    def fetchall(self):
        return []
    def fetchone(self):
        return None


@contextmanager
def _stub_cursor(*a, **k):
    yield _StubCur()


def _build_app(monkeypatch, decision_for, role="user"):
    """Mini app wrapping the real events router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import events

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(events, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(events.router, prefix="/api/events")

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=1, role=role,
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_read_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `events.read` is refused by the chokepoint
    before the handler ever runs; 403 names BOTH role and permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).get("/api/events")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'events.read'" in detail


def test_read_handler_is_reached_when_role_axis_allows(monkeypatch):
    """A user-tier call on `events.read` is admitted; handler returns 200."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/events")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events"] == []
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# the real chain: real identity, real `requires()`, the seeded catalogue
# ---------------------------------------------------------------------------

SECRET = "s" * 64


def _send(app, method, path, headers=None, **kwargs):
    import asyncio

    import httpx

    async def _go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as c:
            return await c.request(method, path, headers=headers or {}, **kwargs)

    return asyncio.run(_go())


def _service(name, secret=SECRET):
    return {"X-SN-Service": name, "X-SN-Service-Auth": secret}


EVENT = {"actor": "component:test-writer", "event_type": "test.written",
         "entity_type": "test", "entity_id": "one", "payload": {}}


@pytest.fixture
def events_app(permission_db, monkeypatch):
    from routes import events

    monkeypatch.setenv("SN_SERVICE_AUTH_SECRET", SECRET)
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS",
                       "brin:internal.events.write;voice:internal.messages.voice.write")
    app = FastAPI()
    app.include_router(events.router, prefix="/api/events")
    return app


def test_event_write_is_refused_without_a_trusted_origin(events_app):
    for headers in ({}, session(PLAIN), {"Remote-User": "runner"},
                    _service("voice"), _service("brin", "w" * 64)):
        r = _send(events_app, "POST", "/api/events", headers, json=EVENT)
        assert r.status_code == 403, (headers, r.text)
        assert "'internal.events.write'" in r.json()["detail"]


def test_event_write_is_admitted_for_a_listed_service_and_for_the_owner(events_app, permission_db):
    r = _send(events_app, "POST", "/api/events", _service("brin"), json=EVENT)
    assert r.status_code == 201, r.text
    with permission_db() as cur:
        cur.execute("SELECT actor, event_type FROM sn_core.events WHERE id = %s", (r.json()["id"],))
        row = cur.fetchone()
        cur.execute("SELECT payload FROM sn_core.events WHERE entity_type = 'permission' "
                    "AND entity_id = 'internal.events.write' ORDER BY id DESC LIMIT 1")
        audit = cur.fetchone()["payload"]
    assert (row["actor"], row["event_type"]) == ("component:test-writer", "test.written")
    assert audit["channel"] == "service" and audit["principal"] == "brin"

    # The node's root tools reach this as the owner.
    r = _send(events_app, "POST", "/api/events", session(OWNER), json=EVENT)
    assert r.status_code == 201, r.text
