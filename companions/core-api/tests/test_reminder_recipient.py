"""A due reminder is one message to its recipient, written by the clock.

The same internal message either way, from the system, and message delivery
takes it from there. To the person: addressed to the chat, the watcher delivers
it, no model. To the agent: addressed to the agent, the Runner wakes it. A chat
deleted meanwhile does not lose it: a new chat is opened with that agent.

Run inside the backend container against the migrated DB.
"""

import uuid

import pytest
from fastapi import HTTPException


@pytest.fixture
def db():
    from db import init_pool, cursor
    init_pool()
    yield cursor
    # The test agents exist only here; left behind they read as real chats.
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.reminders WHERE agent_slug LIKE 'rem-%%'")
        cur.execute("DELETE FROM sn_core.desks WHERE metadata->>'agent' LIKE 'rem-%%'")


def _owner(cur):
    cur.execute("SELECT min(id) AS id FROM sn_config.users")
    return cur.fetchone()["id"]


def _desk(cur, agent):
    desk = str(uuid.uuid4())
    cur.execute("INSERT INTO sn_core.desks (id, tenant_id, owner_user_id, title, metadata) "
                "VALUES (%s::uuid, 1, %s, 'reminder test', jsonb_build_object('agent', %s::text))",
                (desk, _owner(cur), agent))
    return desk


def _due(cur, agent, desk, text, to="person"):
    cur.execute("INSERT INTO sn_core.reminders (agent_slug, desk_id, text, due_at, created_by, recipient, owner_user_id) "
                "VALUES (%s, %s, %s, now() - interval '1 second', %s, %s, %s) RETURNING id",
                (agent, desk, text, agent, to, _owner(cur)))
    return cur.fetchone()["id"]


def _messages(cur, rid):
    cur.execute("SELECT * FROM sn_core.messages WHERE metadata->>'reminder_id' = %s", (str(rid),))
    return cur.fetchall()


def test_to_the_person_is_a_message_from_the_system_to_the_chat(db):
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "stand up")
    fire_due()
    with db() as cur:
        m = _messages(cur, rid)
    assert len(m) == 1 and m[0]["content"] == "stand up"
    assert m[0]["channel_type"] == "internal" and m[0]["direction"] == "inbound"
    assert m[0]["sender"] == "system" and m[0]["recipient"] == f"desk:{desk}"


def test_to_the_agent_is_an_internal_message_that_wakes_it(db):
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "check whether the mail was answered", to="agent")
    fire_due()
    with db() as cur:
        m = _messages(cur, rid)
        cur.execute("SELECT status FROM sn_core.message_state WHERE message_id = %s", (m[0]["id"],))
        state = cur.fetchone()["status"]
    assert len(m) == 1 and m[0]["channel_type"] == "internal" and m[0]["direction"] == "inbound"
    assert m[0]["sender"] == "system"
    assert m[0]["recipient"] == agent and "check whether the mail was answered" in m[0]["content"]
    assert m[0]["metadata"]["needs_reply"] is True
    assert m[0]["metadata"]["origin"] == {"channel": "web-chat", "conversation": f"desk:{desk}"}
    assert state == "new"


def test_the_chat_shows_the_line_as_the_systems(db):
    from types import SimpleNamespace
    from routes.desks import desk_history
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "stand up")
    fire_due()
    with db(commit=True) as cur:
        cur.execute("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = %s",
                    (_messages(cur, rid)[0]["id"],))
    (line,) = desk_history(desk, SimpleNamespace(tenant_id=1))["messages"]
    assert line["content"] == "stand up" and line["reminder"] is True and line["system"] is True


def test_the_agents_answer_lands_in_the_chat_as_its_own(db):
    from auth import Principal
    from types import SimpleNamespace
    from routes.desks import desk_history
    from routes.messages import MessageReply, reply_to_message
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "check the mail", to="agent")
    fire_due()
    with db() as cur:
        mid = _messages(cur, rid)[0]["id"]
    reply_to_message(MessageReply(to_message_id=mid, body="It was answered."),
                     Principal(username=agent, user_id=-1, tenant_id=1, role="agent",
                               actor_id=f"agent:{agent}", actor_kind="agent"))
    with db(commit=True) as cur:
        cur.execute("UPDATE sn_core.message_state ms SET status = 'processed' FROM sn_core.messages m "
                    "WHERE ms.message_id = m.id AND m.recipient = %s", (f"desk:{desk}",))
    (answer,) = desk_history(desk, SimpleNamespace(tenant_id=1))["messages"]
    assert answer["content"] == "It was answered." and "system" not in answer


def _agent_message(db, fire_due, agent):
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "check the mail", to="agent")
    fire_due()
    with db() as cur:
        return desk, _messages(cur, rid)[0]["id"]


def _as_agent(agent):
    from auth import Principal
    return Principal(username=agent, user_id=-1, tenant_id=1, role="agent",
                     actor_id=f"agent:{agent}", actor_kind="agent")


def _relayed(db, desk):
    with db() as cur:
        cur.execute("SELECT content FROM sn_core.messages WHERE recipient = %s", (f"desk:{desk}",))
        return [r["content"] for r in cur.fetchall()]


def test_final_text_answers_even_a_message_the_agent_closed_itself(db):
    """2026-10-31: the agent wrote "Delivered the word" and dismissed the message."""
    from routes.messages import FinalText, MessageDismiss, dismiss_message, final_text
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    desk, mid = _agent_message(db, fire_due, agent)
    dismiss_message(MessageDismiss(message_id=mid), _as_agent(agent))
    final_text(FinalText(message_ids=[mid], body="The word is gone."), _as_agent(agent))
    assert _relayed(db, desk) == ["The word is gone."]


def test_final_text_after_a_reply_is_a_summary_and_is_not_sent(db):
    from routes.messages import FinalText, MessageReply, final_text, reply_to_message
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    desk, mid = _agent_message(db, fire_due, agent)
    reply_to_message(MessageReply(to_message_id=mid, body="The answer."), _as_agent(agent))
    out = final_text(FinalText(message_ids=[mid], body="I answered."), _as_agent(agent))
    assert out["sent"] is False and _relayed(db, desk) == ["The answer."]


def test_a_deleted_chat_gets_one_new_chat_for_all_its_reminders(db):
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        a = _due(cur, agent, desk, "first")
        b = _due(cur, agent, desk, "second", to="agent")
        cur.execute("DELETE FROM sn_core.desks WHERE id = %s", (desk,))
    fire_due()
    with db() as cur:
        cur.execute("SELECT id, owner_user_id, metadata->>'agent' AS agent FROM sn_core.desks "
                    "WHERE metadata->>'agent' = %s", (agent,))
        desks = cur.fetchall()
        cur.execute("SELECT DISTINCT desk_id FROM sn_core.reminders WHERE id = ANY(%s)", ([a, b],))
        moved = cur.fetchall()
        first = _messages(cur, a)[0]
        second = _messages(cur, b)[0]
        owner = _owner(cur)
    assert len(desks) == 1 and desks[0]["owner_user_id"] == owner
    new = str(desks[0]["id"])
    assert new != desk and [str(r["desk_id"]) for r in moved] == [new]
    assert first["recipient"] == f"desk:{new}"
    assert second["metadata"]["origin"]["conversation"] == f"desk:{new}"


def test_an_agent_that_never_answers_is_said_in_the_chat(db, monkeypatch):
    from routes import messages
    from routes.reminders import fire_due
    monkeypatch.setenv("SN_REPLY_WAKE_WINDOW_SECONDS", "600")
    monkeypatch.setenv("SN_REPLY_MAX_ATTEMPTS", "1")
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "check the mail", to="agent")
    fire_due()
    with db(commit=True) as cur:
        mid = _messages(cur, rid)[0]["id"]
        cur.execute("UPDATE sn_core.message_state SET status = 'awaiting_reply', wake_attempts = 1, "
                    "wake_next_at = now() - interval '1 second' WHERE message_id = %s", (mid,))
        messages.sweep_reply_obligations(cur)
    with db() as cur:
        cur.execute("SELECT content FROM sn_core.messages WHERE channel_id = %s AND sender = 'system'",
                    (f"desk:{desk}",))
        lines = [r["content"] for r in cur.fetchall()]
    assert len(lines) == 1 and "check the mail" in lines[0]


@pytest.fixture
def owner_zone(db):
    """Sets the chat owner's time zone for one test."""
    with db() as cur:
        cur.execute("SELECT id, timezone FROM sn_config.users WHERE id = (SELECT min(id) FROM sn_config.users)")
        was = cur.fetchone()

    def set_zone(name):
        with db(commit=True) as cur:
            cur.execute("UPDATE sn_config.users SET timezone = %s WHERE id = %s", (name, was["id"]))
    yield set_zone
    set_zone(was["timezone"])


def test_a_daily_repeat_keeps_the_persons_clock_when_daylight_time_ends():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from routes.reminders import _next
    central = ZoneInfo("America/Winnipeg")
    at = datetime(2026, 10, 31, 19, 20, tzinfo=timezone.utc)   # 14:20 in Winnipeg, the last day of daylight time
    assert _next(at, "day", central) == datetime(2026, 11, 1, 20, 20, tzinfo=timezone.utc)
    assert _next(at, "week", central) == datetime(2026, 11, 7, 20, 20, tzinfo=timezone.utc)
    assert _next(at, "month", central) == datetime(2026, 11, 30, 20, 20, tzinfo=timezone.utc)


def test_firing_moves_a_repeat_on_the_clock_of_the_chats_owner(db, owner_zone):
    from zoneinfo import ZoneInfo
    from routes.reminders import fire_due
    owner_zone("America/Winnipeg")
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "stand up")
        cur.execute("UPDATE sn_core.reminders SET due_at = '2026-10-31 19:20+00', repeat = 'day' WHERE id = %s", (rid,))
    fire_due()
    with db() as cur:
        cur.execute("SELECT due_at FROM sn_core.reminders WHERE id = %s", (rid,))
        due = cur.fetchone()["due_at"]
    assert due.astimezone(ZoneInfo("America/Winnipeg")).strftime("%H:%M") == "14:20"


def test_the_wake_tells_the_time_on_the_persons_clock(db, owner_zone):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from routes.reminders import fire_due
    owner_zone("Pacific/Kiritimati")   # 14 hours from UTC: another date most of the day
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "check the mail", to="agent")
    fire_due()
    with db() as cur:
        (m,) = _messages(cur, rid)
    assert "It is now " + datetime.now(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d") in m["content"]


def test_the_agent_has_one_tool_and_nothing_to_choose():
    """Two tools or a recipient to fill: weak models chose wrongly and lost the work
    (2026-10-31). One tool that wakes the agent leaves no choice to get wrong."""
    import ast
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp_reminders.py"), encoding="utf-8").read()
    fns = {n.name: n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef)}
    assert not {"set_reminder", "remind_agent", "remind_user"} & set(fns)
    args = [a.arg for a in fns["remind_me"].args.args]
    assert "command" in args and "to" not in args


def _set_as_agent(db, **fields):
    from routes.reminders import ReminderIn, add_reminder
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
    return agent, ReminderIn(text="x", in_minutes=5, desk_id=desk, **fields), add_reminder


def test_each_kind_is_kept(db):
    agent, body, add = _set_as_agent(db)
    assert add(body.model_copy(update={"to": "agent"}), _as_agent(agent))["recipient"] == "agent"
    assert add(body.model_copy(update={"to": "person"}), _as_agent(agent))["recipient"] == "person"


def test_one_set_by_an_agent_wakes_that_agent(db):
    agent, body, add = _set_as_agent(db)
    assert add(body, _as_agent(agent))["recipient"] == "agent"


def test_setting_and_cancelling_name_the_chat_so_an_open_calendar_hears_of_it(db):
    """The live stream hands an event to the owner of the chat it names; without
    the chat, a reminder set in another tab appeared only after a reload."""
    from routes.reminders import cancel_reminder
    agent, body, add = _set_as_agent(db, to="agent")
    who = _as_agent(agent)
    rid = add(body, who)["id"]
    cancel_reminder(rid, who)
    with db() as cur:
        cur.execute("SELECT event_type, payload->>'desk' AS desk FROM sn_core.events "
                    "WHERE entity_type = 'reminder' AND entity_id = %s", (str(rid),))
        got = {r["event_type"]: r["desk"] for r in cur.fetchall()}
    assert got == {"reminder.set": body.desk_id, "reminder.cancelled": body.desk_id}


def test_a_person_setting_one_in_the_calendar_sets_it_for_themselves(db):
    from auth import Principal
    agent, body, add = _set_as_agent(db, agent=None)
    body = body.model_copy(update={"agent": agent})
    with db() as cur:
        owner = _owner(cur)
    person = Principal(username="o", user_id=owner, tenant_id=1, role="admin", actor_id="user:o", actor_kind="human")
    assert add(body, person)["recipient"] == "person"


def test_the_wake_gives_the_command_to_carry_out(db):
    from routes.reminders import fire_due
    agent = "rem-" + uuid.uuid4().hex[:8]
    with db(commit=True) as cur:
        desk = _desk(cur, agent)
        rid = _due(cur, agent, desk, "send the mail in 5 minutes", to="agent")
    fire_due()
    with db() as cur:
        (m,) = _messages(cur, rid)
    assert m["content"].startswith("REMINDER TRIGGERED")
    assert 'Your command: "send the mail in 5 minutes"' in m["content"]
    assert "another reminder" in m["content"]
    assert "if applicable" not in m["content"]
