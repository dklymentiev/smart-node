"""Coverage for desk chat history.

The Studio desk chat loads its thread from `GET /api/desks/<id>/history`, which
reads the gateway sessions (`sn_core.ai_session_messages`) tagged with the desk.
brin now keys those sessions PER agent -- `metadata.conversation` is
`'<agent>:desk:<id>'`, not the bare `'desk:<id>'` -- so a page reload returned an
EMPTY thread over a session that held every turn. This pins both the
agent-prefixed key and the bare key.

Run inside the backend container against the migrated DB.
"""

import uuid

import pytest

from auth import Principal, get_principal


# The member is a real actor with NO role row, which is how the role `user` is
# spelled. That role may read desks; which history comes back is the handler's
# tenant scope, since no code path gives an account a seat on a desk yet.
MEMBER_ACTOR = "user:desk-history-member"


@pytest.fixture
def member_actor():
    from db import init_pool, cursor
    init_pool()
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) "
            "VALUES (%s, 'm', 'user', 'human', 'human', 'internal') ON CONFLICT (id) DO NOTHING",
            (MEMBER_ACTOR,))
    # Left in place: every permission decision is journalled against the actor,
    # and the journal keeps its actors.
    return MEMBER_ACTOR


@pytest.fixture
def as_member(member_actor):
    """Stand in a plain member principal (tenant-scoped) for the endpoint, which
    takes get_principal; a TestClient cannot present a real identity."""
    from main import app

    def _set(tid=1):
        app.dependency_overrides[get_principal] = lambda: Principal(
            username="m", user_id=-1, tenant_id=tid, role="member",
            actor_id=member_actor, actor_kind="human")

    yield _set
    app.dependency_overrides.pop(get_principal, None)


def _make_desk(cur, seated=True):
    """A desk row owned by the node's first account; the member gets a seat."""
    desk_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO sn_core.desks (id, tenant_id, owner_user_id, title) "
        "SELECT %s::uuid, 1, min(id), 'history test' FROM sn_config.users",
        (desk_id,))
    if seated:
        cur.execute(
            "INSERT INTO sn_core.desk_participants (desk_id, actor_id) VALUES (%s::uuid, %s)",
            (desk_id, MEMBER_ACTOR))
    return desk_id


def _drop_desk(cur, desk_id):
    # The participant rows go with the desk.
    cur.execute("DELETE FROM sn_core.desks WHERE id = %s::uuid", (desk_id,))


def _make_session(cur, conv):
    cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
    cur.execute(
        "INSERT INTO sn_core.ai_sessions (tenant_id, type, model, provider, status, metadata) "
        "VALUES (1, 'chat', 'sonnet', 'cli', 'active', %s::jsonb) RETURNING id",
        ('{"conversation": "%s"}' % conv,),
    )
    sid = cur.fetchone()["id"]
    for seq, (role, text) in enumerate((("user", "hi there"), ("assistant", "hello back"))):
        cur.execute(
            "INSERT INTO sn_core.ai_session_messages "
            "  (session_id, seq, role, content_type, content) "
            "VALUES (%s, %s, %s, 'text/plain', %s)",
            (sid, seq, role, text),
        )
    return sid


@pytest.fixture
def agent_prefixed_desk(member_actor):
    """A gateway session keyed '<agent>:desk:<id>' with a user + assistant turn --
    the exact shape a reload returned empty on previously."""
    from db import init_pool, cursor
    init_pool()
    with cursor(commit=True) as cur:
        desk_id = _make_desk(cur)
        sid = _make_session(cur, f"foreman:desk:{desk_id}")
    yield {"desk_id": desk_id, "sid": sid}
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.ai_session_messages WHERE session_id = %s", (sid,))
        cur.execute("DELETE FROM sn_core.ai_sessions WHERE id = %s", (sid,))
        _drop_desk(cur, desk_id)


@pytest.fixture
def bare_desk(member_actor):
    """A gateway session keyed the old bare 'desk:<id>' -- must keep working."""
    from db import init_pool, cursor
    init_pool()
    with cursor(commit=True) as cur:
        desk_id = _make_desk(cur)
        sid = _make_session(cur, f"desk:{desk_id}")
    yield {"desk_id": desk_id, "sid": sid}
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.ai_session_messages WHERE session_id = %s", (sid,))
        cur.execute("DELETE FROM sn_core.ai_sessions WHERE id = %s", (sid,))
        _drop_desk(cur, desk_id)


def test_history_finds_an_agent_prefixed_session(client, as_member, agent_prefixed_desk):
    """the gateway keys the session '<agent>:desk:<id>'; history must still
    find it, or a reload shows an empty chat over a full conversation."""
    as_member(1)
    r = client.get(f"/api/desks/{agent_prefixed_desk['desk_id']}/history")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hi there" and msgs[1]["content"] == "hello back"
    assert all(isinstance(m["message_id"], int) for m in msgs)


def test_history_still_finds_a_bare_key_session(client, as_member, bare_desk):
    """The old bare 'desk:<id>' key must not regress."""
    as_member(1)
    r = client.get(f"/api/desks/{bare_desk['desk_id']}/history")
    assert r.status_code == 200
    assert [m["role"] for m in r.json()["messages"]] == ["user", "assistant"]


@pytest.fixture
def quiet_desk(member_actor):
    """A desk the member sits on, with no session behind it."""
    from db import init_pool, cursor
    init_pool()
    with cursor(commit=True) as cur:
        desk_id = _make_desk(cur)
    yield desk_id
    with cursor(commit=True) as cur:
        _drop_desk(cur, desk_id)


@pytest.fixture
def unseated_desk(member_actor):
    """A desk the member has no seat on."""
    from db import init_pool, cursor
    init_pool()
    with cursor(commit=True) as cur:
        desk_id = _make_desk(cur, seated=False)
    yield desk_id
    with cursor(commit=True) as cur:
        _drop_desk(cur, desk_id)


def test_history_of_an_unknown_desk_is_empty(client, as_member, quiet_desk):
    """A desk that never chatted is an empty list, not an error."""
    as_member(1)
    r = client.get(f"/api/desks/{quiet_desk}/history")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_history_of_a_desk_without_a_seat_is_answered_by_the_handler(client, as_member, unseated_desk):
    """A seat is not asked for: the role admits, and the handler's tenant scope
    answers, as it did before seats existed."""
    as_member(1)
    r = client.get(f"/api/desks/{unseated_desk}/history")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_history_is_tenant_scoped(client, as_member, agent_prefixed_desk):
    """Another tenant cannot read this desk's turns."""
    as_member(4242)
    r = client.get(f"/api/desks/{agent_prefixed_desk['desk_id']}/history")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_the_desk_list_counts_an_agent_prefixed_session(agent_prefixed_desk):
    """The list reads the same sessions as the history. It matched only the bare
    key, so every chat listed 0 messages and no last line, and New chat opened a
    chat with messages as if it were blank (tester, 2026-10-30)."""
    from db import cursor
    from routes.desks import list_desks
    with cursor() as cur:
        cur.execute("SELECT min(id) AS id FROM sn_config.users")
        owner = cur.fetchone()["id"]
    out = list_desks(Principal(username="o", user_id=owner, tenant_id=1, role="owner",
                               actor_id=MEMBER_ACTOR, actor_kind="human"))
    row = next(d for d in out["desks"] if d["id"] == agent_prefixed_desk["desk_id"])
    assert row["message_count"] == 2
    assert row["last_message"] == "hello back"
