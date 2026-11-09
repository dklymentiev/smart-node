"""Studio desks -- a desk is a user-owned CONTAINER of items (chats, documents,
images, ... that the user gathers into one workspace). This is the desk
container CRUD; the polymorphic contents (desk_item) and artifact storage land
in later phases.

  GET    /api/desks              list the caller's own active desks (newest first)
  GET    /api/desks/unread       what is new in each of them since it was last read
  POST   /api/desks              create a desk (owner = caller, tenant = caller)
  PATCH  /api/desks/{id}         rename a desk, open or close its Studio tab, mark it read
  DELETE /api/desks/{id}         delete one of the caller's own desks
  PUT    /api/desks/{id}/ensure  upsert a desk by known UUID (trusted-internal; brin)
  POST   /api/desks/{id}/mentions  @name in a chat: a message on the bus for that agent
  GET    /api/desks/{id}/waiting   colleagues still working on a mention from this chat

The /ensure endpoint is called by brin when a desk turn starts so that
conversations that pre-date the UI desk-create path appear in the listing.
It is trusted-internal only (loopback / docker0) and resolves the owner from
the studio_user body field rather than from the authenticated principal.
"""

import email.utils
import hashlib
import ipaddress
import logging
import os
import re
import shutil
import urllib.parse
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from psycopg2.extras import Json

import attachment_scan
from auth import DOCKER0_NET, Principal, get_principal
from db import cursor
from events_emit import emit
from permissions import requires

log = logging.getLogger("core-api.desks")

router = APIRouter(prefix="/api/desks", tags=["desks"])


def _trusted_caller(request: Request) -> bool:
    """True when the HTTP caller is on loopback or the docker0 network."""
    client_ip = (request.client.host if request.client else "") or ""
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return ip.is_loopback or ip in DOCKER0_NET


class DeskCreate(BaseModel):
    title: str = Field(default="Untitled desk", max_length=200)
    agent_slug: str | None = None


def _desk_object(request: Request) -> str:
    """The desk a route acts on, as the object of its permission."""
    return f"desk:{request.path_params['desk_id']}"


def _row(r) -> dict:
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "status": r["status"],
        "metadata": r["metadata"] or {},
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "message_count": (r.get("message_count") or 0),
        "last_message": r.get("last_message"),
        "last_message_at": r.get("last_message_at"),
    }


# desks.read is decided on the role axis only. Which desks a caller sees, and
# whose desk a caller may read by id, is each handler's own scope below.
@router.get("")
def list_desks(principal: Principal = Depends(requires("desks.read"))):
    """The caller's own active desks, newest first.

    The counts and the last message read the same sessions as the history: a
    session named "desk:<id>" or prefixed by its agent ("john:desk:<id>"). Only
    the first was matched, so every chat listed 0 messages and no last line,
    and New chat took a chat with messages for a blank one (tester, 2026-10-30)."""
    with cursor() as cur:
        # One pass over the desk sessions and their messages, then a join per desk. The count and
        # the last message used to be three subqueries per desk, each walking the trigram index:
        # 2.5 s for 400 desks, and New chat waits for this list. The desk id is read out of the
        # conversation key ("desk:<id>" or "<agent>:desk:<id>"), the same two forms as before.
        cur.execute(
            "WITH msgs AS ( "
            "  SELECT x.desk_id, x.tenant_id, m.created_at, m.seq, m.content "
            "  FROM (SELECT s.id, s.tenant_id, "
            "               substring(s.metadata->>'conversation' from '(?:^|:)desk:([0-9a-f-]{36})$') AS desk_id "
            "        FROM sn_core.ai_sessions s WHERE s.metadata->>'conversation' LIKE '%%desk:%%') x "
            "  JOIN sn_core.ai_session_messages m ON m.session_id = x.id AND m.role IN ('user','assistant') "
            "  WHERE x.desk_id IS NOT NULL), "
            "cnt AS (SELECT desk_id, tenant_id, count(*) AS message_count FROM msgs GROUP BY 1, 2), "
            "lastm AS (SELECT DISTINCT ON (desk_id, tenant_id) desk_id, tenant_id, "
            "                 left(content, 200) AS last_message, created_at AS last_message_at "
            "          FROM msgs ORDER BY desk_id, tenant_id, created_at DESC, seq DESC) "
            "SELECT d.id, d.title, d.status, d.metadata, d.created_at, d.updated_at, "
            "  COALESCE(c.message_count, 0) AS message_count, l.last_message, l.last_message_at "
            "FROM sn_core.desks d "
            "LEFT JOIN cnt c ON c.desk_id = d.id::text AND c.tenant_id = d.tenant_id "
            "LEFT JOIN lastm l ON l.desk_id = d.id::text AND l.tenant_id = d.tenant_id "
            "WHERE d.tenant_id = %s AND d.owner_user_id = %s AND d.status = 'active' "
            "ORDER BY d.updated_at DESC",
            (principal.tenant_id, principal.user_id),
        )
        rows = cur.fetchall()
    return {"desks": [_row(r) for r in rows]}


# What a desk shows from others, by time: the same three sources as /history
# (gateway answers under any of the desk's conversation keys, relays routed to
# the desk, and what an agent wrote to it on the bus), less what the Studio
# never draws: the action-claim guard's scaffolding and a deliberate NO_REPLY,
# bare or relayed. The owner's own turns are never new to the owner.
_SHOWN_FROM_OTHERS = (
    "SELECT m.created_at AS ts, m.content AS content "
    "  FROM sn_core.ai_session_messages m "
    "  JOIN sn_core.ai_sessions s ON s.id = m.session_id "
    "  WHERE (s.metadata->>'conversation' = 'desk:' || d.id::text "
    "         OR s.metadata->>'conversation' LIKE '%%:desk:' || d.id::text "
    "         OR s.metadata->>'user' = 'desk:' || d.id::text) "
    "    AND s.tenant_id = d.tenant_id AND m.role = 'assistant' "
    "UNION ALL "
    "SELECT msg.received_at, msg.content FROM sn_core.messages msg "
    "  JOIN sn_core.message_state mst ON mst.message_id = msg.id "
    "  WHERE msg.recipient = 'desk:' || d.id::text AND mst.status = 'processed' "
    "    AND msg.direction = 'inbound' AND msg.channel_type = 'internal' "
    "    AND msg.tenant_id = d.tenant_id "
    "UNION ALL "
    "SELECT msg.received_at, msg.content FROM sn_core.messages msg "
    "  WHERE msg.thread_id = 'desk:' || d.id::text AND msg.direction = 'outbound' "
    "    AND msg.channel_type = 'web-chat' "
    "    AND COALESCE(msg.metadata->>'source', '') <> 'web-chat' "
    "    AND msg.tenant_id = d.tenant_id"
)
_NOT_DRAWN = (
    "q.content !~ '^\\s*\\[SYSTEM CHECK\\]' "
    "AND q.content !~* '^\\s*(Answer from [A-Za-z0-9._-]+:\\s*)?\\W*NO_REPLY\\W*$'"
)


# Declared before the /{desk_id} routes, so "unread" is never read as an id.
@router.get("/unread")
def desks_unread(principal: Principal = Depends(requires("desks.read"))):
    """For each of the caller's active desks: how many messages from others came
    after metadata.read_upto, and the newest of them (when, and its start).

    A desk with no read_upto has never been read here, and a desk from before
    read tracking would otherwise show its whole history as new: it counts 0
    until it is first read (read_upto is null: the Studio marks it when shown).
    New desks start read (see create and ensure)."""
    with cursor() as cur:
        cur.execute(
            "SELECT d.id, COALESCE(a.n, 0) AS unread, a.last_at, a.last_text, "
            "       d.metadata->>'read_upto' AS read_upto "
            "FROM sn_core.desks d "
            "LEFT JOIN LATERAL ("
            "  SELECT count(*) FILTER (WHERE q.ts > (d.metadata->>'read_upto')::timestamptz) AS n, "
            "         max(q.ts) AS last_at, "
            "         (array_agg(left(q.content, 200) ORDER BY q.ts DESC))[1] AS last_text "
            f"  FROM ({_SHOWN_FROM_OTHERS}) q WHERE {_NOT_DRAWN}"
            ") a ON true "
            "WHERE d.tenant_id = %s AND d.owner_user_id = %s AND d.status = 'active'",
            (principal.tenant_id, principal.user_id),
        )
        rows = cur.fetchall()
    return {"desks": [
        {"id": str(r["id"]), "unread": int(r["unread"] or 0),
         "last_at": r["last_at"], "last_text": r["last_text"],
         "read_upto": r["read_upto"]}
        for r in rows]}


@router.post("", status_code=201, dependencies=[Depends(requires("desks.create"))])
def create_desk(payload: DeskCreate, principal: Principal = Depends(get_principal)):
    """Create a desk owned by the caller. Title defaults to 'Untitled desk'."""
    title = (payload.title or "").strip() or "Untitled desk"
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_core.desks (tenant_id, owner_user_id, title, metadata) "
            "VALUES (%s, %s, %s, jsonb_build_object('agent', %s::text, 'read_upto', now())) "
            "RETURNING id, title, status, metadata, created_at, updated_at",
            (principal.tenant_id, principal.user_id, title, payload.agent_slug),
        )
        r = cur.fetchone()
        emit(cur, actor=f"user:{principal.username}", event_type="desk.created",
             entity_type="desk", entity_id=str(r["id"]),
             payload={"title": title, "agent": payload.agent_slug})
    return _row(r)


@router.delete("/{desk_id}", status_code=204, dependencies=[Depends(requires("desks.delete", object=_desk_object))])
def delete_desk(desk_id: str, principal: Principal = Depends(get_principal)):
    """Delete one of the caller's own desks. 404 if it is not theirs."""
    with cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM sn_core.desks WHERE id = %s AND metadata->>'system' = 'true'", (desk_id,))
        if cur.fetchone():
            raise HTTPException(409, "the System chat cannot be deleted")
        cur.execute(
            "DELETE FROM sn_core.desks "
            "WHERE id = %s AND tenant_id = %s AND owner_user_id = %s",
            (desk_id, principal.tenant_id, principal.user_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"desk not found: {desk_id}")
        emit(cur, actor=f"user:{principal.username}", event_type="desk.deleted",
             entity_type="desk", entity_id=desk_id)
    # The attachment rows went with the desk (FK ON DELETE CASCADE);
    # the bytes go here, after the commit, so a failed delete never leaves rows
    # pointing at files that were already removed. A directory that will not go
    # is logged: the rows are gone either way, and the orphan is findable.
    try:
        shutil.rmtree(_desk_dir(desk_id))
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("desk %s deleted but its attachment directory stayed: %s", desk_id, e)
    return None


class DeskEnsure(BaseModel):
    studio_user: str | None = None   # username / email / 'owner' to resolve owner_user_id
    agent_slug: str | None = None    # agent that served this desk
    title: str | None = None         # first message or hint; defaults to 'Untitled chat'


@router.put("/{desk_id}/ensure", status_code=200, dependencies=[Depends(requires("desks.create"))])
def ensure_desk(desk_id: str, payload: DeskEnsure, request: Request):
    """Upsert a desk row by its known UUID.

    Called by brin at desk-turn start so conversations that pre-date the UI
    desk-create path appear in the chat listing. Trusted-internal only
    (loopback / docker0 network). The owner is resolved from payload.studio_user
    using the same fallback chain as record-webchat.

    Returns {created: bool, desk: <desk row>}.
    """
    if not _trusted_caller(request):
        raise HTTPException(403, "ensure is an internal endpoint")

    title = (payload.title or "").strip() or "Untitled chat"
    agent = (payload.agent_slug or "").strip() or None
    ident = (payload.studio_user or "").strip() or "owner"

    with cursor(commit=True) as cur:
        # Resolve owner: try handle / email / web-chat address; fall back to super-admin.
        cur.execute(
            "SELECT u.id, COALESCE(u.tenant_id, 1) AS tenant_id "
            "FROM sn_config.users u "
            "WHERE u.active AND u.archived_at IS NULL "
            "  AND (u.handle = %s OR u.email = %s "
            "       OR (u.role = 'super-admin' AND %s = 'owner')) "
            "ORDER BY u.id LIMIT 1",
            (ident.lower(), ident, ident),
        )
        user_row = cur.fetchone()
        if user_row is None:
            # last resort: super-admin
            cur.execute(
                "SELECT id, COALESCE(tenant_id, 1) AS tenant_id "
                "FROM sn_config.users WHERE role = 'super-admin' "
                "AND active AND archived_at IS NULL ORDER BY id LIMIT 1"
            )
            user_row = cur.fetchone()
        if user_row is None:
            raise HTTPException(500, "cannot resolve desk owner: no users in this node")

        owner_id = user_row["id"]
        tenant_id = user_row["tenant_id"]

        cur.execute(
            "INSERT INTO sn_core.desks (id, tenant_id, owner_user_id, title, metadata) "
            "VALUES (%s::uuid, %s, %s, %s, "
            "        CASE WHEN %s IS NOT NULL THEN jsonb_build_object('agent', %s) "
            "             ELSE '{}'::jsonb END || jsonb_build_object('read_upto', now())) "
            "ON CONFLICT (id) DO NOTHING "
            "RETURNING id, title, status, metadata, created_at, updated_at",
            (desk_id, tenant_id, owner_id, title, agent, agent),
        )
        r = cur.fetchone()
        if r is not None:
            # Newly inserted row.
            emit(cur, actor=f"user:{ident}", event_type="desk.created",
                 entity_type="desk", entity_id=desk_id,
                 payload={"title": title, "agent": agent, "via": "ensure"})
            log.info("desk.created (ensure): id=%s agent=%s user=%s", desk_id, agent, ident)
            return {"created": True, "desk": _row(r)}

        # Row already existed; fetch it for the caller.
        cur.execute(
            "SELECT id, title, status, metadata, created_at, updated_at "
            "FROM sn_core.desks WHERE id = %s::uuid",
            (desk_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            raise HTTPException(500, f"desk {desk_id} vanished between insert and select")
    return {"created": False, "desk": _row(existing)}


# A bus row that is the agent's answer to a reminder coming due: it replies to
# the reminder's message, itself or through the system relay that carried it
# into the chat. The chat marks it, so a reminder does not read as a stray text.
_ANSWERS_A_REMINDER = (
    "(msg.metadata->>'source' = 'reminder' OR "   # the reminder itself, written by the clock
    "EXISTS (SELECT 1 FROM sn_core.messages ans "
    "  JOIN sn_core.messages due ON due.id::text = ans.metadata->>'reply_to_message_id' "
    "  WHERE ans.id::text IN (msg.id::text, msg.metadata->>'relay_of') "
    "    AND due.metadata->>'source' = 'reminder')) AS reminder"
)


@router.get("/{desk_id}/history", dependencies=[Depends(requires("desks.read"))])
def desk_history(desk_id: str, principal: Principal = Depends(get_principal),
                 after: Annotated[datetime | None, Query()] = None):
    """Conversation history for a desk: user/assistant turns from the gateway
    sessions tagged with this desk (ai_sessions.metadata.conversation =
    'desk:<id>'), oldest first. Tenant-scoped. Empty list if the desk never
    chatted.

    `after` (a created_at the page already has) returns only what came later,
    so a page told by the live stream that its chat changed reads the new
    messages, not the whole chat again."""
    conv = f"desk:{desk_id}"
    # brin now keys the gateway session PER agent: metadata.conversation is
    # '<agent>:desk:<id>' (e.g. 'foreman:desk:...'), not the bare 'desk:<id>'
    # this used to match -- so a reload returned an empty thread over a session
    # that had every turn. Match the bare key, the agent-prefixed key,
    # and the legacy metadata.user.
    conv_suffix = f"%:{conv}"
    # Two sources, merged by time:
    #  (1) gateway chat turns. The desk conversation key is stored in
    #      metadata.conversation as 'desk:<id>' or '<agent>:desk:<id>', or (old)
    #      in metadata.user -- match any, so a page reload restores the thread.
    #  (2) proactive relays: a colleague's answer routed back to
    #      this desk as an internal message (recipient = desk:<id>). It lives
    #      on sn_core.messages, not in the gateway session, and web-chat has
    #      no live push -- so fold it in here as an assistant turn, both for
    #      reload and for the frontend poll to pick up.
    with cursor() as cur:
        cur.execute(
            "SELECT role, content, ts, partial, message_id, reminder, tools, from_system FROM ("
            "  SELECT m.role AS role, m.content AS content, m.id AS message_id, "
            "         m.created_at AS ts, m.seq AS seq, m.is_partial AS partial, false AS reminder, "
            "         m.tool_calls AS tools, false AS from_system "
            "  FROM sn_core.ai_session_messages m "
            "  JOIN sn_core.ai_sessions s ON s.id = m.session_id "
            "  WHERE (s.metadata->>'conversation' = %(conv)s "
            "         OR s.metadata->>'conversation' LIKE %(conv_suffix)s "
            "         OR s.metadata->>'user' = %(conv)s) "
            "    AND s.tenant_id = %(tenant)s "
            "    AND m.role IN ('user', 'assistant') "
            "  UNION ALL "
            "  SELECT 'assistant' AS role, msg.content AS content, NULL::bigint AS message_id, "
            "         msg.received_at AS ts, 2147483647 AS seq, false AS partial, " + _ANSWERS_A_REMINDER + ", "
            #  a reminder's own line is the system's; a relay is an agent's answer
            "         NULL::jsonb AS tools, msg.metadata->>'source' IN ('reminder', 'system-chat') AS from_system "
            "  FROM sn_core.messages msg "
            "  JOIN sn_core.message_state mst ON mst.message_id = msg.id "
            "  WHERE msg.recipient = %(conv)s "
            #  only what the delivery layer passed. A row in 'new' is
            #  intent not yet decided; 'blocked' was refused and never shows.
            "    AND mst.status = 'processed' "
            "    AND msg.direction = 'inbound' "
            "    AND msg.channel_type = 'internal' "
            "    AND msg.tenant_id = %(tenant)s "
            #  (3) what the agent wrote TO this desk on the bus instead of in a
            #      gateway turn: an inbox_reply to a web-chat message, or
            #      anything else it originated while nobody was mid-sentence.
            #      Those rows carry the desk in thread_id and had no reader at
            #      all, so an answer the agent had already composed was
            #      recorded and never shown, and the person re-asked.
            #      brin's own transcript of the synchronous exchange is
            #      excluded by metadata.source = 'web-chat': those turns are
            #      already in (1) and would otherwise appear twice.
            "  UNION ALL "
            "  SELECT 'assistant' AS role, msg.content AS content, NULL::bigint AS message_id, "
            "         msg.received_at AS ts, 2147483647 AS seq, false AS partial, " + _ANSWERS_A_REMINDER + ", "
            "         NULL::jsonb AS tools, false AS from_system "
            "  FROM sn_core.messages msg "
            "  WHERE msg.thread_id = %(conv)s "
            "    AND msg.direction = 'outbound' "
            "    AND msg.channel_type = 'web-chat' "
            "    AND COALESCE(msg.metadata->>'source', '') <> 'web-chat' "
            "    AND msg.tenant_id = %(tenant)s "
            #  (4) a reply the person sent to ONE colleague in this chat
            #      (POST /mentions with direct): no gateway turn carries it,
            #      so its bus row is the person's turn.
            "  UNION ALL "
            "  SELECT 'user' AS role, msg.metadata->>'text' AS content, NULL::bigint AS message_id, "
            "         msg.received_at AS ts, 0 AS seq, false AS partial, false AS reminder, NULL::jsonb AS tools, false AS from_system "
            "  FROM sn_core.messages msg "
            "  WHERE msg.metadata->>'kind' = 'chat' "
            "    AND msg.metadata->>'direct' = 'true' "
            "    AND msg.metadata->>'text' IS NOT NULL "
            "    AND msg.metadata->'origin'->>'conversation' = %(conv)s "
            "    AND msg.tenant_id = %(tenant)s "
            ") q "
            "WHERE %(after)s::timestamptz IS NULL OR ts > %(after)s::timestamptz "
            "ORDER BY ts ASC, seq ASC",
            {"conv": conv, "conv_suffix": conv_suffix, "tenant": principal.tenant_id,
             "after": after},
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        content = r["content"] or ""
        # Internal scaffolding -- the action-claim guard re-prompt and its echo
        # are bus mechanics, never meant for the person in the chat.
        if content.startswith("[SYSTEM CHECK]"):
            continue
        if content.startswith("[MEMORY CONTEXT]"):
            # legacy: older turns concatenated a memory block before the message
            sep = content.rfind("\n\n")
            content = content[sep + 2:] if sep != -1 else content
        item = {"role": r["role"], "content": content, "created_at": r["ts"]}
        if r.get("message_id") is not None:
            item["message_id"] = r["message_id"]
        # A reply cut short by Stop: the Studio marks it, after a reload too.
        if r["partial"]:
            item["stopped"] = True
        if r["reminder"]:
            item["reminder"] = True
        if r["from_system"]:
            item["system"] = True
        # The tools the agent really called this turn, by name; the chat words them
        # from the tools registry. What it did, next to what it said.
        if r["role"] == "assistant" and isinstance(r["tools"], list):
            names = [t.get("name") for t in r["tools"] if isinstance(t, dict) and t.get("name")]
            if names:
                item["tools"] = names
        out.append(item)
    return {"desk_id": desk_id, "messages": out}


class DeskMention(BaseModel):
    agent: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20000)
    typed: str | None = Field(default=None, max_length=80)
    # Sent to the named colleagues ONLY: a reply to one of that agent's messages,
    # or a message that starts with @colleague. The chat's own agent never
    # receives it, and the chat shows it as the person's turn from this row
    # (desk_history), since no gateway turn carries it.
    direct: bool = False
    # Whether this row is that turn. One message to several colleagues is one
    # turn: the Studio marks the first row only (turn=false on the others).
    # Default: the same as direct.
    turn: bool | None = None


#: Turns of the chat a mention carries, and how much of each.
_CARD_TURNS = 6
_CARD_TURN_CHARS = 300


def _chat_card(desk, person, mentioned, turns):
    """What a mentioned colleague needs to know about the chat it was called
    into: who is in it, how long it is, and its latest turns. Carried in the
    mention itself, as the chat stood when the person wrote: given only the
    message, "@otto what do you think?" told the colleague nothing, and it had to
    guess whether to read the history."""
    relay = re.compile(r"^\s*Answer from ([A-Za-z0-9][A-Za-z0-9._-]{0,63}):\s*")
    chat_agent = desk["agent"] or "no agent"
    people = [person, chat_agent]
    lines = []
    for t in turns:
        content = t.get("content") or ""
        m = relay.match(content) if t.get("role") == "assistant" else None
        who = person if t.get("role") == "user" else (m.group(1).lower() if m else chat_agent)
        if who not in people:
            people.append(who)
        lines.append((who, content[m.end():] if m else content))
    if mentioned not in people:
        people.append(mentioned)
    when = ""
    if turns:
        first, last = turns[0].get("created_at"), turns[-1].get("created_at")
        fmt = lambda x: x.strftime("%Y-%m-%d %H:%M") if hasattr(x, "strftime") else str(x)[:16]
        when = ", from %s to %s UTC" % (fmt(first), fmt(last))
    out = ["The chat \"%s\": %s's chat with %s." % ((desk["title"] or "untitled").strip(), person, chat_agent),
           "In it: %s." % ", ".join(people),
           "%d messages%s." % (len(turns), when)]
    if lines:
        out.append("Its latest turns, oldest first:")
        for who, content in lines[-_CARD_TURNS:]:
            one = " ".join(content.split())
            if len(one) > _CARD_TURN_CHARS:
                one = one[:_CARD_TURN_CHARS] + "..."
            out.append("  %s: %s" % (who, one))
    return "\n".join(out)


@router.post("/{desk_id}/mentions", status_code=201)
def mention_in_desk(desk_id: str, payload: DeskMention,
                    principal: Principal = Depends(requires("messages.send"))):
    """@name written in a chat: one row on the message bus for the named agent.

    The same primitive as a mention in a task comment (sender, recipient, internal
    channel), with metadata.kind = 'chat' and an origin naming this desk. The agent
    is woken by the Runner like for any other message and answers with
    inbox_reply; the reply path relays that answer into this desk as "Answer from
    <agent>". The chat's own agent is not asked to relay anything.

    Only the desk's owner may mention in it: the mention speaks with the owner's
    name, and the answer lands in the owner's chat. Anyone else gets the same 404
    as for a desk that does not exist. The chat's own agent cannot be mentioned
    (409): it already reads every message of its chat. Which @name in a message
    counts is Studio's decision (the first one outside quotes and code); this
    endpoint takes the one it is given and checks it names an active agent.

    direct=True: the Studio sends the text to the named colleagues only (a reply
    to one of this agent's messages, or a message that starts with @colleague),
    so this row is also the person's turn in the chat (desk_history reads it back
    as role 'user'), unless turn=false says another row of the same message is.

    Audit: metadata.kind = 'chat' AND metadata.reason = 'mentioned' on
    sn_core.messages is the cross-agent mention log.
    """
    desk_id = _as_uuid(desk_id, "desk")
    slug = payload.agent.strip().lower()
    with cursor(commit=True) as cur:
        # The seed-state trigger on sn_core.messages INSERTs into message_state
        # unqualified -> this connection needs sn_core on its search_path.
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "SELECT id, tenant_id, owner_user_id, title, metadata->>'agent' AS agent "
            "FROM sn_core.desks WHERE id = %s",
            (desk_id,),
        )
        d = cur.fetchone()
        if (d is None or d["tenant_id"] != principal.tenant_id
                or d["owner_user_id"] != principal.user_id):
            raise HTTPException(404, f"desk not found: {desk_id}")
        if slug == (d["agent"] or "").lower():
            raise HTTPException(409, f"'{slug}' is this chat's own agent")
        cur.execute(
            "SELECT slug FROM sn_config.agents "
            "WHERE lower(slug) = %s AND enabled AND archived_at IS NULL "
            "  AND (tenant_id IS NULL OR tenant_id = %s)",
            (slug, d["tenant_id"]),
        )
        a = cur.fetchone()
        if a is None:
            raise HTTPException(404, f"no active agent '{slug}'")
        agent = a["slug"]
        conv = f"desk:{desk_id}"
        typed = (payload.typed or "@" + slug).strip()
        text = (
            ("%s wrote to you (%s) in the Studio chat \"%s\" (%s, the chat of %s). "
             "It was sent to you only; the chat's agent did not receive it. "
             "Their message:\n\n%s\n\n") if payload.direct else
            ("%s mentioned you (%s) in the Studio chat \"%s\" (%s, the chat of %s). "
             "Their message:\n\n%s\n\n")) + (
            "%s\n\n"
            "If you need more of the conversation than this, call chat_history with this "
            "message's id: it returns the latest turns of that chat, and before=<index> "
            "gives older ones. Answer with inbox_reply on this message; the node posts "
            "your answer into that chat as \"Answer from %s\". Do not write into the "
            "chat any other way.")
        card = _chat_card(d, principal.username, agent, desk_history(desk_id, principal)["messages"])
        text = text % (principal.username, typed, (d["title"] or "untitled").strip(), conv,
               d["agent"] or "no agent", payload.content.strip(), card, agent)
        ext = email.utils.make_msgid(
            domain=os.environ.get("SMART_NODE_DOMAIN") or "smart-node.local")
        cur.execute(
            "INSERT INTO sn_core.messages "
            "  (channel_type, channel_id, external_id, thread_id, direction, sender, "
            "   recipient, content, content_type, metadata, tenant_id) "
            "VALUES ('internal', %s, %s, %s, 'inbound', %s, %s, %s, 'text/plain', %s, %s) "
            "RETURNING id",
            (principal.username, ext, ext, principal.username, agent, text,
             Json({"kind": "chat", "reason": "mentioned", "source": "studio-chat",
                   "desk_id": desk_id, "desk_agent": d["agent"],
                   "studio_user": principal.username, "typed": typed,
                   "direct": bool(payload.direct),
                   "text": (payload.content.strip() if payload.direct
                            and payload.turn is not False else None),
                   "origin": {"channel": "internal", "conversation": conv}}),
             d["tenant_id"]),
        )
        mid = cur.fetchone()["id"]
        emit(cur, actor=principal.actor_id or f"user:{principal.username}",
             event_type="message.inbound", entity_type="message", entity_id=str(mid),
             payload={"from": principal.username, "to": agent, "channel": "internal",
                      "kind": "chat", "reason": "mentioned", "desk": conv,
                      "direct": bool(payload.direct),
                      "preview": payload.content.strip()[:120]})
    log.info("desk %s: %s mentioned %s -> message %s", desk_id, principal.username, agent, mid)
    return {"message_id": mid, "agent": agent, "desk_id": desk_id}


#: A mention older than this is not shown as "being worked on": an agent that
#: never answered is a fact for the inbox and the journal, not a spinner.
_WAITING_WINDOW = "1 day"


@router.get("/{desk_id}/waiting", dependencies=[Depends(requires("desks.read"))])
def desk_waiting(desk_id: str, principal: Principal = Depends(get_principal)):
    """The colleagues a question from this chat is still waiting on.

    A chat mention (POST /mentions) is answered outside the chat's own turn, so
    the chat has no turn to show while the colleague works. This is that state,
    read from the bus: every mention from this desk whose row is not yet closed
    (the agent closes it by answering or dismissing it), newest first, one per
    agent, with what the agent is doing:

      working  the agent is busy and took that work after the mention arrived
      busy     the agent is busy with something it took before the mention
      queued   the agent is free and has not been woken for it yet

    The desk owner and the whole-node seats read it; anyone else gets the
    desk-not-found 404.
    """
    desk_id = _as_uuid(desk_id, "desk")
    with cursor() as cur:
        cur.execute("SELECT tenant_id, owner_user_id FROM sn_core.desks WHERE id = %s",
                    (desk_id,))
        d = cur.fetchone()
        if d is None or not (
                principal.role in _WHOLE_NODE_SEATS
                or (d["tenant_id"] == principal.tenant_id
                    and d["owner_user_id"] == principal.user_id)):
            raise HTTPException(404, f"desk not found: {desk_id}")
        cur.execute(
            "SELECT DISTINCT ON (m.recipient) m.id, m.recipient AS agent, m.received_at, "
            "       r.status AS runtime, r.updated_at AS runtime_since "
            "FROM sn_core.messages m "
            "JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "LEFT JOIN sn_core.agent_runtime r ON r.agent_slug = m.recipient "
            "WHERE m.metadata->>'kind' = 'chat' AND m.metadata->>'reason' = 'mentioned' "
            "  AND m.metadata->'origin'->>'conversation' = %s "
            "  AND ms.status IN ('new', 'routed', 'awaiting_reply') "
            "  AND m.received_at > now() - interval '" + _WAITING_WINDOW + "' "
            "  AND m.tenant_id = %s "
            "ORDER BY m.recipient, m.id DESC",
            (f"desk:{desk_id}", d["tenant_id"]),
        )
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        busy = r["runtime"] == "busy"
        took_it = busy and r["runtime_since"] is not None and r["runtime_since"] >= r["received_at"]
        out.append({
            "agent": r["agent"], "message_id": r["id"], "since": r["received_at"],
            "state": "working" if took_it else ("busy" if busy else "queued"),
            "working_since": r["runtime_since"] if took_it else None,
        })
    out.sort(key=lambda x: x["since"])
    return {"desk_id": desk_id, "waiting": out}


@router.get("/{desk_id}/sessions", dependencies=[Depends(requires("desks.read"))])
def desk_sessions(desk_id: str, principal: Principal = Depends(get_principal)):
    """Gateway sessions backing this desk (for inspection / sharing).

    Matches the same keys as /history: the bare 'desk:<id>' and the
    agent-prefixed '<agent>:desk:<id>' brin has written now. This one
    was left on the bare key, so it answered [] for every current desk, and the
    Studio Stop and Reload that read sessions[0] acted on nothing."""
    conv = f"desk:{desk_id}"
    with cursor() as cur:
        cur.execute(
            "SELECT s.id, a.slug AS agent, s.model, s.provider, s.is_opaque, s.status, "
            "s.created_at, s.last_activity_at, "
            "(SELECT count(*) FROM sn_core.ai_session_messages m WHERE m.session_id = s.id) AS messages "
            "FROM sn_core.ai_sessions s "
            "LEFT JOIN sn_config.agents a ON a.id = s.agent_id "
            "WHERE (s.metadata->>'conversation' = %s OR s.metadata->>'conversation' LIKE %s) "
            "  AND s.tenant_id = %s "
            "ORDER BY s.last_activity_at DESC NULLS LAST, s.created_at DESC",
            (conv, f"%:{conv}", principal.tenant_id),
        )
        rows = cur.fetchall() or []
    return {"desk_id": desk_id, "sessions": [
        {"id": str(r["id"]), "agent": r["agent"], "model": r["model"],
         "provider": r["provider"], "is_opaque": r["is_opaque"], "status": r["status"],
         "messages": r["messages"], "last_activity_at": r["last_activity_at"]}
        for r in rows]}


class DeskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    # whether the desk has a tab open in the Studio. Closing a tab is a view
    # state of the desk (metadata, like view_mode), NOT its removal: the owner ruled
    # that a chat is never deleted from the Studio and stays in the agent's history,
    # where the Studio lists it and can open its tab again.
    tab_open: bool | None = None
    # the desk is read up to this moment: the time of the newest message the
    # Studio has shown. A time, not "now", so an answer that lands while the mark
    # is on its way stays new. Never moves back, and never past the server's now.
    read_upto: datetime | None = None


@router.patch("/{desk_id}", dependencies=[Depends(requires("desks.update", object=_desk_object))])
def update_desk(desk_id: str, payload: DeskUpdate, principal: Principal = Depends(get_principal)):
    """Rename one of the caller's own desks, open or close its Studio tab, and/or
    mark it read up to a moment."""
    title = None
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(400, "title must not be blank")
    if title is None and payload.tab_open is None and payload.read_upto is None:
        raise HTTPException(400, "nothing to update: send title, tab_open and/or read_upto")
    with cursor(commit=True) as cur:
        # metadata is merged, never replaced: it also carries the desk's agent.
        cur.execute(
            "UPDATE sn_core.desks SET title = COALESCE(%s, title), "
            "  metadata = CASE WHEN %s::boolean IS NULL THEN metadata "
            "                  ELSE metadata || jsonb_build_object('tab_open', %s::boolean) END "
            "WHERE id = %s AND tenant_id = %s AND owner_user_id = %s "
            "RETURNING id, title, status, metadata, created_at, updated_at",
            (title, payload.tab_open, payload.tab_open, desk_id, principal.tenant_id, principal.user_id),
        )
        if payload.read_upto is not None:
            cur.execute(
                "UPDATE sn_core.desks SET metadata = metadata || jsonb_build_object('read_upto', "
                "  GREATEST(LEAST(%s::timestamptz, now()), "
                "           COALESCE((metadata->>'read_upto')::timestamptz, '-infinity'))) "
                "WHERE id = %s AND tenant_id = %s AND owner_user_id = %s "
                "RETURNING id, title, status, metadata, created_at, updated_at",
                (payload.read_upto, desk_id, principal.tenant_id, principal.user_id),
            )
        r = cur.fetchone()
        # A rename or a closed/reopened tab is news to the owner's other pages
        # (the live stream, event_stream.py); a moved read mark is not.
        if r and (title is not None or payload.tab_open is not None):
            emit(cur, actor=f"user:{principal.username}", event_type="desk.updated",
                 entity_type="desk", entity_id=str(r["id"]),
                 payload={k: v for k, v in (("title", title), ("tab_open", payload.tab_open)) if v is not None})
    if not r:
        raise HTTPException(404, f"desk not found: {desk_id}")
    return _row(r)


# ---------------------------------------------------------------------------
# A desk's attachments.
#
# The node's ONE attachment store, with the desk as the parent: a
# desk chat lives in gateway sessions, so it has no sn_core.messages row to
# hang a file from. The owner asked that a chat's files stay together and do
# not become a heap, and that cleanup is not left to memory: they live under
# desks/<desk_id>/ and leave with the desk. Limits are node configuration
#; these defaults are that ADR's per-file 25 MB, plus a
# per-desk ceiling.
# ---------------------------------------------------------------------------
_ATTACHMENT_DIR = os.environ.get("ATTACHMENT_DIR", "/srv/data/attachments")
_DESK_MAX_FILE = int(os.environ.get("SN_DESK_ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)))
_DESK_MAX_TOTAL = int(os.environ.get("SN_DESK_ATTACHMENTS_MAX_BYTES", str(500 * 1024 * 1024)))
_WHOLE_NODE_SEATS = ("super-admin", "operator")
# Rendered in the browser (thumbnails); everything else is served as a download.
# The type is the one the bytes proved at upload, never the declared one.
_INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def _desk_dir(desk_id: str) -> str:
    """The desk's directory. desk_id is parsed as a uuid before it touches a
    path, so no caller-supplied string is ever joined onto the store."""
    return os.path.join(_ATTACHMENT_DIR, "desks", str(uuid.UUID(str(desk_id))))


def _as_uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise HTTPException(404, f"{what} not found: {value}")


def _desk_for(cur, desk_id: str, principal: Principal, write: bool):
    """The desk, if this principal may read its files (write=True: add or remove
    them). Only the owner writes. Reading adds the desk's own agent and the
    whole-node seats; the agent reaches this through the earlier change service proof.
    Anyone else gets the same 404 as for a desk that does not exist."""
    desk_id = _as_uuid(desk_id, "desk")
    cur.execute(
        "SELECT id, tenant_id, owner_user_id, metadata->>'agent' AS agent "
        "FROM sn_core.desks WHERE id = %s",
        (desk_id,),
    )
    d = cur.fetchone()
    if d is None:
        raise HTTPException(404, f"desk not found: {desk_id}")
    same_tenant = d["tenant_id"] == principal.tenant_id
    owner = same_tenant and d["owner_user_id"] == principal.user_id
    if write:
        allowed = owner
    else:
        allowed = (owner or principal.role in _WHOLE_NODE_SEATS
                   or (same_tenant and d["agent"] and d["agent"] == principal.username))
    if not allowed:
        raise HTTPException(404, f"desk not found: {desk_id}")
    return d


def _attachment_row(r) -> dict:
    return {
        "id": str(r["id"]),
        "filename": r["filename"],
        "content_type": r["content_type"],
        "size_bytes": r["size_bytes"],
        "status": r["status"],
        "scan_reason": r["scan_reason"],
        "created_at": r["created_at"],
    }


@router.post("/{desk_id}/attachments", status_code=201,
             dependencies=[Depends(requires("desks.attachments.write", object=_desk_object))])
async def upload_desk_attachment(desk_id: str, request: Request,
                                 principal: Principal = Depends(get_principal)):
    """Store one file the owner attached in the Studio chat.

    The body is the file's raw bytes; its name arrives percent-encoded in
    X-Filename and its declared type in Content-Type. The name is for display
    only: on disk the file is desks/<desk_id>/<uuid>.<type suffix>. The suffix
    comes from the proved/normalized content type, never from the display name.
    The scan stage
    (attachment_scan) runs before the row is written; in phase 1 it approves
    the owner's own upload and records why.
    """
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > _DESK_MAX_FILE:
        raise HTTPException(413, f"file too large: {len(data)} bytes, the limit is {_DESK_MAX_FILE}")
    name = attachment_scan.safe_filename(urllib.parse.unquote(request.headers.get("x-filename", "")))
    declared = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    proved = attachment_scan.sniff_image_type(data[:16])
    # An image is served as the type its bytes prove. A file that claims to be an
    # image and is not stays opaque bytes: it is never rendered in a browser.
    if proved:
        ctype = proved
    elif declared.startswith("image/") or not declared:
        ctype = "application/octet-stream"
    else:
        ctype = declared
    status, reason = attachment_scan.verdict(attachment_scan.DESK_OWNER, True)
    aid = str(uuid.uuid4())
    digest = hashlib.sha256(data).hexdigest()

    with cursor(commit=True) as cur:
        d = _desk_for(cur, desk_id, principal, write=True)
        did = str(d["id"])
        cur.execute(
            "SELECT COALESCE(sum(size_bytes), 0) AS total "
            "FROM sn_core.message_attachments WHERE desk_id = %s",
            (did,),
        )
        total = cur.fetchone()["total"]
        if total + len(data) > _DESK_MAX_TOTAL:
            raise HTTPException(413, f"this chat already holds {total} bytes of files; "
                                     f"the limit per chat is {_DESK_MAX_TOTAL}")
        ddir = _desk_dir(did)
        os.makedirs(ddir, mode=0o750, exist_ok=True)
        rel = os.path.join("desks", did, aid + attachment_scan.storage_suffix(ctype))
        path = os.path.join(_ATTACHMENT_DIR, rel)
        # Temp then rename: a crash mid-write leaves a .part, never a short file
        # under a name a row points at.
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        try:
            cur.execute(
                "INSERT INTO sn_core.message_attachments "
                "  (id, desk_id, tenant_id, channel_type, filename, content_type, size_bytes, "
                "   content_hash, storage_path, status, scan_reason, received_at) "
                "VALUES (%s, %s, %s, 'web-chat', %s, %s, %s, %s, %s, %s, %s, now()) "
                "RETURNING id, filename, content_type, size_bytes, status, scan_reason, created_at",
                (aid, did, d["tenant_id"], name, ctype, len(data), digest, rel, status, reason),
            )
            r = cur.fetchone()
            emit(cur, actor=f"user:{principal.username}", event_type="attachment.stored",
                 entity_type="attachment", entity_id=aid,
                 payload={"desk": did, "filename": name, "content_type": ctype,
                          "size_bytes": len(data), "status": status, "reason": reason})
        except Exception:
            # The bytes reached disk before the row. A row that failed must not
            # leave a file nothing points at.
            try:
                os.remove(path)
            except OSError:
                pass
            raise
    return _attachment_row(r)


@router.get("/{desk_id}/attachments", dependencies=[Depends(requires("desks.read"))])
def list_desk_attachments(desk_id: str, principal: Principal = Depends(get_principal)):
    """The files attached in this desk, oldest first."""
    with cursor() as cur:
        d = _desk_for(cur, desk_id, principal, write=False)
        cur.execute(
            "SELECT id, filename, content_type, size_bytes, status, scan_reason, created_at "
            "FROM sn_core.message_attachments WHERE desk_id = %s ORDER BY created_at",
            (str(d["id"]),),
        )
        rows = cur.fetchall() or []
    return {"desk_id": str(d["id"]), "attachments": [_attachment_row(r) for r in rows]}


@router.get("/{desk_id}/attachments/{attachment_id}/content",
            dependencies=[Depends(requires("desks.read"))])
def desk_attachment_content(desk_id: str, attachment_id: str,
                            principal: Principal = Depends(get_principal)):
    """The file's bytes. Only an approved file is served.
    A proved image renders inline, for the thumbnails in the thread; anything
    else is a download, so no uploaded file ever runs as a page."""
    aid = _as_uuid(attachment_id, "attachment")
    with cursor() as cur:
        d = _desk_for(cur, desk_id, principal, write=False)
        cur.execute(
            "SELECT filename, content_type, storage_path, status "
            "FROM sn_core.message_attachments WHERE id = %s AND desk_id = %s",
            (aid, str(d["id"])),
        )
        r = cur.fetchone()
    if r is None:
        raise HTTPException(404, f"attachment not found: {aid}")
    if r["status"] != attachment_scan.APPROVED:
        raise HTTPException(403, f"attachment is {r['status']}, not approved")
    path = os.path.join(_ATTACHMENT_DIR, r["storage_path"] or "")
    if not r["storage_path"] or not os.path.isfile(path):
        raise HTTPException(404, "attachment bytes are not on disk")
    ctype = r["content_type"] or "application/octet-stream"
    disposition = "inline" if ctype in _INLINE_TYPES else "attachment"
    fname = urllib.parse.quote(r["filename"] or "file")
    return FileResponse(path, media_type=ctype, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{fname}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=86400",
    })


@router.delete("/{desk_id}/attachments/{attachment_id}", status_code=204,
               dependencies=[Depends(requires("desks.attachments.delete", object=_desk_object))])
def delete_desk_attachment(desk_id: str, attachment_id: str,
                           principal: Principal = Depends(get_principal)):
    """Remove a file: the x on a chip before sending, or later. Owner only.
    The row goes in the transaction, the bytes right after it."""
    aid = _as_uuid(attachment_id, "attachment")
    with cursor(commit=True) as cur:
        d = _desk_for(cur, desk_id, principal, write=True)
        cur.execute(
            "DELETE FROM sn_core.message_attachments WHERE id = %s AND desk_id = %s "
            "RETURNING storage_path, filename",
            (aid, str(d["id"])),
        )
        r = cur.fetchone()
        if r is None:
            raise HTTPException(404, f"attachment not found: {aid}")
        emit(cur, actor=f"user:{principal.username}", event_type="attachment.deleted",
             entity_type="attachment", entity_id=aid,
             payload={"desk": str(d["id"]), "filename": r["filename"]})
    try:
        os.remove(os.path.join(_ATTACHMENT_DIR, r["storage_path"]))
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("attachment %s row deleted but its file stayed: %s", aid, e)
    return None
