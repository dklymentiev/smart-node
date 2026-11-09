"""The permission gate on routes/system.py.

Every handler declares the catalogue's permission. Against the seeded matrix
in a throwaway database: an account with no role row reads the status and is
refused the runner state by name, an admin is refused the runner switch by
name, and the owner passes the gate and still meets the handler's own
signed-in-session rule.
"""

from __future__ import annotations

from fastapi import FastAPI

from permission_db import ADMIN, OWNER, PLAIN, call, declared, permission_db  # noqa: F401

# The router has no prefix of its own; main.py mounts it under /api/system.
EXPECTED = {
    ("GET", "/status"): ("system.status.read", False),
    ("GET", "/overview"): ("system.status.read", False),
    ("GET", "/services"): ("system.status.read", False),
    ("GET", "/crons"): ("system.status.read", False),
    ("GET", "/runner"): ("system.runner.read", False),
    ("POST", "/runner"): ("system.runner.control", False),
    ("GET", "/models"): ("system.status.read", False),
    ("POST", "/models"): ("system.models.control", False),
    ("GET", "/summary"): ("system.status.read", False),
}


def _app(monkeypatch) -> FastAPI:
    from routes import system

    # The switches and the unit probe act on the host. No test may get that
    # far, and the probe is answered here so a read never asks systemd.
    def _never(*args, **kwargs):
        raise AssertionError("a test reached the service switch")

    monkeypatch.setattr(system, "_service_control", _never)
    monkeypatch.setattr(system, "_unit_state", lambda unit: {"unit": unit, "active": "inactive"})
    app = FastAPI()
    app.include_router(system.router, prefix="/api/system")
    return app


def test_every_system_route_declares_the_catalogue_permission(permission_db, monkeypatch):
    from permissions import validate_routes
    from routes import system

    assert declared(system.router) == EXPECTED
    validate_routes(_app(monkeypatch), strict_undeclared=True)


def test_account_without_a_role_is_refused_the_runner_state_by_name(permission_db, monkeypatch):
    r = call(_app(monkeypatch), "GET", "/api/system/runner", PLAIN)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail and "'system.runner.read'" in detail


def test_account_without_a_role_reads_the_status(permission_db, monkeypatch):
    r = call(_app(monkeypatch), "GET", "/api/system/status", PLAIN)
    assert r.status_code == 200, r.text
    assert r.json()["db"] == {"reachable": True}


def test_admin_reads_the_runner_state(permission_db, monkeypatch):
    r = call(_app(monkeypatch), "GET", "/api/system/runner", ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["active"] == "inactive"


def test_admin_is_refused_the_runner_switch_by_name(permission_db, monkeypatch):
    r = call(_app(monkeypatch), "POST", "/api/system/runner", ADMIN, json={"action": "stop"})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'admin'" in detail and "'system.runner.control'" in detail


def test_the_session_rule_behind_the_gate_still_applies(permission_db, monkeypatch):
    """The owner holds system.models.control, and a forwarded name without the
    proxy's proof is still not a signed-in session. The refusal is the
    handler's own and the switch is not reached."""
    r = call(_app(monkeypatch), "POST", "/api/system/models", OWNER, proved=False,
             json={"action": "stop"})
    assert r.status_code in (403, 503), r.text
    detail = r.json()["detail"]
    assert "session" in detail and "system.models.control" not in detail
