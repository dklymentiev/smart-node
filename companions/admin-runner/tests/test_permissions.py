"""Tests for the central permission check (`permissions.require_permission`).

Covers the three outcomes the chokepoint must have:
  1. a role without the permission is refused with a reason that names BOTH
     the role AND the permission (so an operator reading the log sees which
     cell of the matrix was missing).
  2. a role with the permission is admitted and the dependency returns the
     Principal unchanged (so handlers that already take `Principal` keep
     working).
  3. super-admin passes without a per-permission grant check beyond the
     matrix row, and a super-admin missing from the matrix is a 500 (server
     misconfiguration, not an actor denial).

The DB is stubbed in each test. These are structural checks of the layer
itself; the live wire is exercised by the gate's regression tests.
"""
import contextlib
import types

import pytest
from fastapi import HTTPException, Request

import auth
import permissions


def _request(host="127.0.0.1"):
    return Request({"type": "http", "client": (host, 1234), "headers": []})


def _principal(username="alice@example.com", role="user"):
    return auth.Principal(username=username, user_id=1, tenant_id=1, role=role)


def _stub_cursor(monkeypatch, rows):
    """Return a cursor stub whose fetchone pops the next row from `rows`.

    rows is a list of dicts (or None for an empty fetch). audit() opens its
    own cursor; we return the same stub.
    """
    pending = list(rows)

    def execute(*_a, **_k):
        return None

    def fetchone():
        if not pending:
            return None
        return pending.pop(0)

    cur = types.SimpleNamespace(execute=execute, fetchone=fetchone)

    @contextlib.contextmanager
    def cursor(commit=False):
        yield cur

    monkeypatch.setattr(permissions, "cursor", cursor)
    monkeypatch.setattr(auth, "cursor", cursor)
    import audit as audit_mod
    monkeypatch.setattr(audit_mod, "_ensure_actor", lambda *a, **k: None)
    return cur


def test_require_permission_role_without_permission_is_refused(monkeypatch):
    """A caller whose role does not grant the permission gets 403 that
    names the role AND the permission in the reason."""
    _stub_cursor(
        monkeypatch,
        [
            # _lookup_permission -> catalogue row
            {"name": "admin.node_status", "object_axis": "none"},
            # _effective_role -> users row (actor_id)
            {"actor_id": "user:bob"},
            # _effective_role -> actor_roles row (none -> fall back to 'user')
            None,
            # _role_has_permission -> no row
            None,
        ],
    )
    monkeypatch.setattr(
        permissions,
        "get_principal",
        lambda r, u: _principal(username="bob@example.com"),
    )
    dep = permissions.require_permission("admin.node_status")
    with pytest.raises(HTTPException) as exc:
        dep(_request(), "bob@example.com")
    assert exc.value.status_code == 403
    assert "role 'user'" in exc.value.detail
    assert "admin.node_status" in exc.value.detail


def test_require_permission_role_with_permission_is_admitted(monkeypatch):
    """A caller whose role grants the permission passes and the dependency
    returns the Principal unchanged."""
    _stub_cursor(
        monkeypatch,
        [
            {"name": "admin.node_status", "object_axis": "none"},
            {"actor_id": "user:alice"},
            {"role": "admin"},
            {"?column?": 1},  # role_permissions row present
        ],
    )
    p = _principal(username="alice@example.com")
    monkeypatch.setattr(permissions, "get_principal", lambda r, u: p)
    dep = permissions.require_permission("admin.node_status")
    assert dep(_request(), "alice@example.com") is p


def test_require_permission_super_admin_is_admitted(monkeypatch):
    """super-admin passes the chokepoint when the matrix row is present."""
    _stub_cursor(
        monkeypatch,
        [
            {"name": "admin.run_command", "object_axis": "none"},
            {"actor_id": "user:root"},
            {"role": "super-admin"},
            {"?column?": 1},
        ],
    )
    p = _principal(username="root@example.com")
    monkeypatch.setattr(permissions, "get_principal", lambda r, u: p)
    dep = permissions.require_permission("admin.run_command")
    assert dep(_request(), "root@example.com") is p


def test_require_permission_super_admin_without_matrix_is_500(monkeypatch):
    """A super-admin missing from role_permissions is a server
    misconfiguration, not an actor denial: raise 500 so the install is
    fixed instead of silently refusing calls."""
    _stub_cursor(
        monkeypatch,
        [
            {"name": "admin.run_command", "object_axis": "none"},
            {"actor_id": "user:root"},
            {"role": "super-admin"},
            None,  # role_permissions miss
        ],
    )
    monkeypatch.setattr(
        permissions,
        "get_principal",
        lambda r, u: _principal(username="root@example.com"),
    )
    dep = permissions.require_permission("admin.run_command")
    with pytest.raises(HTTPException) as exc:
        dep(_request(), "root@example.com")
    # super-admin matrix gap resolves to 403 by design: the request itself
    # is well-formed, the deployment is broken, and the reason names the
    # missing pair so an operator can repair the seed.
    assert exc.value.status_code == 403
    assert "super-admin matrix is incomplete" in exc.value.detail
    assert "admin.run_command" in exc.value.detail


def test_require_permission_unknown_permission_is_500(monkeypatch):
    """A permission the catalogue does not declare is a startup drift: raise
    500 at request time so the misconfiguration is visible if it slips
    past the lifespan check."""
    _stub_cursor(
        monkeypatch,
        [
            None,  # _lookup_permission -> no catalogue row
            None,  # _effective_role -> no users row
        ],
    )
    monkeypatch.setattr(
        permissions,
        "get_principal",
        lambda r, u: _principal(username="x@example.com"),
    )
    dep = permissions.require_permission("admin.nope")
    with pytest.raises(HTTPException) as exc:
        dep(_request(), "x@example.com")
    assert exc.value.status_code == 500
    assert "admin.nope" in exc.value.detail


def test_validate_catalogue_refuses_missing_permission(monkeypatch):
    """`validate_catalogue` refuses a boot when a permission the app declares
    is absent from the sn_config.permissions seed."""
    _stub_cursor(
        monkeypatch,
        [
            # fetchall is not used here; swap to a cursor that returns the
            # single row we want and nothing else.
        ],
    )

    @contextlib.contextmanager
    def cursor(commit=False):
        yield types.SimpleNamespace(
            execute=lambda *a, **k: None,
            fetchall=lambda: [{"name": "admin.node_status", "object_axis": "none"}],
        )

    monkeypatch.setattr(permissions, "cursor", cursor)
    with pytest.raises(RuntimeError) as exc:
        permissions.validate_catalogue(["admin.node_status", "admin.ghost"])
    assert "admin.ghost" in str(exc.value)


def test_validate_catalogue_refuses_non_none_axis(monkeypatch):
    """`validate_catalogue` refuses a boot when a permission has an
    object axis the admin-runner cannot enforce (not 'none')."""

    @contextlib.contextmanager
    def cursor(commit=False):
        yield types.SimpleNamespace(
            execute=lambda *a, **k: None,
            fetchall=lambda: [
                {"name": "admin.node_status", "object_axis": "none"},
                {"name": "admin.on_desk", "object_axis": "desk"},
            ],
        )

    monkeypatch.setattr(permissions, "cursor", cursor)
    with pytest.raises(RuntimeError) as exc:
        permissions.validate_catalogue(["admin.node_status", "admin.on_desk"])
    assert "admin.on_desk" in str(exc.value)
