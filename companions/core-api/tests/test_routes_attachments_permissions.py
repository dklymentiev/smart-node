"""Role-axis chokepoint on routes/attachments.py.

Catalogue section 5.2 maps the three attachment handlers to the same
permission:

- GET /api/attachments/{id}          get_attachment_metadata -> attachments.read
- GET /api/attachments/{id}/content  get_attachment_bytes    -> attachments.read
- GET /api/attachments/{id}/text     get_attachment_text     -> attachments.read

The permission carries object_axis='none' (object-scoped access is enforced
in the handler via the parent message's tenant/desk rules, not by the role
chokepoint). Granted to super-admin, admin and user.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from permission_db import PLAIN, call, permission_db  # noqa: F401


EXPECTED = {
    ("GET", "/api/attachments/{attachment_id}"): "attachments.read",
    ("GET", "/api/attachments/{attachment_id}/content"): "attachments.read",
    ("GET", "/api/attachments/{attachment_id}/text"): "attachments.read",
}


def test_attachments_router_declares_catalogue_permissions():
    """Structural: every handler carries the catalogue permission name."""
    from routes import attachments
    from permissions import route_permission

    seen: dict[tuple[str, str], str] = {}
    for route in attachments.router.routes:
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
    """Mini app wrapping the real attachments router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import attachments

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(attachments, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(attachments.router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role="installer",
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_metadata_handler_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `attachments.read` is refused by the chokepoint
    before the handler ever looks the attachment up; the 403 detail names
    BOTH the role and the permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="installer", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'installer' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).get(
        "/api/attachments/00000000-0000-0000-0000-000000000000"
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'installer'" in detail
    assert "'attachments.read'" in detail


def test_metadata_handler_reaches_handler_when_role_axis_allows(monkeypatch):
    """A user-tier call on `attachments.read` is admitted; absent the row
    the handler responds 404, proving the chokepoint let it through."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get(
        "/api/attachments/00000000-0000-0000-0000-000000000000"
    )
    # The stubbed cursor returns no row, so the handler itself answers 404.
    # That 404 is proof the chokepoint admitted the request.
    assert r.status_code == 404, r.text
    assert "attachment not found" in r.json()["detail"]


# ---------------------------------------------------------------------------
# the real chain: the seeded catalogue row, real identity, real `requires()`
# ---------------------------------------------------------------------------

def test_attachment_read_is_decided_on_the_role_and_scoped_by_the_handler(permission_db):
    """An attachment hangs on a message, and a message need not belong to a
    desk, so the request names no desk to check. The seeded row carries no
    object axis: the role admits and the handler's own message-level scope
    answers. Seeded with an axis and declared without an object, every caller
    but the owner was refused before the handler."""
    import uuid

    from permissions import validate_routes
    from routes import attachments

    app = FastAPI()
    app.include_router(attachments.router)
    validate_routes(app, strict_undeclared=True)
    r = call(app, "GET", f"/api/attachments/{uuid.uuid4()}", PLAIN)
    assert r.status_code == 404, r.text
    assert "attachments.read" not in r.text
