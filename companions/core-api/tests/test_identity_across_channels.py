"""Coverage for identity across channels: the cross-channel read side
(`GET /api/messages/by-contact`), the web-chat registry-landing write side
(`POST /api/messages/record-webchat`), and the additive schema the release
introduced (`sn_core.message_identity`, `users.contact_id`,
`contact_channels.verified_by/verified_at/last_seen_at`).

Run inside the backend container against the migrated DB:
    docker compose exec -w /app backend python -m pytest tests/test_identity_across_channels.py -v

by-contact and /search take a principal now and scope every caller but
the whole-node seats to its own correspondence. These tests are about the
cross-channel resolution itself, so they run as the owner (see as_owner), who
sees the whole contact. Test data uses unique handles per run so runs do not
collide; `sn_core.messages` is append-only by trigger, so message rows are left
in place (as the other suites leave their tenant rows).
"""

import email.utils

import pytest

from auth import Principal, get_principal


@pytest.fixture(autouse=True)
def as_owner():
    """Stand in the owner for every request here: a TestClient cannot present a
    real identity, and its host reads as untrusted to get_actor."""
    from main import app
    app.dependency_overrides[get_principal] = lambda: Principal(
        username="admin", user_id=1, tenant_id=1, role="super-admin")
    yield
    app.dependency_overrides.pop(get_principal, None)


# --------------------------------------------------------------------------- helpers

def _channel_id(cur, slug):
    cur.execute("SELECT id FROM sn_config.channels WHERE slug = %s", (slug,))
    row = cur.fetchone()
    assert row is not None, f"channel {slug!r} must be registered"
    return row["id"]


def _insert_message(cur, channel_type, direction, sender, recipient, content):
    """Insert one sn_core.messages row the way the adapters do, seeding its state
    via the trigger. Returns the new id."""
    cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
    ext = email.utils.make_msgid(domain="test.identity")
    cur.execute(
        "INSERT INTO sn_core.messages "
        "  (channel_type, channel_id, external_id, direction, sender, recipient, "
        "   content, content_type, metadata, tenant_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'text/plain', '{}'::jsonb, 1) "
        "RETURNING id",
        (channel_type, sender, ext, direction, sender, recipient, content),
    )
    return cur.fetchone()["id"]


@pytest.fixture
def person(uid):
    """A throwaway contact with two channel handles (telegram + web-chat) and a
    message on each, plus an account (users row) carrying contact_id. Yields the
    contact_id and the two handles. Cleans up the config rows; messages stay
    (append-only)."""
    from db import init_pool, cursor
    init_pool()
    tg_handle = f"tg-{uid}"
    web_handle = f"web-{uid}"
    email_addr = f"{uid}@example.test"
    voice_addr = f"voice:{uid}"
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "INSERT INTO sn_config.contacts (tenant_id, kind, display_name, source) "
            "VALUES (1, 'person', %s, 'test') RETURNING id",
            (f"Test Person {uid}",),
        )
        cid = cur.fetchone()["id"]
        # Four channels the person is known on, so the recall must span them.
        chans = (("telegram", tg_handle, "platform-link"),
                 ("web-chat", web_handle, "session"),
                 ("email", email_addr, "dkim"),
                 ("voice", voice_addr, "session"))
        for slug, handle, vby in chans:
            cur.execute(
                "INSERT INTO sn_config.contact_channels "
                "  (contact_id, channel_id, handle, enabled, verified_by, verified_at) "
                "VALUES (%s, %s, %s, true, %s, now())",
                (cid, _channel_id(cur, slug), handle, vby),
            )
        # an account for this person, to exercise identity-by-account resolution
        cur.execute(
            "INSERT INTO sn_config.users (tenant_id, email, name, active, contact_id) "
            "VALUES (1, %s, %s, true, %s) RETURNING id",
            (email_addr, f"acct-{uid}", cid),
        )
        urow = cur.fetchone()["id"]
        # a voice user_channel, so record-voice-turns can resolve this person
        cur.execute(
            "INSERT INTO sn_config.user_channels (tenant_id, user_id, channel_slug, address, enabled) "
            "VALUES (1, %s, 'voice', %s, true)",
            (urow, voice_addr),
        )
        # one message on telegram/web/email, and MANY on voice: voice is the chatty
        # channel that must not crowd the others out of a no-channel recall.
        _insert_message(cur, "telegram", "inbound", tg_handle, "node-bot",
                         f"telegram hello {uid}")
        _insert_message(cur, "web-chat", "inbound", web_handle, "foreman",
                         f"web hello {uid}")
        _insert_message(cur, "email", "inbound", email_addr, "foreman@x",
                         f"email subject {uid}")
        _insert_message(cur, "email", "outbound", "foreman@x", email_addr,
                         f"email reply {uid}")
        for i in range(20):
            _insert_message(cur, "voice", "inbound", voice_addr, "foreman",
                            f"voice turn {i} {uid}")
    yield {"contact_id": cid, "tg": tg_handle, "web": web_handle,
           "email": email_addr, "voice": voice_addr}
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_config.user_channels WHERE user_id = %s", (urow,))
        # A recorded turn is stamped with who it is from; the stamp points at
        # the contact and has to go before the contact can.
        cur.execute("DELETE FROM sn_core.message_identity WHERE contact_id = %s", (cid,))
        cur.execute("DELETE FROM sn_config.contact_channels WHERE contact_id = %s", (cid,))
        cur.execute("DELETE FROM sn_config.users WHERE id = %s", (urow,))
        cur.execute("DELETE FROM sn_config.contacts WHERE id = %s", (cid,))


# ------------------------------------------------------------------ by-contact (read)

def test_by_contact_spans_every_channel(client, person):
    """The whole point: one query over the contact returns history from BOTH the
    telegram and the web-chat handle, not one channel."""
    r = client.get(f"/api/messages/by-contact?contact_id={person['contact_id']}&limit=20")
    assert r.status_code == 200
    body = r.json()
    assert body["contact"] == person["contact_id"]
    channels = {m["channel_type"] for m in body["messages"]}
    assert "telegram" in channels and "web-chat" in channels
    contents = " ".join(m["content"] for m in body["messages"])
    assert "telegram hello" in contents and "web hello" in contents


def test_by_contact_resolves_a_handle_to_the_contact(client, person):
    """Given a channel handle (not the contact id), it resolves through the one
    handle map to the same person and the same cross-channel history."""
    r = client.get(f"/api/messages/by-contact?identity={person['tg']}&limit=20")
    assert r.status_code == 200
    body = r.json()
    assert body["contact"] == person["contact_id"]
    assert {m["channel_type"] for m in body["messages"]} >= {"telegram", "web-chat"}


def test_by_contact_resolves_an_account_email(client, person):
    """An identity that is an account email (users.email) resolves via users.contact_id."""
    r = client.get(f"/api/messages/by-contact?identity={person['email']}&limit=20")
    assert r.status_code == 200
    assert r.json()["contact"] == person["contact_id"]


def test_by_contact_unknown_identity_is_empty_not_error(client):
    """An identity that maps to no contact returns an empty result, never a 500 --
    the read side must be safe to call on a first-time stranger."""
    r = client.get("/api/messages/by-contact?identity=nobody-xyz-unmapped")
    assert r.status_code == 200
    body = r.json()
    assert body["contact"] is None
    assert body["messages"] == []


def test_by_contact_requires_some_selector(client):
    """No identity and no contact_id: empty, not an error."""
    r = client.get("/api/messages/by-contact")
    assert r.status_code == 200
    assert r.json()["messages"] == []


# ------------------------------------------------------------- record-webchat (write)

# The recording routes are callbacks of the node's own services and are admitted
# by trusted origin: the service credential, the service's name, and its place
# in the permission list. The values below are this test's own.
_SERVICE_SECRET = "t" * 64


@pytest.fixture
def as_line(monkeypatch):
    """Headers of a node service listed for the recording callbacks."""
    monkeypatch.setenv("SN_SERVICE_AUTH_SECRET", _SERVICE_SECRET)
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", "voice:internal.messages.voice.write")
    return {"X-SN-Service": "voice", "X-SN-Service-Auth": _SERVICE_SECRET}


def test_record_webchat_writes_a_processed_pair(client, uid, as_line):
    """A desk turn lands one inbound + one outbound web-chat row, both settled to
    'processed' so nothing mistakes them for work awaiting delivery, under
    one shared thread."""
    from db import cursor
    session = f"itest-{uid}"
    r = client.post("/api/messages/record-webchat", headers=as_line, json={
        "identity": "admin", "session_id": session, "agent": "foreman",
        "turns": [{"role": "user", "text": f"desk in {uid}"},
                  {"role": "assistant", "text": f"desk out {uid}"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["recorded"] == 2
    assert body["thread_id"] == f"desk:{session}"
    with cursor() as cur:
        cur.execute(
            "SELECT m.direction, ms.status FROM sn_core.messages m "
            "JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "WHERE m.channel_type = 'web-chat' AND m.thread_id = %s ORDER BY m.id",
            (f"desk:{session}",),
        )
        rows = cur.fetchall()
    assert [r["direction"] for r in rows] == ["inbound", "outbound"]
    assert all(r["status"] == "processed" for r in rows)


def test_record_webchat_empty_turns_records_nothing(client, as_line):
    """No usable turns: records nothing, does not error."""
    r = client.post("/api/messages/record-webchat", headers=as_line, json={
        "identity": "admin", "turns": [{"role": "user", "text": "   "}],
    })
    assert r.status_code == 200
    assert r.json()["recorded"] == 0


# --------------------------------------------------------------------- schema (stage 1)

def test_message_identity_table_shape(client):
    """The arrival-time verdict table exists with the columns and the trust CHECK."""
    from db import cursor
    with cursor() as cur:
        cur.execute("SELECT to_regclass('sn_core.message_identity') AS t")
        assert cur.fetchone()["t"] is not None
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='sn_core' AND table_name='message_identity'"
        )
        cols = {row["column_name"] for row in cur.fetchall()}
    assert {"message_id", "contact_id", "handle", "trust", "evidence", "resolved_at"} <= cols


def test_contact_channels_has_proof_columns(client):
    """contact_channels became the one handle map and gained the proof columns."""
    from db import cursor
    with cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='sn_config' AND table_name='contact_channels'"
        )
        cols = {row["column_name"] for row in cur.fetchall()}
    assert {"verified_by", "verified_at", "last_seen_at"} <= cols


def test_users_has_contact_id(client):
    """An account points at the person it belongs to."""
    from db import cursor
    with cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='sn_config' AND table_name='users' AND column_name='contact_id'"
        )
        assert cur.fetchone() is not None


# ----------------------------------------------------------- messages_search (tool)

def test_search_spans_every_channel(client, person):
    """The catalog tool returns the person's history from EVERY channel they use
    -- telegram, web-chat, email AND voice -- resolved by identity."""
    r = client.post("/api/messages/search", json={"identity": person["email"], "limit": 40})
    assert r.status_code == 200
    body = r.json()
    assert body["contact"] == person["contact_id"]
    channels = {m["channel_type"] for m in body["messages"]}
    assert {"telegram", "web-chat", "email", "voice"} <= channels


def test_search_channel_filter_returns_only_that_channel(client, person):
    """channel=email returns email and nothing else -- 'what about email' answered
    precisely."""
    r = client.post("/api/messages/search",
                    json={"identity": person["email"], "channel": "email", "limit": 20})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert msgs, "expected the email messages"
    assert {m["channel_type"] for m in msgs} == {"email"}
    # the person's address is on the envelope (sender/recipient), not in the body
    assert any(person["email"] in ((m.get("sender") or "") + (m.get("recipient") or ""))
               for m in msgs)


def test_search_balances_channels_so_a_chatty_one_cannot_hide_the_rest(client, person):
    """The regression that shipped: voice (20 turns here) crowded email and
    telegram out of a no-channel recall, so 'what did we discuss by email' came
    back empty over an inbox full of it. With per-channel balancing, the quiet
    channels are still present even next to a loud one."""
    r = client.post("/api/messages/search", json={"identity": person["email"], "limit": 20})
    assert r.status_code == 200
    channels = {m["channel_type"] for m in r.json()["messages"]}
    assert "email" in channels and "telegram" in channels and "voice" in channels


def test_search_floors_a_tiny_limit(client, person):
    """The model sometimes passes limit=1; a recall of one message is never what was
    meant. A no-channel search still returns several, across channels."""
    r = client.post("/api/messages/search", json={"identity": person["email"], "limit": 1})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) > 1
    assert len({m["channel_type"] for m in msgs}) > 1


def test_search_unknown_identity_is_empty_not_error(client):
    r = client.post("/api/messages/search", json={"identity": "nobody-unmapped-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["contact"] is None and body["messages"] == []


# -------------------------------------------------------- record-voice-turns (write)

def test_record_voice_turns_is_idempotent(client, person, uid, as_line):
    """A turn keyed voice:<session>:<seq> is written once; a second write of the
    same seq is a no-op -- so the live per-turn write and the end-of-call
    reconciliation cannot double-write."""
    from db import cursor
    sess = f"vt-{uid}"
    payload = {"identity": person["email"], "session_id": sess, "agent": "foreman",
               "turns": [{"seq": 0, "role": "user", "text": f"voice turn probe {uid}"}]}
    r1 = client.post("/api/messages/record-voice-turns", headers=as_line, json=payload)
    r2 = client.post("/api/messages/record-voice-turns", headers=as_line, json=payload)
    assert r1.status_code == 200 and r1.json()["recorded"] == 1
    assert r2.status_code == 200 and r2.json()["recorded"] == 0
    with cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n, min(ms.status) AS status "
            "FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "WHERE m.external_id = %s",
            (f"voice:{sess}:0",),
        )
        row = cur.fetchone()
    assert row["n"] == 1
    assert row["status"] == "processed"
