"""Role-axis chokepoint on routes/budgets.py.

Catalogue section 5.2 maps the five budget handlers to:

- POST  /api/budgets/{id}/notify-exhaustion  notify_budget_exhaustion -> budgets.notify_exhaustion
- GET   /api/budgets/check-warnings          check_spend_warnings     -> budgets.read
- GET   /api/budgets                         list_budgets             -> budgets.read
- POST  /api/budgets                         create_budget            -> budgets.create
- PATCH /api/budgets/{id}                    update_budget            -> budgets.update

All five permissions carry object_axis='none'.
budgets.read is granted to super-admin, admin and user; budgets.create,
budgets.update and budgets.notify_exhaustion to super-admin only.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED = {
    ("POST",  "/api/budgets/{budget_id}/notify-exhaustion"): "budgets.notify_exhaustion",
    ("GET",   "/api/budgets/check-warnings"):                "budgets.read",
    ("GET",   "/api/budgets"):                               "budgets.read",
    ("POST",  "/api/budgets"):                               "budgets.create",
    ("PATCH", "/api/budgets/{budget_id}"):                   "budgets.update",
}


def test_budgets_router_declares_catalogue_permissions():
    """Structural: every handler carries the catalogue permission name."""
    from routes import budgets
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in budgets.router.routes:
        for method in route.methods:
            if method == "HEAD":
                continue
            declared = route_permission(route)
            assert declared is not None, (
                f"{method} {route.path} has no requires() permission declaration"
            )
            seen[(method, route.path)] = declared[0]
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


def _build_app(monkeypatch, decision_for, role="user"):
    """Mini app wrapping the real budgets router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import budgets

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(budgets, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(budgets.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role=role,
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_create_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `budgets.create` is refused by the chokepoint
    before the handler ever runs; 403 names BOTH the role and the permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/budgets",
        json={"scope_type": "tenant", "scope_id": "1", "period": "daily",
              "limit_usd": 1.0},
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'budgets.create'" in detail


def test_list_handler_is_reached_when_role_axis_allows(monkeypatch):
    """A user-tier call on `budgets.read` is admitted; handler responds 200."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/budgets")
    assert r.status_code == 200, r.text
    assert r.json() == {"budgets": []}
