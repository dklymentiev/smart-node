"""The permission gate on routes/gateway_secrets.py.

Every handler declares the catalogue's permission; both are held by the owner
alone. Against the seeded matrix in a throwaway database: an admin is refused
by name on the read and on the write, the owner reaches the handler, and the
handler's own signed-in-session rule still applies behind the gate.
"""

from __future__ import annotations

from fastapi import FastAPI

from permission_db import ADMIN, OWNER, call, declared, permission_db  # noqa: F401

EXPECTED = {
    ("GET", "/api/gateway/cli/{slug}/env-vars"): ("gateway_secrets.read", False),
    ("PATCH", "/api/gateway/cli/{slug}/env-vars"): ("gateway_secrets.write", False),
    ("POST", "/api/gateway/secrets"): ("gateway_secrets.write", False),
    ("POST", "/api/gateway/secrets/delete"): ("gateway_secrets.write", False),
    ("POST", "/api/gateway/cli/{slug}/credential"): ("gateway_secrets.write", False),
    ("POST", "/api/gateway/cli/{slug}/login/start"): ("gateway_secrets.write", False),
    ("POST", "/api/gateway/cli/{slug}/login/complete"): ("gateway_secrets.write", False),
    ("POST", "/api/gateway/cli/{slug}/project"): ("gateway_secrets.write", False),
}

# Refused by the handler's own format check, before any key or store is read.
_MALFORMED = {"ref": "NOT A REF", "value": "x"}


def _app() -> FastAPI:
    from routes import gateway_secrets

    app = FastAPI()
    app.include_router(gateway_secrets.router)
    return app


def test_every_gateway_secret_route_declares_the_catalogue_permission(permission_db):
    from permissions import validate_routes
    from routes import gateway_secrets

    assert declared(gateway_secrets.router) == EXPECTED
    validate_routes(_app(), strict_undeclared=True)


def test_admin_is_refused_the_write_by_name(permission_db):
    r = call(_app(), "POST", "/api/gateway/secrets", ADMIN, json=_MALFORMED)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'admin'" in detail and "'gateway_secrets.write'" in detail


def test_admin_is_refused_the_read_by_name(permission_db):
    r = call(_app(), "GET", "/api/gateway/cli/claude/env-vars", ADMIN)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'admin'" in detail and "'gateway_secrets.read'" in detail


def test_owner_reaches_the_handler(permission_db):
    r = call(_app(), "POST", "/api/gateway/secrets", OWNER, json=_MALFORMED)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid ref"


def test_the_session_rule_behind_the_gate_still_applies(permission_db):
    """The owner holds gateway_secrets.read, and a forwarded name without the
    proxy's proof is still not a signed-in session. The refusal is the
    handler's own."""
    r = call(_app(), "GET", "/api/gateway/cli/claude/env-vars", OWNER, proved=False)
    assert r.status_code in (403, 503), r.text
    detail = r.json()["detail"]
    assert "session" in detail and "gateway_secrets.read" not in detail
