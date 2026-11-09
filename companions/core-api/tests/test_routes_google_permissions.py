"""The permission gate on routes/google.py.

Every handler declares the catalogue's permission. The seed grants all five
to every role, so the refusal is shown the only honest way: the pair is taken
out of this test's own matrix, the call is refused by name, and the pair is
put back.
"""

from __future__ import annotations

from fastapi import FastAPI

from permission_db import PLAIN, call, declared, permission_db  # noqa: F401

EXPECTED = {
    ("GET", "/api/integrations/google/connect"): ("google.oauth.connect", False),
    ("GET", "/api/integrations/google/callback"): ("google.oauth.connect", False),
    ("GET", "/api/integrations/google/status"): ("google.read", False),
    ("GET", "/api/integrations/google/token"): ("google.read", False),
    ("GET", "/api/integrations/google/picker-config"): ("google.read", False),
    ("POST", "/api/integrations/google/docs"): ("google.docs.create", False),
    ("POST", "/api/integrations/google/calendar/freebusy"): ("google.calendar.read", False),
    ("POST", "/api/integrations/google/calendar/events"): ("google.calendar.write", False),
}


def _app(monkeypatch) -> FastAPI:
    from routes import google

    # The handler reads the node's secret store; here it finds nothing there.
    monkeypatch.setattr(google, "_google", lambda: {})
    app = FastAPI()
    app.include_router(google.router)
    return app


def test_every_google_route_declares_the_catalogue_permission(permission_db, monkeypatch):
    from permissions import validate_routes
    from routes import google

    assert declared(google.router) == EXPECTED
    validate_routes(_app(monkeypatch), strict_undeclared=True)


def test_account_without_a_role_reaches_the_handler(permission_db, monkeypatch):
    r = call(_app(monkeypatch), "GET", "/api/integrations/google/status", PLAIN)
    assert r.status_code == 200, r.text
    assert r.json()["connected"] is False


def test_role_without_the_pair_is_refused_by_name(permission_db, monkeypatch):
    pair = ("user", "google.read")
    with permission_db(commit=True) as cur:
        cur.execute("DELETE FROM sn_config.role_permissions WHERE role = %s AND permission = %s", pair)
        assert cur.rowcount == 1
    try:
        r = call(_app(monkeypatch), "GET", "/api/integrations/google/status", PLAIN)
    finally:
        with permission_db(commit=True) as cur:
            cur.execute("INSERT INTO sn_config.role_permissions (role, permission) VALUES (%s, %s)", pair)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail and "'google.read'" in detail
