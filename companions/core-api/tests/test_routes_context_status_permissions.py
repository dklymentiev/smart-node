"""Role-axis chokepoint on routes/context_status.py.

Catalogue section 5.2 maps the two context-status handlers (the canonical
path and its deprecated desks alias) to:

- GET /api/context-status        context_status -> context_status.read
- GET /api/desks/context-status  context_status -> context_status.read

The permission carries object_axis='none' and is granted to super-admin,
admin and user (section 4.3 of the catalogue, seeded in B02).
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED = {
    ("GET", "/api/context-status"):       "context_status.read",
    ("GET", "/api/desks/context-status"): "context_status.read",
}


def test_context_status_router_declares_catalogue_permissions():
    """Structural: every handler carries the catalogue permission name."""
    from routes import context_status
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in context_status.router.routes:
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
    """Mini app wrapping the real context_status router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import context_status

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(context_status, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(context_status.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role=role,
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_read_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `context_status.read` is refused by the
    chokepoint before the handler ever runs; 403 names BOTH role and permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).get("/api/context-status")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'context_status.read'" in detail


def test_read_handler_is_reached_when_role_axis_allows(monkeypatch):
    """A user-tier call on `context_status.read` is admitted; handler responds 200."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    # scope=desks is the ordinary tenant-member path; scope=active is the
    # cross-tenant monitor view and is refused by the handler body itself for
    # a non-cross-tenant principal (independent of the role chokepoint).
    r = TestClient(app, raise_server_exceptions=False).get(
        "/api/context-status?scope=desks"
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"scope": "desks", "contexts": []}
