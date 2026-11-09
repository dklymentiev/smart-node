"""Role-axis chokepoint on routes/planner.py.

The planner route is a single ANY proxy under `/api/planner/{subpath:path}`.
The catalogue maps it to one permission, `planner.proxy`, with no object
axis. The route additionally runs an internal grant and identity gate
inside `_handle`, but that is a layer below the role axis and belongs to
its own tests.

Two facts:

1. The proxy route carries a `requires("planner.proxy")` dep that
   resolves to the catalogue permission name.

2. When the central check denies (role axis), the handler is NOT
   reached and the 403 names BOTH the role and the permission. When it
   allows, the request falls into `_handle` (which may then refuse for
   its own reasons; that is a different test).
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED_PERMISSION = "planner.proxy"
EXPECTED_PATH = "/api/planner/{subpath:path}"


def test_planner_router_declares_catalogue_permission():
    """Structural: the single proxy route carries the catalogue permission."""
    from routes import planner
    from permissions import route_permission

    saw = False
    for route in planner.router.routes:
        if route.path != EXPECTED_PATH:
            continue
        saw = True
        declared = route_permission(route)
        assert declared is not None, (
            f"{route.path} has no requires() permission declaration"
        )
        assert declared[0] == EXPECTED_PERMISSION, (
            f"{route.path} declared {declared[0]!r}, "
            f"catalogue says {EXPECTED_PERMISSION!r}"
        )
        assert declared[1] is False, (
            f"{route.path} declares an object fetcher but the catalogue "
            "places this permission on object_axis='none'"
        )
    assert saw, f"expected a route at {EXPECTED_PATH}"


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
    """Mini app wrapping the real planner router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import planner

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(planner.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role="user",
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    # The internal _handle resolves the actor itself; stub it to a fixed
    # name so the chokepoint is what the test is seeing, not the internal
    # DB lookup.
    app.dependency_overrides[auth.get_actor] = lambda: "test"
    return app


def test_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A user-tier call is refused by the chokepoint when the role has no
    `planner.proxy`; the 403 names BOTH the role and the permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).get("/api/planner/api/v1/tasks")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'planner.proxy'" in detail


def test_handler_is_reached_when_role_axis_allows(monkeypatch):
    """An admin-tier call falls through the chokepoint into `_handle`.

    `_handle` then runs its own event-emit path and tries to reach the
    actual planner, which is unavailable in a test. The test only
    asserts that the chokepoint did NOT short-circuit at 403 with a
    role-level denial message; anything from `_handle` after that
    (502 planner unreachable, etc.) is a different layer and is tested
    elsewhere.
    """
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="admin", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    # Make _handle's db cursor a no-op so emit() does not actually touch
    # Postgres while the test is proving the gate ran.
    import routes.planner as planner_mod
    monkeypatch.setattr(planner_mod, "cursor", _stub_cursor)

    def _unreachable(*a, **k):
        raise OSError("planner unreachable in test")

    monkeypatch.setattr(planner_mod, "_forward", _unreachable)

    r = TestClient(app, raise_server_exceptions=False).get("/api/planner/api/v1/tasks")
    # The role gate let the request through. `_handle` ran and returned
    # 502 because the planner is not reachable in the test. The point is
    # not 502 specifically; it is that we did NOT see the role denial.
    assert r.status_code != 403 or "'planner.proxy'" not in r.text, (
        f"chokepoint denied an admin on planner.proxy: {r.status_code} {r.text}"
    )
