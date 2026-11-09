"""Role-axis chokepoint on routes/models.py.

Two facts:

1. Every handler in `routes/models.py` carries a `requires("<name>")` dep
   that resolves to the catalogue name expected for that handler
   (`models.read` on the two reads; `models.write` on create / update /
   delete).

2. When the central check denies (role axis), the handler is NOT reached
   and the 403 names BOTH the role and the permission -- the shape the
   mechanism documents in `permissions.check_permission`. When it allows,
   the handler is reached.

This test lives next to `test_requires_permission.py` which covers the
chokepoint itself; this file is the per-route-file acceptance that the
chokepoint is actually wired to the real router.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED = {
    ("GET", "/api/models"): "models.read",
    ("GET", "/api/models/providers"): "models.read",
    ("POST", "/api/models"): "models.write",
    ("PATCH", "/api/models/{model_id:path}"): "models.write",
    ("DELETE", "/api/models/{model_id:path}"): "models.write",
}


def test_models_router_declares_catalogue_permissions():
    """Structural: every handler carries the catalogue permission name."""
    from routes import models
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in models.router.routes:
        for method in route.methods:
            if method == "HEAD":
                continue
            declared = route_permission(route)
            assert declared is not None, (
                f"{method} {route.path} has no requires() permission declaration"
            )
            seen[(method, route.path)] = declared[0]
            # None of these permissions has an object axis per the catalogue.
            assert declared[1] is False, (
                f"{method} {route.path} declares an object fetcher but the "
                "catalogue places this permission on object_axis='none'"
            )

    assert seen == EXPECTED, (
        "handler-to-permission mapping diverged from the catalogue; "
        f"got {sorted(seen.items())}"
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


def _build_app(monkeypatch, decision_for):
    """Mini app wrapping the real models router with DB and chokepoint stubbed.

    `decision_for(principal, name, object_id)` returns the PermissionDecision
    the test wants the chokepoint to produce. Every dependency on
    `get_principal` is overridden via `app.dependency_overrides` so the test
    can run without `auth.get_principal` touching Postgres.
    """
    import permissions
    import auth
    from routes import models

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(models, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(models.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role="user",
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A user-tier call on `models.read` is refused by the chokepoint; the 403
    detail names BOTH the role and the permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).get("/api/models")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'models.read'" in detail


def test_handler_is_reached_when_role_axis_allows(monkeypatch):
    """An admin-tier call on `models.read` is admitted; the chokepoint lets
    the request fall through to the handler and the response carries a
    `models` list (empty because the DB cursor is stubbed)."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="admin", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/models")
    assert r.status_code == 200, r.text
    assert r.json() == {"models": []}
