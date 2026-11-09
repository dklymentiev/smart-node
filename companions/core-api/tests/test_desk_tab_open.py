"""A desk's tab can be closed and reopened without the desk going away.

The Studio tab's cross used to DELETE the desk. The owner: a chat is never
deleted from the Studio; only its tab is hidden, and the chat stays in the
agent's history. So closing is a view state on the desk (metadata.tab_open),
written through PATCH, and the desk keeps being listed.
"""

import pytest

from auth import Principal, get_principal
from db import cursor


# Changing a desk is an admin-tier permission that also needs a seat on that
# desk. So the desk owner here is a real account with a real actor row, a real
# admin role row, and a participant row on the desk it owns.
OWNER_EMAIL = "desk-tab-owner"
OWNER_ACTOR = "user:desk-tab-owner"


@pytest.fixture
def owner():
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) "
            "VALUES (%s, %s, 'user', 'human', 'human', 'internal') ON CONFLICT (id) DO NOTHING",
            (OWNER_ACTOR, OWNER_EMAIL))
        cur.execute(
            "INSERT INTO sn_config.users (tenant_id, email, role, active, actor_id) "
            "VALUES (1, %s, 'member', true, %s) "
            "ON CONFLICT (tenant_id, email) DO UPDATE SET actor_id = EXCLUDED.actor_id "
            "RETURNING id",
            (OWNER_EMAIL, OWNER_ACTOR))
        user_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO sn_core.actor_roles (actor_id, role, source) "
            "VALUES (%s, 'admin', 'owner_grant') ON CONFLICT (actor_id) DO NOTHING",
            (OWNER_ACTOR,))
    # Left in place: every permission decision is journalled against the actor,
    # and the journal keeps its actors.
    return user_id


@pytest.fixture
def as_member(owner):
    from main import app

    # uid=-2 is the same caller acting from another account: the permission
    # layer admits it (same actor, same seat) and the handler's owner scope
    # is what answers.
    def _set(tid=1, uid=None):
        app.dependency_overrides[get_principal] = lambda: Principal(
            username="m", user_id=owner if uid is None else uid, tenant_id=tid,
            role="member", actor_id=OWNER_ACTOR, actor_kind="human")

    yield _set
    app.dependency_overrides.pop(get_principal, None)


@pytest.fixture
def desk(uid, owner):
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_core.desks (tenant_id, owner_user_id, title, metadata) "
            "VALUES (1, %s, %s, jsonb_build_object('agent', 'max')) RETURNING id",
            (owner, "tab test " + uid),
        )
        desk_id = str(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO sn_core.desk_participants (desk_id, actor_id) VALUES (%s::uuid, %s)",
            (desk_id, OWNER_ACTOR))
    yield desk_id
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.desks WHERE id = %s", (desk_id,))


def _listed(client, desk_id):
    r = client.get("/api/desks")
    assert r.status_code == 200
    return next((d for d in r.json()["desks"] if d["id"] == desk_id), None)


def test_closing_a_tab_keeps_the_desk_listed(client, as_member, desk):
    as_member()
    r = client.patch(f"/api/desks/{desk}", json={"tab_open": False})
    assert r.status_code == 200
    assert r.json()["metadata"]["tab_open"] is False
    listed = _listed(client, desk)
    assert listed is not None, "a closed tab must not remove the chat"
    assert listed["metadata"]["tab_open"] is False
    assert listed["metadata"]["agent"] == "max", "closing must not drop the rest of metadata"


def test_reopening_a_tab(client, as_member, desk):
    as_member()
    client.patch(f"/api/desks/{desk}", json={"tab_open": False})
    r = client.patch(f"/api/desks/{desk}", json={"tab_open": True})
    assert r.status_code == 200
    assert _listed(client, desk)["metadata"]["tab_open"] is True


def test_rename_still_works_and_keeps_tab_state(client, as_member, desk):
    as_member()
    client.patch(f"/api/desks/{desk}", json={"tab_open": False})
    r = client.patch(f"/api/desks/{desk}", json={"title": "Renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"
    assert r.json()["metadata"]["tab_open"] is False


def test_nothing_to_update_is_refused(client, as_member, desk):
    as_member()
    assert client.patch(f"/api/desks/{desk}", json={}).status_code == 400
    assert client.patch(f"/api/desks/{desk}", json={"title": "   "}).status_code == 400


def test_someone_elses_desk_is_not_found(client, as_member, desk):
    as_member(uid=-2)
    assert client.patch(f"/api/desks/{desk}", json={"tab_open": False}).status_code == 404
