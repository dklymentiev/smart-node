"""Role-axis chokepoint on routes/contacts.py.

Catalogue section 5.3 maps the nine contacts-related handlers (two routers)
to:

- GET    /api/contacts                             list_contacts    -> contacts.read
- POST   /api/contacts                             create_contact   -> contacts.create
- PATCH  /api/contacts/{cid}                       update_contact   -> contacts.update
- DELETE /api/contacts/{cid}                       archive_contact  -> contacts.archive
- POST   /api/contacts/{cid}/channels              add_channel      -> contacts.channels.write
- DELETE /api/contacts/{cid}/channels/{ch_id}      remove_channel   -> contacts.channels.write
- GET    /api/agents/{slug}/contacts               list_grants      -> agents.contacts.read
- PUT    /api/agents/{slug}/contacts/{cid}         grant_contact    -> agents.contacts.write
- DELETE /api/agents/{slug}/contacts/{cid}         revoke_contact   -> agents.contacts.write

All nine carry object_axis='none': the role decides.

The last two still pass the (agent, contact) pair as their object, for the day
a reach grant table exists. Until then the permission
is seeded with axis 'none', so the owner and the admin tier change an agent's
reach and the user tier is refused by role. With axis 'reach' and no grant
table every actor but the owner was refused, and the configurator could not
give a hired agent its contacts.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from permission_db import ADMIN, OWNER, PLAIN, call, permission_db  # noqa: F401


DECLARED = {
    ("GET",    "/api/contacts"):                             "contacts.read",
    ("POST",   "/api/contacts"):                             "contacts.create",
    ("PATCH",  "/api/contacts/{cid}"):                       "contacts.update",
    ("DELETE", "/api/contacts/{cid}"):                       "contacts.archive",
    ("POST",   "/api/contacts/{cid}/channels"):              "contacts.channels.write",
    ("DELETE", "/api/contacts/{cid}/channels/{ch_id}"):      "contacts.channels.write",
    ("GET",    "/api/agents/{slug}/contacts"):               "agents.contacts.read",
}

DECLARED_WITH_AN_OBJECT = {
    ("PUT",    "/api/agents/{slug}/contacts/{cid}"):         "agents.contacts.write",
    ("DELETE", "/api/agents/{slug}/contacts/{cid}"):         "agents.contacts.write",
}


def test_contacts_routers_declare_catalogue_permissions():
    """Structural: every handler carries the catalogue permission name, and
    the two agent-contact handlers pass an object."""
    from routes import contacts
    from permissions import route_permission

    seen_declared: dict[tuple[str, str], str] = {}
    seen_with_object: dict[tuple[str, str], str] = {}
    seen_undeclared: set[tuple[str, str]] = set()
    for r in list(contacts.router.routes) + list(contacts.agent_contacts_router.routes):
        for method in r.methods:
            if method == "HEAD":
                continue
            declared = route_permission(r)
            if declared is None:
                seen_undeclared.add((method, r.path))
                continue
            name, has_object = declared
            if has_object:
                seen_with_object[(method, r.path)] = name
            else:
                seen_declared[(method, r.path)] = name
    assert seen_declared == DECLARED, (
        "declared handler-to-permission mapping diverged from the catalogue; "
        f"got {sorted(seen_declared.items())}"
    )
    assert seen_with_object == DECLARED_WITH_AN_OBJECT, sorted(seen_with_object.items())
    assert seen_undeclared == set(), sorted(seen_undeclared)


def _reach_app() -> FastAPI:
    from routes import contacts

    app = FastAPI()
    app.include_router(contacts.agent_contacts_router)
    return app


def test_reach_routes_pass_the_startup_scan(permission_db):
    from permissions import validate_routes

    validate_routes(_reach_app(), strict_undeclared=True)


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_changing_an_agents_reach_is_decided_by_role(permission_db, method):
    # The admin tier passes the permission check and reaches the handler, which
    # answers for the agent that is not there. In 1.12.49 this was a 403 on the
    # object axis 'reach': no grant table existed, so only the owner got through
    # and a configurator could not give a hired agent its contacts.
    r = call(_reach_app(), method, "/api/agents/some-agent/contacts/7", ADMIN,
             json={"channels": []})
    assert r.status_code != 403, r.text
    assert "agents.contacts.write" not in r.text, r.text

    r = call(_reach_app(), method, "/api/agents/some-agent/contacts/7", PLAIN,
             json={"channels": []})
    assert r.status_code == 403 and "'user'" in r.json()["detail"], r.text


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_owner_reaches_the_reach_handlers(permission_db, method):
    r = call(_reach_app(), method, "/api/agents/no-such-agent/contacts/7", OWNER,
             json={"channels": []})
    # Past the gate: the handler's own answer for an agent that is not there.
    assert r.status_code == 404, r.text


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


def _build_app(monkeypatch, decision_for, role="admin"):
    """Mini app wrapping the real contacts router with DB and chokepoint stubbed."""
    import permissions
    import auth
    from routes import contacts

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(contacts, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(contacts.router)
    app.include_router(contacts.agent_contacts_router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=0, role=role,
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_create_contact_is_refused_when_role_axis_denies(monkeypatch):
    """A caller whose role lacks `contacts.create` is refused by the chokepoint
    before the handler ever runs; 403 names BOTH the role and the permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny, role="user")
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/contacts",
        json={"display_name": "Someone", "kind": "person"},
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'contacts.create'" in detail


def test_list_contacts_is_reached_when_role_axis_allows(monkeypatch):
    """A role grant on `contacts.read` is admitted; handler responds 200."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="admin", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/contacts")
    assert r.status_code == 200, r.text
    assert r.json() == {"contacts": []}
