"""The permission gate on routes/desks.py.

Every handler declares the catalogue's permission. The four that change one
desk pass that desk as the object: the role decides first and the caller's
participant row second, and the owner is admitted without a row. Reading is
decided on the role alone, because nothing gives an account a seat on a desk
yet; which desks are listed and whose files are served is the handler's own
scope behind the gate.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI

from permission_db import ADMIN, OWNER, PLAIN, call, declared, permission_db  # noqa: F401

EXPECTED = {
    ("POST", "/api/desks"): ("desks.create", False),
    ("DELETE", "/api/desks/{desk_id}"): ("desks.delete", True),
    ("PUT", "/api/desks/{desk_id}/ensure"): ("desks.create", False),
    ("GET", "/api/desks/{desk_id}/history"): ("desks.read", False),
    ("GET", "/api/desks/{desk_id}/sessions"): ("desks.read", False),
    ("PATCH", "/api/desks/{desk_id}"): ("desks.update", True),
    ("POST", "/api/desks/{desk_id}/attachments"): ("desks.attachments.write", True),
    ("GET", "/api/desks/{desk_id}/attachments"): ("desks.read", False),
    ("GET", "/api/desks/{desk_id}/attachments/{attachment_id}/content"): ("desks.read", False),
    ("DELETE", "/api/desks/{desk_id}/attachments/{attachment_id}"): ("desks.attachments.delete", True),
    ("GET", "/api/desks"): ("desks.read", False),
    ("GET", "/api/desks/unread"): ("desks.read", False),
    ("POST", "/api/desks/{desk_id}/mentions"): ("messages.send", False),
    ("GET", "/api/desks/{desk_id}/waiting"): ("desks.read", False),
}


def _app() -> FastAPI:
    from routes import desks

    app = FastAPI()
    app.include_router(desks.router)
    return app


@pytest.fixture
def desk(permission_db):
    """A desk owned by the owner account, with no participant rows."""
    desk_id = str(uuid.uuid4())
    with permission_db(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_core.desks (id, tenant_id, owner_user_id, title) "
            "SELECT %s::uuid, tenant_id, id, 'permission test' FROM sn_config.users WHERE email = %s",
            (desk_id, OWNER))
        assert cur.rowcount == 1
    yield desk_id
    with permission_db(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.desks WHERE id = %s::uuid", (desk_id,))


def _seat(permission_db, desk_id: str, who: str) -> None:
    with permission_db(commit=True) as cur:
        cur.execute("INSERT INTO sn_core.desk_participants (desk_id, actor_id) VALUES (%s::uuid, %s)",
                    (desk_id, f"user:{who}"))


def test_every_desk_route_declares_the_catalogue_permission(permission_db):
    from permissions import validate_routes
    from routes import desks

    assert declared(desks.router) == EXPECTED
    # Names exist in the seeded catalogue, and each object-axis permission is
    # declared with an object on every route that uses it.
    validate_routes(_app(), strict_undeclared=True)


def test_account_without_a_role_is_refused_by_name(permission_db):
    r = call(_app(), "POST", "/api/desks", PLAIN, json={"title": "mine"})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail and "'desks.create'" in detail


def test_admin_reaches_the_handler(permission_db):
    r = call(_app(), "POST", "/api/desks", ADMIN, json={"title": "an admin desk"})
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "an admin desk"


def test_listing_is_admitted_by_role_and_shows_only_the_callers_desks(permission_db, desk):
    """No desk is named and none is asked for. The handler lists the caller's
    own desks: the owner sees the fixture's desk, the others do not."""
    for who in (PLAIN, ADMIN):
        r = call(_app(), "GET", "/api/desks", who)
        assert r.status_code == 200, r.text
        assert desk not in [d["id"] for d in r.json()["desks"]]
    r = call(_app(), "GET", "/api/desks", OWNER)
    assert r.status_code == 200, r.text
    assert desk in [d["id"] for d in r.json()["desks"]]


@pytest.mark.parametrize("who", [ADMIN, PLAIN])
def test_reading_a_desk_needs_no_seat(permission_db, desk, who):
    """The role holds desks.read and no participant row is consulted."""
    r = call(_app(), "GET", f"/api/desks/{desk}/history", who)
    assert r.status_code == 200, r.text
    assert r.json() == {"desk_id": desk, "messages": []}
    with permission_db() as cur:
        cur.execute("SELECT payload FROM sn_core.events WHERE entity_type = 'permission' "
                    "AND entity_id = 'desks.read' ORDER BY id DESC LIMIT 1")
        payload = cur.fetchone()["payload"]
    assert payload["object_axis"] == "none"
    assert payload["object_axis_deferred"] == "handler"


def test_the_handler_scope_behind_a_read_still_applies(permission_db, desk):
    """Past the gate the files of a desk are served to its owner, its agent and
    the whole-node seats; anyone else gets the answer for a desk that is not
    there."""
    r = call(_app(), "GET", f"/api/desks/{desk}/attachments", PLAIN)
    assert r.status_code == 404, r.text
    assert "desk not found" in r.json()["detail"]
    r = call(_app(), "GET", f"/api/desks/{desk}/attachments", OWNER)
    assert r.status_code == 200, r.text


def test_changing_a_desk_still_needs_a_seat(permission_db, desk):
    r = call(_app(), "PATCH", f"/api/desks/{desk}", ADMIN, json={"title": "renamed"})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'desks.update'" in detail and "object axis 'desk'" in detail and desk in detail


def test_owner_changes_a_desk_without_a_seat(permission_db, desk):
    r = call(_app(), "PATCH", f"/api/desks/{desk}", OWNER, json={"title": "renamed"})
    assert r.status_code == 200, r.text


def test_the_owner_scope_behind_the_gate_still_applies(permission_db, desk):
    """A seated admin holds desks.update on this desk, and renaming is still
    the desk owner's alone: the handler answers as for a desk that is not there."""
    _seat(permission_db, desk, ADMIN)
    r = call(_app(), "PATCH", f"/api/desks/{desk}", ADMIN, json={"title": "renamed"})
    assert r.status_code == 404, r.text
    assert "desk not found" in r.json()["detail"]
