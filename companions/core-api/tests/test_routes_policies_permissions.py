"""Role-axis chokepoint on routes/policies.py.

Catalogue section 5.11 maps the four policy handlers to:

- GET  /api/policies           list_policies  -> policies.read
- POST /api/policies           create_policy  -> policies.write
- PATCH  /api/policies/{slug}  update_policy  -> policies.write
- DELETE /api/policies/{slug}  delete_policy  -> policies.write

Both permissions carry object_axis='none'. policies.read is granted to
super-admin, admin and user; policies.write to super-admin and admin.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


# Router is included at /api/policies by main.py; the paths below reflect
# what FastAPI reports for the router in isolation.
EXPECTED = {
    ("GET", ""): "policies.read",
    ("POST", ""): "policies.write",
    ("PATCH", "/{slug}"): "policies.write",
    ("DELETE", "/{slug}"): "policies.write",
}


def test_policies_router_declares_catalogue_permissions():
    """Structural: every handler carries the catalogue permission name."""
    from routes import policies
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in policies.router.routes:
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


def _build_app(monkeypatch, decision_for):
    """Mini app wrapping the real policies router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import policies

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(policies, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(policies.router, prefix="/api/policies")

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role="user",
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_write_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A user-tier call on `policies.write` is refused by the chokepoint;
    the 403 detail names BOTH the role and the permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/policies",
        json={"category": "safety", "title": "x", "body_md": "y",
              "rationale": "test"},
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'policies.write'" in detail


def test_read_handler_is_reached_when_role_axis_allows(monkeypatch):
    """A user-tier call on `policies.read` is admitted; handler responds."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/policies")
    assert r.status_code == 200, r.text
    assert r.json() == {"policies": [], "collisions": []}
