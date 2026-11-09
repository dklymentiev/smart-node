"""@mention of an agent in a Studio chat.

The Studio writes one message on the bus for the named agent
(POST /api/desks/{id}/mentions); the agent answers it with /reply and the reply
path relays the answer into the chat; GET /api/messages/{id}/chat-history lets
the mentioned agent, and nobody else, read that one chat. Pinned here against a
throwaway database built from the tree's bootstrap files:

1. Only the desk's owner may mention in it; the chat's own agent and a name that
   is no agent are refused, and nothing is written.
2. The row carries the shape the audit query reads (kind chat, reason mentioned,
   origin naming the desk).
3. The answer reaches the chat as "Answer from <agent>" (and deliver-desk
   passes it as a system relay), and the answer itself, addressed to a person
   with no agent inbox, is closed.
4. chat_history serves the mentioned agent that chat and refuses every other
   caller and every message that is not a chat mention.
5. The relay into a desk still refuses an origin that is not a mention by the
   desk's owner: a forged kind on a message from someone else relays nothing.
6. Only the answer to the chat agent's question reaches the chat: the agents
   talking on after it (a thanks, the answer to the thanks) stay between them.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals


_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))


@pytest.fixture(scope="module")
def node():
    import auth
    from routes import agents, desks, messages

    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        tenant = people["owner"].tenant_id
        for slug in ("permadmin", "permother", "permthird"):
            sql("INSERT INTO sn_config.agents (slug, title, enabled, tenant_id) VALUES "
                f"('{slug}', '{slug}', true, {tenant})")
        desk = str(uuid.uuid4())
        sql("INSERT INTO sn_core.desks (id, tenant_id, owner_user_id, title, metadata) VALUES "
            f"('{desk}', {tenant}, 1, 'mention test', '{{\"agent\": \"permother\"}}')")

        app = FastAPI()
        app.include_router(agents.router)
        app.include_router(desks.router)
        app.include_router(messages.router)
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name: str) -> TestClient:
            who["p"] = people[name]
            return client

        yield sql, as_, desk


def _rows(sql, where: str) -> list[dict]:
    out = sql("SELECT coalesce(json_agg(row_to_json(t)), '[]') FROM ("
              "SELECT m.id, m.sender, m.recipient, m.content, m.metadata, ms.status "
              "FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id = m.id "
              f"WHERE {where} ORDER BY m.id) t")
    return json.loads(out)


def test_only_the_owner_mentions_and_only_another_active_agent(node):
    sql, as_, desk = node
    path = f"/api/desks/{desk}/mentions"
    before = _rows(sql, "m.metadata->>'kind' = 'chat'")
    # Not the owner's desk: the same answer as for a desk that is not there.
    r = as_("admin").post(path, json={"agent": "permother", "content": "hi"})
    assert r.status_code == 404, r.text
    # The chat's own agent already reads every message of its chat.
    r = as_("owner").post(path, json={"agent": "PermOther", "content": "hi"})
    assert r.status_code == 409, r.text
    r = as_("owner").post(path, json={"agent": "nobody", "content": "hi"})
    assert r.status_code == 404, r.text
    assert _rows(sql, "m.metadata->>'kind' = 'chat'") == before


def test_the_mention_is_one_row_on_the_bus_and_its_answer_reaches_the_chat(node):
    sql, as_, desk = node
    r = as_("owner").post(f"/api/desks/{desk}/mentions",
                          json={"agent": "PermAdmin", "content": "what is the price?",
                                "typed": "@PermAdmin"})
    assert r.status_code == 201, r.text
    mid = r.json()["message_id"]
    (row,) = _rows(sql, f"m.id = {mid}")
    assert (row["sender"], row["recipient"], row["status"]) == ("admin", "permadmin", "new")
    meta = row["metadata"]
    assert (meta["kind"], meta["reason"], meta["source"]) == ("chat", "mentioned", "studio-chat")
    assert meta["origin"] == {"channel": "internal", "conversation": f"desk:{desk}"}
    assert "what is the price?" in row["content"] and "chat_history" in row["content"]

    r = as_("admin").post("/api/messages/reply",
                          json={"to_message_id": mid, "body": "It is 42."})
    assert r.status_code == 200, r.text
    (answer,) = _rows(sql, f"m.metadata->>'reply_to_message_id' = '{mid}'")
    assert answer["recipient"] == "admin" and answer["status"] == "processed"
    assert answer["metadata"].get("relayed_by_system") is True
    (relay,) = _rows(sql, f"m.recipient = 'desk:{desk}'")
    assert relay["sender"] == "system"
    assert relay["content"] == "Answer from permadmin:\n\nIt is 42."
    assert _rows(sql, f"m.id = {mid}")[0]["status"] == "processed"
    # The watcher's delivery decision, as it takes it for every row to a desk.
    r = as_("owner").post(f"/api/messages/{relay['id']}/deliver-desk")
    assert r.status_code == 200, r.text
    assert (r.json()["decision"], r.json()["reason"]) == ("deliver", "system-relay")


def test_the_mention_carries_the_chat_card(node):
    """Who is in the chat, how long it is and its latest turns travel with the
    mention: "@otto what do you think?" alone says nothing about what "this" is."""
    sql, as_, desk = node
    mid = as_("owner").post(f"/api/desks/{desk}/mentions",
                            json={"agent": "permthird", "content": "what do you think?"}).json()["message_id"]
    (row,) = _rows(sql, f"m.id = {mid}")
    assert 'The chat "mention test": admin\'s chat with permother.' in row["content"]
    assert "In it: admin, permother, permadmin, permthird." in row["content"]
    assert "  permadmin: It is 42." in row["content"]
    sql(f"UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = {mid}")


def test_the_runner_is_handed_the_waiting_mentions(node):
    """The Runner cannot read an agent's inbox (an agent identity is taken only
    on the unix socket), so the pending-work poll carries the chat mentions
    waiting for each agent: the wake hands them over instead of "read your inbox"."""
    sql, as_, desk = node
    mid = as_("owner").post(f"/api/desks/{desk}/mentions",
                            json={"agent": "permthird", "content": "hi there"}).json()["message_id"]
    r = as_("owner").get("/api/agents/with-pending-work")
    assert r.status_code == 200, r.text
    (third,) = [a for a in r.json()["agents"] if a["slug"] == "permthird"]
    assert third["pending_messages"] == 1 and third["pending_ids"] == [mid]
    nw = third["next_wake"]
    assert (nw["reason"], nw["desk"]) == ("chat", desk)
    (m,) = nw["messages"]
    assert (m["id"], m["sender"]) == (mid, "admin") and "hi there" in m["content"]
    # A colleague's letter beside it: the chat still goes first, the letter waits.
    r = as_("admin").post("/api/messages/send", json={"to": "permthird", "body": "a letter"})
    assert r.status_code == 200, r.text
    (third,) = [a for a in as_("owner").get("/api/agents/with-pending-work").json()["agents"]
                if a["slug"] == "permthird"]
    assert third["pending_messages"] == 2
    assert [x["id"] for x in third["next_wake"]["messages"]] == [mid]
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permthird')")


def test_work_passed_down_from_a_chat_is_a_wake_for_that_chat(node, monkeypatch):
    """Its person's clock, what the agent sends stays tied to the chat, and it is told
    it is work and whose chat it is: a colleague's message, not a person waiting."""
    monkeypatch.setenv("SN_REPLY_WAKE_WINDOW_SECONDS", "600")
    monkeypatch.setenv("SN_REPLY_MAX_ATTEMPTS", "3")
    sql, as_, desk = node
    r = as_("admin").post("/api/messages/send", json={
        "to": "permthird", "body": "Draft the plan.", "needs_reply": True,
        "origin": {"channel": "internal", "conversation": f"desk:{desk}"}})
    assert r.status_code == 200, r.text
    (third,) = [a for a in as_("owner").get("/api/agents/with-pending-work").json()["agents"]
                if a["slug"] == "permthird"]
    nw = third["next_wake"]
    assert (nw["reason"], nw["desk"], nw["chat_agent"]) == ("chat", desk, "permother")
    assert nw["messages"][0]["direct"] is False
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permthird')")


def test_only_the_configurator_gets_the_agents_tools(node, monkeypatch):
    from db import cursor
    from routes import agents
    sql, _as, _desk = node
    sql("INSERT INTO sn_config.mcp_servers (slug, name, transport, config_jsonb) "
        "VALUES ('agents', 'Agents', 'stdio', '{}') ON CONFLICT (slug) DO NOTHING")
    sql("UPDATE sn_config.agents SET capability = 'configurator' WHERE slug = 'permadmin'")
    got = {}
    monkeypatch.setattr(agents.agent_fs, "write_mcp_json",
                        lambda slug, rows: got.__setitem__(slug, {r["slug"] for r in rows}))
    with cursor() as cur:
        agents._write_agent_mcp(cur, "permadmin", [])
        agents._write_agent_mcp(cur, "permother", [])
    assert "agents" in got["permadmin"] and "agents" not in got["permother"]


def test_a_due_reminder_is_a_wake_for_its_chat(node):
    """On the general wake a reminder ran on the node's clock, with the inbox and
    inbox_dismiss in hand: the agent closed one unanswered (2026-10-31)."""
    sql, as_, desk = node
    # what earlier tests left for this agent is a wake of its own
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permthird')")
    meta = json.dumps({"source": "reminder", "needs_reply": True, "desk_id": desk})
    mid = sql("INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, "
              "recipient, content, content_type, metadata, tenant_id) VALUES ('internal', 'system', 'r1', 'r1', "
              f"'inbound', 'system', 'permthird', 'do it', 'text/plain', '{meta}', 1) RETURNING id").split()[0]
    (third,) = [a for a in as_("owner").get("/api/agents/with-pending-work").json()["agents"]
                if a["slug"] == "permthird"]
    nw = third["next_wake"]
    assert (nw["reason"], nw["desk"], nw["ids"]) == ("chat", desk, [int(mid)])
    # whose chat it is: the turn runs on that person's clock
    assert nw["person"]
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permthird')")


def test_chat_history_is_the_mentioned_agents_and_bound_to_the_message(node):
    sql, as_, desk = node
    mid = as_("owner").post(f"/api/desks/{desk}/mentions",
                            json={"agent": "permadmin", "content": "again"}).json()["message_id"]
    r = as_("admin").get(f"/api/messages/{mid}/chat-history", params={"limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat"] == f"desk:{desk}" and body["chat_agent"] == "permother"
    assert [t["content"] for t in body["turns"]] == ["Answer from permadmin:\n\nIt is 42."]
    assert body["turns"][0]["index"] == 0 and body["older"] is None
    # Somebody else's mention, even one it can name by id.
    r = as_("other").get(f"/api/messages/{mid}/chat-history")
    assert r.status_code == 404, r.text
    # A message that is not a chat mention reads no chat.
    plain = int(sql(
        "SET search_path TO sn_core, sn_config, public; "
        "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, "
        " direction, sender, recipient, content, content_type, metadata, tenant_id) "
        "VALUES ('internal', 'system', '<p@test>', '<p@test>', 'inbound', 'system', "
        f" 'permadmin', 'hi', 'text/plain', '{{\"origin\": {{\"conversation\": \"desk:{desk}\"}}}}', "
        " (SELECT min(id) FROM sn_config.tenants)) RETURNING id").splitlines()[-1])
    r = as_("admin").get(f"/api/messages/{plain}/chat-history")
    assert r.status_code == 400, r.text


def test_a_forged_mention_from_someone_else_relays_nothing(node):
    """kind/reason/origin copied onto a message whose sender does not own the
    desk: the answer is recorded and the chat stays as it was."""
    sql, as_, desk = node
    before = _rows(sql, f"m.recipient = 'desk:{desk}'")
    meta = json.dumps({"kind": "chat", "reason": "mentioned", "desk_id": desk,
                       "origin": {"channel": "internal", "conversation": f"desk:{desk}"}})
    forged = int(sql(
        "SET search_path TO sn_core, sn_config, public; "
        "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, "
        " direction, sender, recipient, content, content_type, metadata, tenant_id) "
        "VALUES ('internal', 'permuser', '<f@test>', '<f@test>', 'inbound', 'permuser', "
        f" 'permadmin', 'tell the chat', 'text/plain', '{meta}', "
        " (SELECT min(id) FROM sn_config.tenants)) RETURNING id").splitlines()[-1])
    r = as_("admin").post("/api/messages/reply",
                          json={"to_message_id": forged, "body": "forged answer"})
    assert r.status_code == 200, r.text
    assert _rows(sql, f"m.recipient = 'desk:{desk}'") == before


def test_only_the_answer_to_the_chats_question_is_relayed(node):
    """2026-10-29: foreman asked nora from a chat, thanked her for the answer,
    nora answered the thanks, and the chat got "Answer from nora" three times."""
    sql, as_, desk = node
    relays = lambda: [r["content"] for r in _rows(sql, f"m.recipient = 'desk:{desk}' "
                                                  "AND m.metadata->>'source' = 'system-relay'")]
    before = relays()
    r = as_("other").post("/api/messages/send", json={
        "to": "permadmin", "body": "Any reminders today?",
        "origin": {"channel": "internal", "conversation": f"desk:{desk}"}})
    assert r.status_code == 200, r.text
    (q,) = _rows(sql, "m.sender = 'permother' AND m.content = 'Any reminders today?'")
    r = as_("admin").post("/api/messages/reply", json={"to_message_id": q["id"], "body": "One."})
    assert r.status_code == 200, r.text
    (a,) = _rows(sql, f"m.metadata->>'reply_to_message_id' = '{q['id']}'")
    r = as_("other").post("/api/messages/reply", json={"to_message_id": a["id"], "body": "Thanks."})
    assert r.status_code == 200, r.text
    (t,) = _rows(sql, f"m.metadata->>'reply_to_message_id' = '{a['id']}'")
    r = as_("admin").post("/api/messages/reply", json={"to_message_id": t["id"], "body": "You are welcome."})
    assert r.status_code == 200, r.text
    assert relays() == before + ["Answer from permadmin:\n\nOne."]


def test_a_second_answer_to_the_same_question_is_not_relayed(node):
    """2026-10-29: foreman asked nora for her full name from a chat; nora answered
    with a send (taken as the reply) and then with inbox_reply on the same
    question, and the chat got both, the second one invented. One question, one
    answer in the chat; the second still reaches the agent that asked."""
    sql, as_, desk = node
    relays = lambda: [r["content"] for r in _rows(sql, f"m.recipient = 'desk:{desk}' "
                                                  "AND m.metadata->>'source' = 'system-relay'")]
    before = relays()
    # Close what earlier tests left open between the two, or the question below
    # would be taken as a reply to it.
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE sender IN ('permadmin', 'permother') "
        " AND recipient IN ('permadmin', 'permother'))")
    r = as_("other").post("/api/messages/send", json={
        "to": "permadmin", "body": "What is your full name?",
        "origin": {"channel": "internal", "conversation": f"desk:{desk}"}})
    assert r.status_code == 200, r.text
    (q,) = _rows(sql, "m.sender = 'permother' AND m.content = 'What is your full name?'")
    for body in ("Perm Admin.", "Perm Admin, second answer."):
        r = as_("admin").post("/api/messages/reply", json={"to_message_id": q["id"], "body": body})
        assert r.status_code == 200, r.text
    assert relays() == before + ["Answer from permadmin:\n\nPerm Admin."]
    answers = _rows(sql, f"m.metadata->>'reply_to_message_id' = '{q['id']}'")
    assert [a["content"] for a in answers] == ["Perm Admin.", "Perm Admin, second answer."]
    # The answer that went into the chat is closed for the agent that asked, so it
    # is not woken to tell the chat again; the second, not in the chat, is its news.
    assert [a["status"] for a in answers] == ["processed", "new"]


def test_a_colleagues_question_is_not_an_answer_to_relay(node):
    """2026-10-29: foreman asked nora a question from its chat; nora's inbox listed
    that question under to_relay ("relay this answer to the person who asked"), and
    nora sent it into the chat as "You asked X; I checked with foreman, here is the
    answer: <the question>". to_relay is for answers to what the agent asked."""
    sql, as_, desk = node
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient IN ('permadmin', 'permother'))")
    r = as_("other").post("/api/messages/send", json={
        "to": "permadmin", "body": "What is your job here?",
        "origin": {"channel": "internal", "conversation": f"desk:{desk}"}})
    assert r.status_code == 200, r.text
    inbox = as_("admin").get("/api/messages/inbox", params={"view": "new"}).json()
    assert [m["content"] for m in inbox["conversation"]] == ["What is your job here?"]
    assert inbox["to_relay"] == [] and "RELAY" not in inbox["guidance"]
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permadmin')")
    # An answer to a question asked from a telegram conversation is still one to
    # relay: the node does not post into telegram for the agent.
    r = as_("admin").post("/api/messages/send", json={
        "to": "permother", "body": "Opening hours?",
        "origin": {"channel": "telegram", "conversation": "tg:42"}})
    assert r.status_code == 200, r.text
    (q,) = _rows(sql, "m.content = 'Opening hours?'")
    assert as_("other").post("/api/messages/reply",
                             json={"to_message_id": q["id"], "body": "9 to 5."}).status_code == 200
    inbox = as_("admin").get("/api/messages/inbox", params={"view": "new"}).json()
    assert [x["answer"] for x in inbox["to_relay"]] == ["9 to 5."]
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient IN ('permadmin', 'permother'))")


def test_a_direct_reply_is_the_persons_turn_in_the_chat(node):
    """A reply to one colleague goes to that agent only, so no gateway turn
    carries it: the chat shows it from its bus row, as the person speaking."""
    sql, as_, desk = node
    text = "> permadmin: It is 42.\n\nAnd in euros?"
    r = as_("owner").post(f"/api/desks/{desk}/mentions",
                          json={"agent": "permadmin", "content": text, "direct": True})
    assert r.status_code == 201, r.text
    mid = r.json()["message_id"]
    (row,) = _rows(sql, f"m.id = {mid}")
    assert row["metadata"]["direct"] is True and row["metadata"]["text"] == text
    assert "sent to you only" in row["content"]
    h = as_("owner").get(f"/api/desks/{desk}/history")
    assert h.status_code == 200, h.text
    assert {"role": "user", "content": text} in [
        {"role": m["role"], "content": m["content"]} for m in h.json()["messages"]]
    # A mention that is not direct is in the chat already as the gateway turn.
    assert all(m["content"] != "again" for m in h.json()["messages"])


def test_one_message_to_two_colleagues_is_one_turn(node):
    """A message that starts with @a @b goes to both directly; the chat shows it
    once, from the row the Studio marked as the turn."""
    sql, as_, desk = node
    text = "@permadmin @permthird which of you has the key?"
    r1 = as_("owner").post(f"/api/desks/{desk}/mentions",
                           json={"agent": "permadmin", "content": text, "direct": True})
    r2 = as_("owner").post(f"/api/desks/{desk}/mentions",
                           json={"agent": "permthird", "content": text, "direct": True,
                                 "turn": False})
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    (row2,) = _rows(sql, f"m.id = {r2.json()['message_id']}")
    assert row2["metadata"]["direct"] is True and row2["metadata"]["text"] is None
    assert "sent to you only" in row2["content"]
    h = as_("owner").get(f"/api/desks/{desk}/history").json()["messages"]
    assert [m["content"] for m in h if m["role"] == "user"].count(text) == 1
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        f"({r1.json()['message_id']}, {r2.json()['message_id']})")


def test_waiting_lists_unanswered_mentions_with_what_the_agent_is_doing(node):
    """Only the owner reads it; an open mention shows its agent, a closed one
    does not; busy-before-the-mention is 'busy', busy-after is 'working'."""
    sql, as_, desk = node
    assert as_("admin").get(f"/api/desks/{desk}/waiting").status_code == 404
    mid = as_("owner").post(f"/api/desks/{desk}/mentions",
                            json={"agent": "permadmin", "content": "wait for me"}).json()["message_id"]
    w = as_("owner").get(f"/api/desks/{desk}/waiting").json()["waiting"]
    assert [(x["agent"], x["message_id"], x["state"]) for x in w] == [("permadmin", mid, "queued")]
    sql("INSERT INTO sn_core.agent_runtime (agent_slug, status, updated_at) "
        "VALUES ('permadmin', 'busy', now() - interval '1 hour') "
        "ON CONFLICT (agent_slug) DO UPDATE SET status = 'busy', updated_at = now() - interval '1 hour'")
    assert as_("owner").get(f"/api/desks/{desk}/waiting").json()["waiting"][0]["state"] == "busy"
    sql("UPDATE sn_core.agent_runtime SET updated_at = now() + interval '1 minute' "
        "WHERE agent_slug = 'permadmin'")
    assert as_("owner").get(f"/api/desks/{desk}/waiting").json()["waiting"][0]["state"] == "working"
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permadmin')")
    sql("UPDATE sn_core.agent_runtime SET status = 'free' WHERE agent_slug = 'permadmin'")
    assert as_("owner").get(f"/api/desks/{desk}/waiting").json()["waiting"] == []


def test_a_wake_for_one_chat_answers_that_chat_only(node):
    """The Runner claims the agent for one chat's messages (scope); while that
    claim stands core-api refuses it an answer to, or the history of, any other
    message: each chat is worked in its own context."""
    sql, as_, desk = node
    mine, other = (as_("owner").post(f"/api/desks/{desk}/mentions",
                                     json={"agent": "permadmin", "content": c}).json()["message_id"]
                   for c in ("first chat", "second chat"))
    r = as_("owner").post("/api/agents/permadmin/runtime",
                          json={"action": "claim", "ref": "wake-1", "scope": [mine]})
    assert r.status_code == 200 and r.json()["claimed"] is True, r.text
    r = as_("admin").post("/api/messages/reply", json={"to_message_id": other, "body": "no"})
    assert r.status_code == 409 and "own" in r.text, r.text
    assert as_("admin").get(f"/api/messages/{other}/chat-history").status_code == 409
    r = as_("admin").post("/api/messages/reply", json={"to_message_id": mine, "body": "yes"})
    assert r.status_code == 200, r.text
    # The claim is over: the scope no longer binds.
    as_("owner").post("/api/agents/permadmin/runtime", json={"action": "release"})
    r = as_("admin").post("/api/messages/reply", json={"to_message_id": other, "body": "now"})
    assert r.status_code == 200, r.text


def test_seen_is_kept_on_the_message(node):
    """A message handed to a wake is seen (message_state.handed_at): it does not
    wake the agent again until its reminder is due, after the agent's last turn
    ended; after every reminder it is listed as exhausted instead. Kept in the
    database, so a Runner restart does not wake everything again."""
    sql, as_, desk = node
    # what earlier tests left for this agent is a wake of its own
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permthird')")
    mid = as_("owner").post(f"/api/desks/{desk}/mentions",
                            json={"agent": "permthird", "content": "seen test"}).json()["message_id"]

    def third():
        (a,) = [a for a in as_("owner").get("/api/agents/with-pending-work").json()["agents"]
                if a["slug"] == "permthird"]
        return a

    assert third()["due_ids"] == [mid]
    # a person is waiting in the chat: the wake cap does not hold it back
    assert third()["attended"] is True
    r = as_("owner").post("/api/agents/permthird/runtime",
                          json={"action": "claim", "ref": "w1", "handed": [mid], "scope": [mid]})
    assert r.json()["claimed"] is True, r.text
    as_("owner").post("/api/agents/permthird/runtime", json={"action": "release"})
    assert third()["due_ids"] == []                       # seen, reminder not due yet
    sql("UPDATE sn_core.message_state SET handed_at = now() - interval '2 minutes' "
        f"WHERE message_id = {mid}")
    sql("UPDATE sn_core.agent_runtime SET updated_at = now() - interval '2 minutes' "
        "WHERE agent_slug = 'permthird'")
    assert third()["due_ids"] == [mid]                    # the reminder is due
    sql(f"UPDATE sn_core.message_state SET handed_count = 4 WHERE message_id = {mid}")
    a = third()
    assert a["due_ids"] == [] and a["exhausted_ids"] == [mid]
    sql(f"UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = {mid}")


def test_the_queue_shows_what_waits_for_the_agent(node):
    """The agent's card lists what waits for it: a chat mention with its chat,
    a colleague's message with its sender, each marked seen once a wake took it."""
    sql, as_, desk = node
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permthird')")
    mid = as_("owner").post(f"/api/desks/{desk}/mentions",
                            json={"agent": "permthird", "content": "queue test"}).json()["message_id"]
    as_("admin").post("/api/messages/send", json={"to": "permthird", "body": "a letter"})
    # a turn typed in the agent's own chat names its chat as "desk"
    sql("INSERT INTO sn_core.messages (direction, channel_type, external_id, sender, recipient, content, metadata) "
               f"VALUES ('inbound', 'internal', 'queue-test-turn', 'admin', 'permthird', 'hi', '{{\"desk\": \"{desk}\", \"source\": \"web-chat\"}}')")
    q = as_("owner").get("/api/agents/permthird/queue").json()
    assert [(x["what"], x["from"], x["chat"], x["seen"]) for x in q["waiting"]] == [
        ("chat", "admin", "mention test", False), ("message", "permadmin", None, False),
        ("chat", "admin", "mention test", False)]
    assert q["waiting"][2]["desk"] == desk
    sql(f"UPDATE sn_core.message_state SET handed_at = now() WHERE message_id = {mid}")
    assert as_("owner").get("/api/agents/permthird/queue").json()["waiting"][0]["seen"] is True
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id IN "
        "(SELECT id FROM sn_core.messages WHERE recipient = 'permthird')")
