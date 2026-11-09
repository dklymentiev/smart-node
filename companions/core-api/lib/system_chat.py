"""The System chat: where the node tells its owner what needs them.

An ordinary chat with no agent. A message here is an internal message from the
system addressed to that chat, delivered like any other row to desk:<id>; nothing
here delivers. The chat rises by its new messages like any chat.

Only what needs the person, and only what has nowhere better to go. The key names
the thing: the same one does not repeat until the chat has been read.

Most of what it says it reads from the event journal, so whoever has something to
report emits an event and knows nothing of this chat. A new kind is one line in
_TOLD. Services are read from systemd: they cannot report their own death.
"""

import asyncio
import email.utils
import logging
import subprocess

from psycopg2.extras import Json

from db import cursor

log = logging.getLogger("core-api.system-chat")


def _owner_desk(cur, tenant_id):
    cur.execute("SELECT id FROM sn_config.users WHERE role = 'super-admin' AND active AND archived_at IS NULL "
                "ORDER BY id LIMIT 1")
    owner = cur.fetchone()
    if owner is None:
        return None
    # The unique index makes two at once one chat. read_upto before now(): the first
    # message is written in this transaction, at this same now(), and must count as new.
    cur.execute("INSERT INTO sn_core.desks (tenant_id, owner_user_id, title, metadata) "
                "VALUES (%s, %s, 'System', jsonb_build_object('system', true, 'read_upto', now() - interval '1 second')) "
                "ON CONFLICT (owner_user_id) WHERE metadata->>'system' = 'true' DO NOTHING", (tenant_id, owner["id"]))
    cur.execute("SELECT id, metadata->>'read_upto' AS read_upto FROM sn_core.desks "
                "WHERE owner_user_id = %s AND metadata->>'system' = 'true'", (owner["id"],))
    return cur.fetchone()


def tell_owner(cur, tenant_id, text, key) -> bool:
    """Put `text` in the owner's System chat. False when the same `key` is still
    unread there, or when the node has no owner yet."""
    # The seed-state trigger on sn_core.messages INSERTs into message_state unqualified.
    cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
    desk = _owner_desk(cur, tenant_id)
    if desk is None:
        return False
    conv = f"desk:{desk['id']}"
    cur.execute("SELECT 1 FROM sn_core.messages WHERE recipient = %s AND metadata->>'key' = %s "
                "AND received_at > %s::timestamptz LIMIT 1", (conv, key, desk["read_upto"]))
    if cur.fetchone():
        return False
    ext = email.utils.make_msgid(domain="system.local")
    cur.execute("INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, "
                "recipient, content, content_type, metadata, tenant_id) "
                "VALUES ('internal', 'system', %s, %s, 'inbound', 'system', %s, %s, 'text/plain', %s, %s)",
                (ext, conv, conv, text, Json({"source": "system-chat", "key": key}), tenant_id))
    # The list orders by this for a chat with no agent's turns; a closed tab reopens.
    cur.execute("UPDATE sn_core.desks SET updated_at = now(), metadata = metadata - 'tab_open' WHERE id = %s", (desk["id"],))
    return True


def _failed_send(e):
    p = e["payload"]
    return (f"A message by {p.get('channel', '')} was not delivered: {p.get('error', 'no reason given')}",
            f"failed:{p.get('channel', '')}")


def _in_own_words(e):
    return e["payload"].get("text") or e["event_type"], f"{e['event_type']}:{e['payload'].get('period', '')}"


# event type -> (text, key)
_TOLD = {
    "message.failed": _failed_send,
    "budget.exhausted": _in_own_words,
    "budget.warning": _in_own_words,
}


def read_events():
    """Tell what the journal gained since the last pass. The first pass on a node
    only takes its place: what happened before this chat existed is not news."""
    with cursor(commit=True) as cur:
        cur.execute("SELECT event_id FROM sn_core.system_chat_watch FOR UPDATE")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO sn_core.system_chat_watch (event_id) "
                        "SELECT COALESCE(max(id), 0) FROM sn_core.events")
            return
        cur.execute("SELECT id, event_type, entity_id, payload, tenant_id FROM sn_core.events "
                    "WHERE id > %s AND event_type = ANY(%s) ORDER BY id LIMIT 200",
                    (row["event_id"], list(_TOLD)))
        events = cur.fetchall()
        for e in events:
            text, key = _TOLD[e["event_type"]](e)
            tell_owner(cur, e["tenant_id"], text, key)
        if events:
            cur.execute("UPDATE sn_core.system_chat_watch SET event_id = %s", (events[-1]["id"],))


def _failed_units() -> set:
    """Units systemd says failed. Stopped on purpose (the panel's switches) is
    `inactive`, not this."""
    out = subprocess.run(["systemctl", "list-units", "smart-node*", "--all", "--plain", "--no-legend", "--state=failed"],
                         capture_output=True, text=True, timeout=10).stdout
    return {line.split()[0] for line in out.splitlines() if line.strip()}


_down = set()   # told, and still down: said once however long it lasts


def read_units():
    failed = _failed_units()
    for unit in sorted(failed - _down):
        with cursor(commit=True) as cur:
            tell_owner(cur, 1, f"The service {unit} has failed.", f"service:{unit}")
    _down.clear()
    _down.update(failed)


async def watch_loop(interval: float = 30.0):
    while True:
        for read in (read_events, read_units):
            try:
                await asyncio.to_thread(read)
            except Exception:
                log.exception("system chat: %s failed", read.__name__)
        await asyncio.sleep(interval)
