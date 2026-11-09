"""Reminders: at a time the clock sends one message to whoever it is for, about the chat it was asked in.

"Remind me tomorrow at 9 to call Dana" is not a task (nobody works on it) and must
work on the smallest node, without the planner, on every agent runtime. So it is
a row here, and core-api watches the clock. A due row becomes one internal message
to whoever it is for, and message delivery takes it from there; this file delivers
nothing, and no model decides anything at that moment:

- the agent (anything an agent set, "check in an hour whether they answered",
  "remind me to call Dana"): addressed to the agent, tied to the chat and asking
  for a reply. The Runner wakes it like on any message, it carries out its command,
  and its answer lands in the chat.
- the person (set in the Calendar): addressed to the chat, their own words as they
  wrote them; the node has no words of its own to add. That needs no AI, and arrives
  even when the AI is down.

A chat deleted meanwhile does not lose a reminder: a new chat is opened with
that agent, for the person who owned the old one.

Times are the person's: "at 9" is 9 on the clock of whoever owns the chat
(sn_config.users.timezone, reported by their Studio), not the node's.

An agent reaches this through the `reminders` MCP server (mcp_reminders.py) over
the socket, so it is always itself: it sets, lists and cancels only its own. A
person (Studio's Calendar) sees and cancels all of the tenant's.
"""

import asyncio
import calendar
import email.utils
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2.extras import Json
from pydantic import BaseModel, Field

from auth import Principal, get_principal
from db import cursor
from events_emit import emit
from permissions import requires

log = logging.getLogger("core-api.reminders")
router = APIRouter(prefix="/api/reminders", tags=["reminders"])

COLS = "id, agent_slug, desk_id, text, due_at, repeat, status, created_by, created_at, sent_at, sent_count, recipient"


class ReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)  # what to remind of, the agent's words
    # One of the two. `at` without an offset is the chat owner's local time.
    at: datetime | None = None
    in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 366)
    repeat: Literal["day", "week", "month"] | None = None
    desk_id: str | None = None
    agent: str | None = None  # a person setting one names the agent; an agent is itself
    # Who gets it. By default whoever set it: an agent is woken, a person in the
    # Calendar gets their own words back.
    to: Literal["person", "agent"] | None = None


class TimezoneIn(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)


def _is_agent(p: Principal) -> bool:
    return p.actor_kind == "agent"


def _row(r) -> dict:
    d = dict(r)
    d["desk_id"] = str(d["desk_id"]) if d["desk_id"] else None
    return d


def _zone(name) -> tzinfo:
    """An IANA zone; the node's local zone when there is none or it is unknown."""
    try:
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().astimezone().tzinfo


def _desk_zone(cur, desk_id) -> tzinfo:
    """The time zone of the person whose chat it is: their 9 is what they meant."""
    cur.execute("SELECT u.timezone FROM sn_core.desks d JOIN sn_config.users u ON u.id = d.owner_user_id "
                "WHERE d.id::text = %s", (str(desk_id),))
    r = cur.fetchone()
    return _zone(r["timezone"] if r else None)


def _add_month(t: datetime) -> datetime:
    y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return t.replace(year=y, month=m, day=min(t.day, calendar.monthrange(y, m)[1]))


def _next(t: datetime, repeat: str, zone: tzinfo) -> datetime:
    """The same time on the person's clock. Counted in UTC, "every day at 14:20"
    came at 13:20 once daylight time ended."""
    local = t.astimezone(zone)
    nxt = _add_month(local) if repeat == "month" else local + timedelta(days=7 if repeat == "week" else 1)
    return nxt.astimezone(timezone.utc)


@router.put("/timezone", dependencies=[Depends(requires("reminders.read"))])
def set_my_timezone(body: TimezoneIn, principal: Principal = Depends(get_principal)):
    """The Studio reports the person's browser time zone here, so reminders are
    set and told on their clock. Lives with reminders: they are its only reader."""
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, "unknown time zone")
    with cursor(commit=True) as cur:
        cur.execute("UPDATE sn_config.users SET timezone = %s WHERE id = %s", (body.timezone, principal.user_id))
    return {"ok": True, "timezone": body.timezone}


@router.get("", dependencies=[Depends(requires("reminders.read"))])
def list_reminders(status: str = Query("scheduled"), agent: str | None = None, desk: str | None = None,
                   principal: Principal = Depends(get_principal)):
    if _is_agent(principal):
        agent = principal.username
    where, args = ["tenant_id = %s"], [principal.tenant_id]
    if status != "all":
        where.append("status = %s"); args.append(status)
    if agent:
        where.append("agent_slug = %s"); args.append(agent)
    with cursor() as cur:
        cur.execute(f"SELECT {COLS} FROM sn_core.reminders WHERE {' AND '.join(where)} "
                    "ORDER BY due_at LIMIT 200", args)
        rows = cur.fetchall()
        # `now` on the person's clock: the agent reads it to turn "tomorrow at 9" into a
        # time. For an agent the person is the one in the chat its session serves.
        if desk:
            zone = _desk_zone(cur, desk)
        else:
            cur.execute("SELECT timezone FROM sn_config.users WHERE id = %s", (principal.user_id,))
            u = cur.fetchone()
            zone = _zone(u["timezone"] if u else None)
    return {"now": datetime.now(zone).isoformat(timespec="minutes"),
            "reminders": [_row(r) for r in rows]}


@router.post("", status_code=201, dependencies=[Depends(requires("reminders.write"))])
def add_reminder(body: ReminderIn, principal: Principal = Depends(get_principal)):
    agent = principal.username if _is_agent(principal) else (body.agent or "").strip()
    if not agent:
        raise HTTPException(400, "name the agent that will remind")
    recipient = body.to or ("agent" if _is_agent(principal) else "person")
    if (body.at is None) == (body.in_minutes is None):
        raise HTTPException(400, "give either at (a time) or in_minutes")
    now = datetime.now(timezone.utc)
    with cursor(commit=True) as cur:
        # The chat it was asked in; without one (asked on another channel), the
        # agent's latest chat. A desk of another agent is refused: it would speak there.
        if body.desk_id:
            cur.execute("SELECT id, owner_user_id, metadata->>'agent' AS agent FROM sn_core.desks "
                        "WHERE id::text = %s AND tenant_id = %s", (body.desk_id, principal.tenant_id))
            d = cur.fetchone()
            if d is None:
                raise HTTPException(404, "no such chat")
            if d["agent"] and d["agent"] != agent:
                raise HTTPException(403, "that chat belongs to another agent")
        else:
            cur.execute("SELECT id, owner_user_id FROM sn_core.desks WHERE tenant_id = %s AND metadata->>'agent' = %s "
                        "AND status = 'active' ORDER BY updated_at DESC LIMIT 1", (principal.tenant_id, agent))
            d = cur.fetchone()
            if d is None:
                raise HTTPException(409, f"{agent} has no chat to remind in yet")
        zone = _desk_zone(cur, d["id"])
        due = now + timedelta(minutes=body.in_minutes) if body.in_minutes else \
            (body.at if body.at.tzinfo else body.at.replace(tzinfo=zone)).astimezone(timezone.utc)
        if due <= now:
            raise HTTPException(400, f"that time has passed (now is {datetime.now(zone).isoformat(timespec='minutes')})")
        cur.execute(f"INSERT INTO sn_core.reminders (tenant_id, agent_slug, desk_id, text, due_at, repeat, created_by, "
                    f"recipient, owner_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {COLS}",
                    (principal.tenant_id, agent, d["id"], body.text.strip(), due, body.repeat, principal.username,
                     recipient, d["owner_user_id"]))
        r = cur.fetchone()
        emit(cur, actor=f"{'agent' if _is_agent(principal) else 'user'}:{principal.username}",
             event_type="reminder.set", entity_type="reminder", entity_id=str(r["id"]),
             payload={"agent": agent, "desk": str(d["id"]), "due_at": due.isoformat(), "repeat": body.repeat, "to": recipient})
    out = _row(r)
    # the person's own clock; the id is for cancelling, not for saying
    out["due_local"] = r["due_at"].astimezone(zone).isoformat(timespec="minutes")
    return out


@router.delete("/{reminder_id}", dependencies=[Depends(requires("reminders.write"))])
def cancel_reminder(reminder_id: int, principal: Principal = Depends(get_principal)):
    with cursor(commit=True) as cur:
        cur.execute("UPDATE sn_core.reminders SET status = 'cancelled' "
                    "WHERE id = %s AND tenant_id = %s AND status = 'scheduled' "
                    "AND (%s OR agent_slug = %s) RETURNING id, agent_slug, desk_id",
                    (reminder_id, principal.tenant_id, not _is_agent(principal), principal.username))
        r = cur.fetchone()
        if r is None:
            raise HTTPException(404, "no such reminder waiting")
        emit(cur, actor=f"{'agent' if _is_agent(principal) else 'user'}:{principal.username}",
             event_type="reminder.cancelled", entity_type="reminder", entity_id=str(r["id"]),
             payload={"agent": r["agent_slug"], "desk": str(r["desk_id"])})
    return {"ok": True, "id": reminder_id}


def _live_desk(cur, r, opened: dict) -> str:
    """The reminder's chat, or a new one with its agent when that was deleted. Every
    reminder still waiting on the deleted chat moves with it, so they arrive
    together (`opened` covers the ones already picked up in this pass)."""
    old = str(r["desk_id"])
    if old in opened:
        return opened[old]
    cur.execute("SELECT 1 FROM sn_core.desks WHERE id::text = %s", (old,))
    if cur.fetchone():
        return old
    cur.execute("INSERT INTO sn_core.desks (tenant_id, owner_user_id, title, metadata) "
                "VALUES (%s, %s, %s, jsonb_build_object('agent', %s::text)) RETURNING id",
                (r["tenant_id"], r["owner_user_id"], r["text"][:60], r["agent_slug"]))
    new = str(cur.fetchone()["id"])
    cur.execute("UPDATE sn_core.reminders SET desk_id = %s WHERE desk_id::text = %s", (new, old))
    opened[old] = new
    return new


def _to_person(cur, r, desk) -> int:
    # Addressed to the chat. Delivering it is message delivery's job, as for any
    # row to desk:<id>: the watcher asks deliver-desk, the history shows what
    # passed (desks.py source 2) and marks this one with the bell.
    cur.execute(
        "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, "
        "sender, recipient, content, content_type, metadata, tenant_id) "
        "VALUES ('internal', 'system', %s, %s, 'inbound', 'system', %s, %s, 'text/plain', %s, %s) RETURNING id",
        (email.utils.make_msgid(domain="reminders.local"), f"desk:{desk}", f"desk:{desk}",
         r["text"], Json({"source": "reminder", "reminder_id": r["id"]}), r["tenant_id"]))
    return cur.fetchone()["id"]


def _to_agent(cur, r, desk) -> int:
    # From the system, tied to the chat by its origin: _relay_desk_for relays the
    # answer of the chat's own agent back into it.
    # needs_reply: the node holds it open until it is answered, and says so in the
    # chat if it never is (sweep_reply_obligations).
    ext = email.utils.make_msgid(domain="reminders.local")
    zone = _desk_zone(cur, desk)
    when = r["created_at"].astimezone(zone).strftime("%Y-%m-%d %H:%M")
    now = datetime.now(zone).strftime("%Y-%m-%d %H:%M")
    # The command may keep the request's words, "in 5 minutes" included: without saying
    # the time has come, that reads as a new request to set a reminder.
    body = ("REMINDER TRIGGERED: The scheduled time has arrived.\n"
            f'Your command: "{r["text"]}" (set on {when})\n'
            "Carry it out now. This is not a new request: do not set another reminder.\n"
            f"It is now {now} on the person's clock: tell times on it. "
            "What you answer goes into the person's chat.")
    cur.execute(
        "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, "
        "sender, recipient, content, content_type, metadata, tenant_id) "
        "VALUES ('internal', 'system', %s, %s, 'inbound', 'system', %s, %s, 'text/plain', %s, %s) RETURNING id",
        (ext, ext, r["agent_slug"], body,
         Json({"source": "reminder", "reminder_id": r["id"], "needs_reply": True, "reminder_text": r["text"],
               "desk_id": desk,
               "origin": {"channel": "web-chat", "conversation": f"desk:{desk}"}}),
         r["tenant_id"]))
    mid = cur.fetchone()["id"]
    emit(cur, actor="component:core-api", event_type="message.inbound",
         entity_type="message", entity_id=str(mid),
         payload={"from": "system", "to": r["agent_slug"], "channel": "internal",
                  "preview": body[:120]})
    return mid


def fire_due(limit: int = 50) -> int:
    """Send every due reminder to whoever it is for. A repeating one moves to its
    next time in the future (a node that was off does not replay the days it
    missed)."""
    now = datetime.now(timezone.utc)
    with cursor(commit=True) as cur:
        # The seed-state trigger on sn_core.messages INSERTs into message_state unqualified.
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(f"SELECT {COLS}, tenant_id, owner_user_id FROM sn_core.reminders WHERE status = 'scheduled' AND due_at <= %s "
                    "ORDER BY due_at LIMIT %s FOR UPDATE SKIP LOCKED", (now, limit))
        rows = cur.fetchall()
        opened = {}
        for r in rows:
            n = r["sent_count"] + 1
            desk = _live_desk(cur, r, opened)
            mid = (_to_agent if r["recipient"] == "agent" else _to_person)(cur, r, desk)
            if r["repeat"]:
                zone = _desk_zone(cur, desk)
                nxt = _next(r["due_at"], r["repeat"], zone)
                while nxt <= now:
                    nxt = _next(nxt, r["repeat"], zone)
                cur.execute("UPDATE sn_core.reminders SET due_at = %s, sent_at = %s, sent_count = %s WHERE id = %s",
                            (nxt, now, n, r["id"]))
            else:
                cur.execute("UPDATE sn_core.reminders SET status = 'sent', sent_at = %s, sent_count = %s WHERE id = %s",
                            (now, n, r["id"]))
            emit(cur, actor="component:core-api", event_type="reminder.sent", entity_type="reminder",
                 entity_id=str(r["id"]), payload={"agent": r["agent_slug"], "message_id": mid,
                                                   "desk": desk, "repeat": r["repeat"], "to": r["recipient"]})
    return len(rows)


async def fire_loop(interval: float = 10.0):
    while True:
        try:
            sent = await asyncio.to_thread(fire_due)
            if sent:
                log.info("reminders: delivered %d", sent)
        except Exception:
            log.exception("reminders: fire pass failed")
        await asyncio.sleep(interval)
