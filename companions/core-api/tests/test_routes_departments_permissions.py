"""Role-axis chokepoint on routes/departments.py.

Catalogue section 5.5 maps the one departments handler to:

- GET /api/departments  list_departments -> departments.read

The permission carries object_axis='none' and is granted to super-admin,
admin and user (section 4.3 of the catalogue, seeded in B02).
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED = {
    ("GET", "/api/departments"): "departments.read",
}


def test_departments_router_declares_catalogue_permissions():
    """Structural: the handler carries the catalogue permission name."""
    from routes import departments
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in departments.router.routes:
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
    """Mini app wrapping the real departments router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import departments

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(departments, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(departments.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role=role,
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_read_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `departments.read` is refused by the
    chokepoint before the handler ever runs; 403 names BOTH role and permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).get("/api/departments")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'departments.read'" in detail


def test_read_handler_is_reached_when_role_axis_allows(monkeypatch):
    """A user-tier call on `departments.read` is admitted; handler responds 200."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/departments")
    assert r.status_code == 200, r.text
    assert r.json() == {"departments": []}
