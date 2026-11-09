"""The permission gate on routes/crons.py.

Every handler declares the catalogue's permission, and the declaration is
measured against the seeded matrix in a throwaway database: an account with no
role row is refused by name, an admin reaches the handler, and the handler's
own cross-tenant rule still applies behind the gate.
"""

from __future__ import annotations

from fastapi import FastAPI

from permission_db import ADMIN, PLAIN, call, declared, permission_db  # noqa: F401

EXPECTED = {
    ("GET", "/api/crons"): ("crons.read", False),
    ("GET", "/api/crons/panel"): ("crons.read", False),
    ("POST", "/api/crons/render"): ("crons.render", False),
    ("GET", "/api/crons/{cron_id}"): ("crons.read", False),
    ("POST", "/api/crons"): ("crons.write", False),
    ("PUT", "/api/crons/{cron_id}"): ("crons.write", False),
    ("DELETE", "/api/crons/{cron_id}"): ("crons.write", False),
    ("GET", "/api/crons/{cron_id}/runs"): ("crons.runs.read", False),
    ("POST", "/api/crons/{cron_id}/runs"): ("crons.runs.write", False),
    ("POST", "/api/crons/by-job/{job}/runs"): ("crons.runs.write", False),
    ("POST", "/api/crons/by-job/{job}/runs/start"): ("crons.runs.write", False),
    ("POST", "/api/crons/runs/{run_id}/finish"): ("crons.runs.write", False),
}


def _app() -> FastAPI:
    from routes import crons

    app = FastAPI()
    app.include_router(crons.router)
    return app


def test_every_cron_route_declares_the_catalogue_permission(permission_db):
    from permissions import validate_routes
    from routes import crons

    assert declared(crons.router) == EXPECTED
    # The names exist in the seeded catalogue and none needs an object.
    validate_routes(_app(), strict_undeclared=True)


def test_account_without_a_role_is_refused_by_name(permission_db):
    r = call(_app(), "GET", "/api/crons", PLAIN)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail and "'crons.read'" in detail


def test_admin_reaches_the_handler(permission_db):
    r = call(_app(), "GET", "/api/crons", ADMIN)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"crons", "count"}


def test_the_handler_rule_behind_the_gate_still_applies(permission_db):
    """An admin holds crons.render, and the handler still keeps the node
    crontab to a cross-tenant account. The refusal is the handler's own."""
    r = call(_app(), "POST", "/api/crons/render", ADMIN)
    assert r.status_code == 403, r.text
    assert "cross-tenant" in r.json()["detail"]


def test_the_refusal_to_render_says_the_timer_does_it(permission_db):
    """A 403 alone reads as 'ask the owner'. It must say registered jobs do not need the call."""
    r = call(_app(), "POST", "/api/crons/render", ADMIN)
    assert r.status_code == 403, r.text
    assert "smart-node-cron-reconcile.timer" in r.json()["detail"]
