"""Role-axis chokepoint on routes/integrations.py.

Per the 4a catalogue the handler-to-permission mapping is:

- GET    /api/integrations                  list_integrations       -> integrations.read
- GET    /api/integrations/{slug}           get_integration         -> integrations.read
- GET    /api/integrations/{slug}/tools     get_integration_tools   -> integrations.read
- POST   /api/integrations                  upsert_integration      -> integrations.write
- PUT    /api/integrations/{slug}           update_integration      -> integrations.write
- DELETE /api/integrations/{slug}           delete_integration      -> integrations.write

Both permissions carry object_axis='none'. integrations.read is granted to
super-admin, admin and user; integrations.write to super-admin and admin.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED = {
    ("GET",    "/api/integrations"):                "integrations.read",
    ("GET",    "/api/integrations/{slug}"):         "integrations.read",
    ("GET",    "/api/integrations/{slug}/tools"):   "integrations.read",
    ("POST",   "/api/integrations"):                "integrations.write",
    ("PUT",    "/api/integrations/{slug}"):         "integrations.write",
    ("DELETE", "/api/integrations/{slug}"):         "integrations.write",
}


def test_integrations_router_declares_catalogue_permissions():
    from routes import integrations
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in integrations.router.routes:
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
    import permissions
    import auth
    from routes import integrations

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(integrations, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(integrations.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=1, role=role,
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_write_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `integrations.write` is refused by the
    chokepoint before the handler runs; 403 names BOTH role and permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).delete("/api/integrations/anything")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'integrations.write'" in detail


def test_read_is_reached_when_role_axis_allows(monkeypatch):
    """A user-tier call on `integrations.read` is admitted; handler runs."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/integrations")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["integrations"] == []
