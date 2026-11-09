"""Role-axis chokepoint on routes/ai.py.

Catalogue section 5.2 maps the one AI handler to:

- GET /api/ai/status   ai_status  -> ai.status.read

The permission carries object_axis='none' and is granted to super-admin,
admin and user (user-visible read, section 4.1 of the catalogue).
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED = {
    ("GET", "/api/ai/status"): "ai.status.read",
}


def test_ai_router_declares_catalogue_permissions():
    """Structural: every handler carries the catalogue permission name."""
    from routes import ai
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in ai.router.routes:
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
    """Cursor that answers empty collections and a zero-row count.

    The ai_status handler reads `fetchone()["n"]` for the sessions-total
    count; returning {"n": 0} keeps the handler on the happy path with no
    database behind it. All list reads come back empty.
    """
    def execute(self, *a, **k):
        return None
    def fetchall(self):
        return []
    def fetchone(self):
        return {"n": 0}


@contextmanager
def _stub_cursor(*a, **k):
    yield _StubCur()


def _build_app(monkeypatch, decision_for):
    """Mini app wrapping the real ai router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import ai

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(ai, "cursor", _stub_cursor)
    monkeypatch.setattr(ai, "_gateway_health", lambda: None)

    app = FastAPI()
    app.include_router(ai.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role="user",
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_ai_status_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `ai.status.read` is refused by the chokepoint;
    the 403 detail names BOTH the role and the permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="installer", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'installer' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).get("/api/ai/status")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'installer'" in detail
    assert "'ai.status.read'" in detail


def test_ai_status_is_reached_when_role_axis_allows(monkeypatch):
    """A user-tier call on `ai.status.read` is admitted; handler responds."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/ai/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "summary" in body and "providers" in body
