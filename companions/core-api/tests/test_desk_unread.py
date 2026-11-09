"""What is new in a desk since its owner last read it (Studio's unread
counts and their Unread filter).

The desk keeps metadata.read_upto, the moment its owner has read it up to;
GET /api/desks/unread counts what others wrote after it. Pinned here against a
throwaway database built from the tree's bootstrap files:

1. A new desk starts read, and an answer after that is new; the owner's own
   turns never are.
2. Every source the thread draws counts: a gateway answer under the agent's
   conversation key, a colleague's relay, and what an agent wrote to the desk
   on the bus. What the thread never draws does not: a NO_REPLY, bare or
   relayed, the action-claim guard's scaffolding, and a relay still waiting
   for delivery.
3. Marking read takes a moment, not "now": an answer that lands after the
   newest message the Studio showed stays new. The mark never moves back and
   never past the server's clock.
4. A desk from before read tracking counts nothing until it is first read.
5. Only the owner's own desks are counted or marked.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals


@pytest.fixture(scope="module")
def node():
    import auth
    from routes import desks

    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        tenant = people["owner"].tenant_id
        app = FastAPI()
        app.include_router(desks.router)
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name: str) -> TestClient:
            who["p"] = people[name]
            return client

        yield sql, as_, tenant


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _new_desk(as_) -> str:
    r = as_("owner").post("/api/desks", json={"title": "unread test", "agent_slug": "dev"})
    assert r.status_code == 201, r.text
    assert r.json()["metadata"].get("read_upto"), "a new desk starts read"
    return r.json()["id"]


def _turn(sql, tenant, desk, role, text, conv=None, at="clock_timestamp()"):
    """One gateway turn in the desk's session (agent-prefixed key by default)."""
    conv = conv or f"dev:desk:{desk}"
    sid = sql("SELECT id FROM sn_core.ai_sessions WHERE metadata->>'conversation' = "
              f"{_q(conv)} LIMIT 1")
    if not sid:
        sid = sql("INSERT INTO sn_core.ai_sessions (tenant_id, type, model, provider, status, metadata) "
                  f"VALUES ({tenant}, 'chat', 'sonnet', 'cli', 'active', "
                  f"jsonb_build_object('conversation', {_q(conv)})) RETURNING id")
    seq = sql(f"SELECT COALESCE(max(seq), -1) + 1 FROM sn_core.ai_session_messages WHERE session_id = '{sid}'")
    sql("INSERT INTO sn_core.ai_session_messages (session_id, seq, role, content_type, content, created_at) "
        f"VALUES ('{sid}', {seq}, '{role}', 'text/plain', {_q(text)}, {at})")


def _relay(sql, tenant, desk, text, processed=True):
    mid = sql("INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, "
              "direction, sender, recipient, content, content_type, tenant_id) "
              f"VALUES ('internal', 'desk:{desk}', '<{uuid.uuid4()}@test>', '<r@test>', 'inbound', "
              f"'system', 'desk:{desk}', {_q(text)}, 'text/plain', {tenant}) RETURNING id")
    if processed:
        sql(f"UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = {mid}")


def _bus(sql, tenant, desk, text):
    sql("INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, "
        "direction, sender, recipient, content, content_type, tenant_id) "
        f"VALUES ('web-chat', 'desk:{desk}', '<{uuid.uuid4()}@test>', 'desk:{desk}', 'outbound', "
        f"'dev', 'admin', {_q(text)}, 'text/plain', {tenant})")


def _unread(as_, desk, who="owner"):
    r = as_(who).get("/api/desks/unread")
    assert r.status_code == 200, r.text
    return next((d for d in r.json()["desks"] if d["id"] == desk), None)


def test_a_new_desk_starts_read_and_only_others_count(node):
    sql, as_, tenant = node
    desk = _new_desk(as_)
    assert _unread(as_, desk)["unread"] == 0
    _turn(sql, tenant, desk, "user", "my question")
    assert _unread(as_, desk)["unread"] == 0, "the owner's own turn is not new to the owner"
    _turn(sql, tenant, desk, "assistant", "the answer")
    row = _unread(as_, desk)
    assert row["unread"] == 1
    assert row["last_text"] == "the answer" and row["last_at"]


def test_every_drawn_source_counts_and_nothing_undrawn_does(node):
    sql, as_, tenant = node
    desk = _new_desk(as_)
    _turn(sql, tenant, desk, "assistant", "on the bare key", conv=f"desk:{desk}")
    _relay(sql, tenant, desk, "Answer from ann:\n\nI checked it.")
    _bus(sql, tenant, desk, "written to the desk later")
    assert _unread(as_, desk)["unread"] == 3
    _turn(sql, tenant, desk, "assistant", "NO_REPLY")
    _relay(sql, tenant, desk, "Answer from ann:\n\nNO_REPLY.")
    _turn(sql, tenant, desk, "assistant", "[SYSTEM CHECK] did you really send it?")
    _relay(sql, tenant, desk, "not delivered yet", processed=False)
    row = _unread(as_, desk)
    assert row["unread"] == 3
    assert row["last_text"] == "written to the desk later"


def test_marking_read_takes_a_moment_and_never_goes_back(node):
    sql, as_, tenant = node
    desk = _new_desk(as_)
    _turn(sql, tenant, desk, "assistant", "first")
    seen = _unread(as_, desk)
    assert seen["unread"] == 1
    # The answer that lands while the mark is on its way.
    _turn(sql, tenant, desk, "assistant", "second")
    r = as_("owner").patch(f"/api/desks/{desk}", json={"read_upto": seen["last_at"]})
    assert r.status_code == 200, r.text
    row = _unread(as_, desk)
    assert row["unread"] == 1 and row["last_text"] == "second"
    # Back in time: ignored.
    r = as_("owner").patch(f"/api/desks/{desk}", json={"read_upto": "2000-01-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    assert _unread(as_, desk)["unread"] == 1
    # Past the server's clock: held at now, so what comes next still counts.
    r = as_("owner").patch(f"/api/desks/{desk}", json={"read_upto": "2999-01-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    held = sql(f"SELECT (metadata->>'read_upto')::timestamptz <= now() FROM sn_core.desks WHERE id = '{desk}'")
    assert held in ("t", True), held
    _turn(sql, tenant, desk, "assistant", "third", at="now() + interval '1 hour'")
    assert _unread(as_, desk)["unread"] >= 1


def test_marking_read_keeps_the_rest_of_the_desk(node):
    sql, as_, tenant = node
    desk = _new_desk(as_)
    as_("owner").patch(f"/api/desks/{desk}", json={"tab_open": False})
    r = as_("owner").patch(f"/api/desks/{desk}", json={"read_upto": "2026-01-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    meta = r.json()["metadata"]
    assert meta["tab_open"] is False and meta["agent"] == "dev" and meta["read_upto"]
    assert r.json()["title"] == "unread test"


def test_a_desk_from_before_read_tracking_counts_nothing_until_read(node):
    sql, as_, tenant = node
    desk = str(uuid.uuid4())
    sql("INSERT INTO sn_core.desks (id, tenant_id, owner_user_id, title, metadata) VALUES "
        f"('{desk}', {tenant}, 1, 'old chat', '{{\"agent\": \"dev\"}}')")
    _turn(sql, tenant, desk, "assistant", "an old answer", at="now() - interval '1 day'")
    row = _unread(as_, desk)
    assert row["unread"] == 0 and row["last_text"] == "an old answer"
    assert row["read_upto"] is None, "the Studio marks a desk it shows with no mark"
    as_("owner").patch(f"/api/desks/{desk}", json={"read_upto": row["last_at"]})
    _turn(sql, tenant, desk, "assistant", "a new answer")
    assert _unread(as_, desk)["unread"] == 1


def test_only_the_owner_counts_and_marks(node):
    sql, as_, tenant = node
    desk = _new_desk(as_)
    _turn(sql, tenant, desk, "assistant", "for the owner")
    assert _unread(as_, desk, who="admin") is None
    r = as_("admin").patch(f"/api/desks/{desk}", json={"read_upto": "2026-01-01T00:00:00Z"})
    assert r.status_code in (403, 404), r.text
    assert _unread(as_, desk)["unread"] == 1


def test_an_empty_mark_is_refused(node):
    sql, as_, tenant = node
    desk = _new_desk(as_)
    r = as_("owner").patch(f"/api/desks/{desk}", json={"read_upto": None})
    assert r.status_code == 400, r.text
