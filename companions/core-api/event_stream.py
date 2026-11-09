"""The one live event stream.

sn_core.events announces every row on channel sn_events when it commits (the
events_notify trigger, B02). One listener per core-api process holds a
connection of its own on that channel and hands each subscriber the events of
the chats that person owns. The Studio reads this instead of polling.

What a subscriber gets is an address, not the row: {"id", "type", "desk"}.
The page then reads what changed through the ordinary routes, with the
person's own rights, so nothing here widens what anyone can see.
"""

import asyncio
import json
import logging

import psycopg2
import psycopg2.extensions

from db import DB_NAME

log = logging.getLogger("core-api.event_stream")

CHANNEL = "sn_events"

# (tenant_id, user_id) -> the queues of that person's open streams
_subscribers: dict[tuple[int, int], set[asyncio.Queue]] = {}
# desk id -> (tenant_id, owner_user_id); a desk's owner never changes
_desk_owner: dict[str, tuple[int, int] | None] = {}

# The Runner's streams. It used to look for work every 5 s, so a message waited
# 2.5 s on average before its agent was woken. It now hears that something that
# may be work has arrived and looks at once; the poll stays as the fallback.
# It gets the event type only: what the work is it reads through its own route.
WAKE_TYPES = {"message.inbound", "agent.wake_requested", "reminder.sent"}
_wakers: set[asyncio.Queue] = set()


def subscribe_wakes() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _wakers.add(q)
    return q


def unsubscribe_wakes(q: asyncio.Queue) -> None:
    _wakers.discard(q)


def subscribe(tenant_id: int, user_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.setdefault((tenant_id, user_id), set()).add(q)
    return q


def unsubscribe(tenant_id: int, user_id: int, q: asyncio.Queue) -> None:
    subs = _subscribers.get((tenant_id, user_id))
    if subs:
        subs.discard(q)
        if not subs:
            _subscribers.pop((tenant_id, user_id), None)


def _desk_of(note: dict) -> str | None:
    """The chat an event is about: a desk entity ("<id>" or "desk:<id>"), or
    the desk a reminder or relay names in its payload."""
    if note.get("entity_type") == "desk" and note.get("entity_id"):
        return str(note["entity_id"]).removeprefix("desk:")
    return note.get("desk")


def _owner(conn, desk: str) -> tuple[int, int] | None:
    if desk not in _desk_owner:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tenant_id, owner_user_id FROM sn_core.desks WHERE id = %s::uuid", (desk,))
                row = cur.fetchone()
        except psycopg2.Error:
            return None  # not a desk id; not cached, a bad id is not worth a slot
        _desk_owner[desk] = (row[0], row[1]) if row else None
    return _desk_owner[desk]


def _deliver(conn, payload: str) -> None:
    try:
        note = json.loads(payload)
    except ValueError:
        return
    if note.get("type") in WAKE_TYPES:
        for q in _wakers:
            try:
                q.put_nowait({"type": note.get("type")})
            except asyncio.QueueFull:
                pass  # a backlog of signals says nothing more than one
    desk = _desk_of(note)
    if not _subscribers:
        return
    # An agent's state (claimed, released, woken, a turn started or ended) and a
    # message arriving belong to no one chat, and every open page shows them
    # (the agents' rings, Working now, who waits). They go to every subscriber,
    # as an address only: what changed is read through the routes, with each
    # person's own rights, so nothing here widens what anyone can see. With them
    # the Studio needs no polls at all.
    if not desk:
        t = str(note.get("type") or "")
        if t.startswith("agent.") or t.startswith("message."):
            ev = {"id": note.get("id"), "type": t}
            if note.get("entity_type") == "agent":
                ev["agent"] = note.get("entity_id")
            for (tenant, _user), subs in _subscribers.items():
                if note.get("tenant") is not None and tenant != note.get("tenant"):
                    continue   # another tenant's agents are not this page's
                for q in subs:
                    try:
                        q.put_nowait(ev)
                    except asyncio.QueueFull:
                        pass
        return
    owner = _owner(conn, desk)
    for q in _subscribers.get(owner, ()) if owner else ():
        try:
            q.put_nowait({"id": note.get("id"), "type": note.get("type"), "desk": desk})
        except asyncio.QueueFull:
            pass  # a stalled reader misses events; its page reloads on reconnect


async def listen_loop() -> None:
    """Run for the life of the process; reconnect after a dropped connection."""
    loop = asyncio.get_running_loop()
    while True:
        conn = None
        try:
            conn = psycopg2.connect(dbname=DB_NAME, user="smartnode")
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {CHANNEL}")
            log.info("listening on %s", CHANNEL)
            ready = asyncio.Event()
            loop.add_reader(conn.fileno(), ready.set)
            try:
                while True:
                    await ready.wait()
                    ready.clear()
                    conn.poll()
                    while conn.notifies:
                        _deliver(conn, conn.notifies.pop(0).payload)
            finally:
                loop.remove_reader(conn.fileno())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- the stream must outlive a db restart
            log.warning("event listener stopped (%s); reconnecting", e)
            await asyncio.sleep(3)
        finally:
            if conn is not None:
                conn.close()
