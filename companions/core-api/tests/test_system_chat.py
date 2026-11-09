"""The System chat: what needs the owner and has nowhere else to go.

An ordinary chat with no agent, fed by internal messages from the system through
normal delivery. Rises by its new messages like any chat; there is one per owner
and it cannot be deleted.

Run inside the backend container against the migrated DB.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.fixture
def db():
    from db import init_pool, cursor
    init_pool()
    yield cursor
    # The messages stay: the table keeps them. Without the chat nothing reads them,
    # and the next test gets a clean one.
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.desks WHERE metadata->>'system' = 'true'")


def _tell(db, text, key):
    from lib.system_chat import tell_owner
    with db(commit=True) as cur:
        return tell_owner(cur, 1, text, key)


def _lines(db, key):
    with db() as cur:
        cur.execute("SELECT sender, recipient, content, channel_type, direction FROM sn_core.messages "
                    "WHERE metadata->>'key' = %s ORDER BY id", (key,))
        return cur.fetchall()


def _system_desks(db):
    with db() as cur:
        cur.execute("SELECT id::text AS id, title, updated_at, metadata FROM sn_core.desks WHERE metadata->>'system' = 'true'")
        return cur.fetchall()


def _key():
    return "test:" + uuid.uuid4().hex


def test_a_message_from_the_system_to_its_chat(db):
    key = _key()
    assert _tell(db, "Something needs you.", key)
    (desk,) = _system_desks(db)
    assert desk["title"] == "System" and "agent" not in desk["metadata"]
    (line,) = _lines(db, key)
    assert line["sender"] == "system" and line["recipient"] == "desk:" + desk["id"]
    assert line["channel_type"] == "internal" and line["direction"] == "inbound"


def test_one_chat_per_owner(db):
    _tell(db, "one", _key())
    _tell(db, "two", _key())
    assert len(_system_desks(db)) == 1


def test_a_new_message_raises_the_chat_and_reopens_its_tab(db):
    _tell(db, "one", _key())
    (before,) = _system_desks(db)
    with db(commit=True) as cur:
        cur.execute("UPDATE sn_core.desks SET metadata = metadata || '{\"tab_open\": false}' WHERE id::text = %s", (before["id"],))
    _tell(db, "two", _key())
    (after,) = _system_desks(db)
    assert after["updated_at"] > before["updated_at"] and "tab_open" not in after["metadata"]


def test_the_same_thing_does_not_repeat_while_unread(db):
    key = _key()
    assert _tell(db, "broken", key)
    assert not _tell(db, "broken", key)
    assert len(_lines(db, key)) == 1
    (desk,) = _system_desks(db)
    with db(commit=True) as cur:
        cur.execute("UPDATE sn_core.desks SET metadata = jsonb_set(metadata, '{read_upto}', to_jsonb(now()::text)) "
                    "WHERE id::text = %s", (desk["id"],))
    assert _tell(db, "broken again", key)
    assert len(_lines(db, key)) == 2


def test_it_cannot_be_deleted(db):
    from auth import Principal
    from routes.desks import delete_desk
    _tell(db, "one", _key())
    (desk,) = _system_desks(db)
    with db() as cur:
        cur.execute("SELECT owner_user_id FROM sn_core.desks WHERE id::text = %s", (desk["id"],))
        owner = cur.fetchone()["owner_user_id"]
    with pytest.raises(HTTPException) as e:
        delete_desk(desk["id"], Principal(username="o", user_id=owner, tenant_id=1, role="admin",
                                          actor_id="user:o", actor_kind="human"))
    assert e.value.status_code == 409 and len(_system_desks(db)) == 1


def test_its_lines_read_as_the_systems(db):
    from routes.desks import desk_history
    key = _key()
    _tell(db, "Something needs you.", key)
    (desk,) = _system_desks(db)
    with db(commit=True) as cur:
        cur.execute("UPDATE sn_core.message_state ms SET status = 'processed' FROM sn_core.messages m "
                    "WHERE ms.message_id = m.id AND m.metadata->>'key' = %s", (key,))
    lines = [m for m in desk_history(desk["id"], SimpleNamespace(tenant_id=1))["messages"]
             if m["content"] == "Something needs you."]
    assert lines and all(m["system"] is True for m in lines) and "reminder" not in lines[0]


def test_a_broken_agent_is_told(db):
    from routes.agents import RuntimeAction, agent_runtime_action
    with db() as cur:
        cur.execute("SELECT slug FROM sn_config.agents WHERE enabled AND archived_at IS NULL ORDER BY id LIMIT 1")
        slug = cur.fetchone()["slug"]
        cur.execute("SELECT status, last_error FROM sn_core.agent_runtime WHERE agent_slug = %s", (slug,))
        was = cur.fetchone()
    try:
        agent_runtime_action(slug, RuntimeAction(action="broken", error="wake 500 (3 consecutive)"))
        lines = _lines(db, f"broken:{slug}")
        assert lines and "wake 500" not in lines[-1]["content"] and f"/agents/{slug}" in lines[-1]["content"]
    finally:
        with db(commit=True) as cur:
            cur.execute("UPDATE sn_core.agent_runtime SET status = %s, last_error = %s WHERE agent_slug = %s",
                        (was["status"] if was else "free", was["last_error"] if was else None, slug))


def _overdue(db, monkeypatch, sender, recipient, meta):
    from psycopg2.extras import Json
    from routes import messages
    monkeypatch.setenv("SN_REPLY_WAKE_WINDOW_SECONDS", "600")
    monkeypatch.setenv("SN_REPLY_MAX_ATTEMPTS", "1")
    ext = uuid.uuid4().hex
    with db(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute("INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, "
                    "recipient, content, content_type, metadata, tenant_id) VALUES ('internal', 'system', %s, %s, 'inbound', "
                    "%s, %s, 'the request', 'text/plain', %s, 1) RETURNING id",
                    (ext, ext, sender, recipient, Json(dict(meta, needs_reply=True))))
        mid = cur.fetchone()["id"]
        cur.execute("UPDATE sn_core.message_state SET status = 'awaiting_reply', wake_attempts = 1, "
                    "wake_next_at = now() - interval '1 second' WHERE message_id = %s", (mid,))
        messages.sweep_reply_obligations(cur)
    return mid


def test_an_unanswered_request_from_someone_with_no_inbox_is_told(db, monkeypatch):
    mid = _overdue(db, monkeypatch, "person-" + uuid.uuid4().hex[:6], "sys-" + uuid.uuid4().hex[:6], {})
    lines = _lines(db, f"overdue:{mid}")
    assert len(lines) == 1 and "#" not in lines[0]["content"] and "the request" in lines[0]["content"]


def test_an_unanswered_reminder_whose_chat_is_gone_is_told(db, monkeypatch):
    gone = str(uuid.uuid4())
    mid = _overdue(db, monkeypatch, "system", "sys-" + uuid.uuid4().hex[:6],
                   {"reminder_text": "check the mail",
                    "origin": {"channel": "web-chat", "conversation": f"desk:{gone}"}})
    lines = _lines(db, f"overdue:{mid}")
    assert len(lines) == 1 and "check the mail" in lines[0]["content"]


def test_the_agent_is_told_by_its_name_not_its_slug(db, monkeypatch):
    with db() as cur:
        cur.execute("SELECT slug, title FROM sn_config.agents WHERE enabled AND archived_at IS NULL "
                    "AND title IS NOT NULL AND title <> slug ORDER BY id LIMIT 1")
        a = cur.fetchone()
    mid = _overdue(db, monkeypatch, "system", a["slug"],
                   {"reminder_text": "check the mail",
                    "origin": {"channel": "web-chat", "conversation": f"desk:{uuid.uuid4()}"}})
    (line,) = _lines(db, f"overdue:{mid}")
    assert a["title"] in line["content"] and a["slug"] not in line["content"]


def _event(db, event_type, entity_id, payload):
    from events_emit import emit
    with db(commit=True) as cur:
        emit(cur, actor="component:core-api", event_type=event_type, entity_type="message",
             entity_id=entity_id, payload=payload)


def _watch():
    from lib import system_chat
    system_chat.read_events()


def test_a_failed_send_is_told(db):
    from lib import system_chat
    _watch()   # the first pass only takes its place in the journal
    channel = "ch" + uuid.uuid4().hex[:6]
    _event(db, "message.failed", "901", {"channel": channel, "stage": "transmit", "error": "SMTP refused"})
    _event(db, "message.failed", "902", {"channel": channel, "stage": "transmit", "error": "SMTP refused"})
    _watch()
    lines = _lines(db, f"failed:{channel}")
    assert len(lines) == 1 and "#" not in lines[0]["content"] and "SMTP refused" in lines[0]["content"]


def test_budget_events_are_told_in_their_own_words(db):
    _watch()
    period = "p" + uuid.uuid4().hex[:6]
    _event(db, "budget.exhausted", "node", {"text": "Budget exhausted: raise the limit.", "period": period})
    _watch()
    (line,) = _lines(db, f"budget.exhausted:{period}")
    assert line["content"] == "Budget exhausted: raise the limit."


def test_an_ordinary_event_is_not_told(db):
    _watch()
    key = uuid.uuid4().hex
    _event(db, "agent.woken", key, {"text": "nothing to see"})
    _watch()
    with db() as cur:
        cur.execute("SELECT count(*) AS n FROM sn_core.messages WHERE content = 'nothing to see'")
        assert cur.fetchone()["n"] == 0


def test_a_failed_service_is_told_once_while_it_stays_down(db, monkeypatch):
    from lib import system_chat
    name = "smart-node-" + uuid.uuid4().hex[:6] + ".service"
    monkeypatch.setattr(system_chat, "_failed_units", lambda: {name})
    system_chat._down.clear()
    system_chat.read_units()
    (desk,) = _system_desks(db)
    with db(commit=True) as cur:   # read: only "still down" keeps it from coming back
        cur.execute("UPDATE sn_core.desks SET metadata = jsonb_set(metadata, '{read_upto}', to_jsonb(now()::text)) "
                    "WHERE id::text = %s", (desk["id"],))
    system_chat.read_units()
    assert len(_lines(db, f"service:{name}")) == 1
    monkeypatch.setattr(system_chat, "_failed_units", lambda: set())
    system_chat.read_units()
    monkeypatch.setattr(system_chat, "_failed_units", lambda: {name})
    system_chat.read_units()
    assert len(_lines(db, f"service:{name}")) == 2


def test_the_unit_list_reads_systemd_output(monkeypatch):
    from lib import system_chat
    out = ("smart-node-event-digest.service loaded failed failed Smart Node daily event digest\n"
           "smart-node-x.service loaded failed failed X\n")
    monkeypatch.setattr(system_chat.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    assert system_chat._failed_units() == {"smart-node-event-digest.service", "smart-node-x.service"}
