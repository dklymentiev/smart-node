"""Messages log -- live read of sn_core.messages (adapter-ingested comms).

Replaces the static /data/messages.json snapshot. Tenant-scoped: a cross-tenant
principal (super-admin) sees every tenant; everyone else only their own.

Email-specific fields (subject / in_reply_to / from_name) live in the message
metadata jsonb and are surfaced flat for the panel.

Write side: a consumer (the secretary) never sends -- it composes a reply and
POSTs it to /reply, which records an OUTBOUND row threaded against the original
and marks the inbound message processed. The communication outbound-pass drains
those outbound rows and transmits them. The secretary touches only this table.
"""

import email.utils
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from psycopg2.extras import Json

from auth import Principal, get_principal
from db import cursor
from events_emit import emit
from lib.system_chat import tell_owner
from lib.channel_routing import channel_receivers, block_ambiguous_inbound
from message_identity import stamp as stamp_identity
from permissions import requires

log = logging.getLogger("core-api.messages")

router = APIRouter(prefix="/api/messages", tags=["messages"])

# Every state the work queue can hold. The single list in this file: filtering
# and marking are different rights over the same set, and keeping two literals
# is how 'blocked' became filterable nowhere while dispatch was writing it.
_QUEUE_STATES = ("new", "routed", "processed", "failed", "blocked", "history",
                 "awaiting_reply")


def _reply_obligation_config():
    """(wake window in seconds, max attempts) for reply obligations, or None.

    Both come from the node's environment and neither has a default: a node
    that has not set them has the feature off, and a send that asks for a
    reply is refused with the reason rather than accepted and never enforced.
    """
    window = os.environ.get("SN_REPLY_WAKE_WINDOW_SECONDS", "").strip()
    attempts = os.environ.get("SN_REPLY_MAX_ATTEMPTS", "").strip()
    if not window or not attempts:
        return None
    return int(window), int(attempts)


def _keep_reply_obligation(cur, message_ids):
    """Hold open the still-unanswered messages whose sender asked for a reply.

    Called where a message would otherwise be closed without an answer: the
    Runner's mechanical closure after a wake, and read_thread. Such a message
    goes to 'awaiting_reply' instead of 'processed' and is closed only by a
    reply, by a message back to the asker, or by the agent's own dismiss. The
    wake that just ended counts as the first attempt. Returns the ids held.
    """
    cfg = _reply_obligation_config()
    if cfg is None or not message_ids:
        return []
    cur.execute(
        "UPDATE sn_core.message_state ms SET status = 'awaiting_reply', "
        "       wake_next_at = now() + make_interval(secs => %s), "
        "       wake_attempts = 1, updated_at = now() "
        "FROM sn_core.messages m "
        "WHERE ms.message_id = m.id AND m.id = ANY(%s) "
        "  AND m.channel_type = 'internal' AND m.direction = 'inbound' "
        "  AND m.metadata->>'needs_reply' = 'true' "
        "  AND ms.status IN ('new', 'routed') "
        "RETURNING ms.message_id",
        (cfg[0], list(message_ids)),
    )
    return [r["message_id"] for r in cur.fetchall()]

# Terminal/transitional states a consumer may SET on an inbound message.
# 'new' is the seed state only and is never set via the API. 'blocked' and
# 'history' are verdicts of the ingest path, recorded with the journal entry
# that explains them -- letting a consumer stamp either one afterwards would
# produce a row that says "refused" or "predates the binding" with nothing
# anywhere saying by what rule.
_MARKABLE_STATES = ("routed", "processed", "failed")

# Message-ID domain for minted external ids -- the node's OWN domain
# (Environment=SMART_NODE_DOMAIN, set by core-api.sh), not the demo fleet.
_NODE_DOMAIN = os.environ.get("SMART_NODE_DOMAIN") or "smart-node.local"


#: The seats that read the whole node's correspondence:
#: the owner in the panel, and brin's message watcher, which calls as admin;
#: and the operator seat. Named, not inferred: every other principal, including
#: any role added later, reads only the messages it is a party to.
_READS_WHOLE_NODE = {"super-admin", "operator"}


def _must_scope(principal, mine: bool) -> bool:
    """Whether this caller sees only the messages it is a party to.

    Until this existed the list was scoped by tenant alone, so an agent calling
    it without `mine` read every colleague's mail, both directions, bodies
    included: more than the contact gate lets the same agent WRITE to. The
    field PR scoped role == "agent"; that leaves whatever role comes next
    reading everything, so the rule is inverted here and fails closed.
    """
    return bool(mine) or (principal.role or "") not in _READS_WHOLE_NODE


def _party_clause(username: str, ph: str = "%s"):
    """SQL for "this principal is on this message", either end of it.

    `mine` used to match the recipient alone, so "my messages" meant "my inbox":
    an agent could not see what it had sent, nor that a send had been refused,
    and reported letters as sent because queueing succeeded. On the field node
    a front desk reported seven letters out that the gate had blocked a second
    later. An agent is named two ways and both ends match both: by slug on
    internal mail, and by every mailbox it holds on a channel.

    `ph` is the placeholder style of the query this joins: the list and
    by-contact bind positionally, /search binds by name. One rule, so
    the three reads cannot drift apart.
    """
    box = ("SELECT ac.instance_ref FROM sn_config.agents a"
           " JOIN sn_config.agent_channels ac ON ac.agent_id = a.id"
           " WHERE a.slug = " + ph + " AND ac.enabled AND ac.instance_ref IS NOT NULL")
    clause = ("(m.sender = " + ph + " OR m.recipient = " + ph +
              " OR m.sender IN (" + box + ")"
              " OR m.recipient IN (" + box + "))")
    return clause, [username] * 4


def _inbox_clause(username: str):
    """SQL for "addressed to this principal": what `mine` has always meant.

    NOT widened to both ends, which is what the field PR did to `mine`. `mine`
    is comm.sh inbox's contract, and comm.sh marks everything it lists as
    'routed', the delivery receipt. An internal message an agent SENDS is stored
    as an INBOUND row with that agent as sender (it is the recipient's inbox
    copy, see /send and /reply). Matched on the sender, the author would take
    delivery of its own letter and the recipient would never be woken for it.
    """
    return ("(m.recipient = %s OR m.recipient IN ("
            " SELECT ac.instance_ref FROM sn_config.agents a"
            " JOIN sn_config.agent_channels ac ON ac.agent_id = a.id"
            " WHERE a.slug = %s AND ac.enabled AND ac.instance_ref IS NOT NULL))",
            [username] * 2)


def _refused_hidden(principal, mine=None, ph: str = "%s", *, allow_own_outbound: bool = True):
    """SQL for messages the contact gate did not refuse.

    The contact gate marks a refused inbound message 'blocked' and wakes nobody,
    and that was all it did: every read an agent has kept returning the row, body
    included. On example-node six letters from a sender Content agent was never granted were
    blocked, and Content agent read them anyway on a wake for something else, took a
    verification token out of one and used it. A whitelist that only stops the
    wake is a prompt-injection path with extra steps.

    Blocked inbound rows are invisible to every agent. A blocked outbound row
    authored by the caller may remain visible so the agent can report that its
    send was refused; it never grants access to an inbound stranger's content.
    Whole-node seats -- the owner and operator -- still see everything as
    release evidence. Action endpoints pass allow_own_outbound=False.

    Needs `ms` (message_state) and `m` (messages) in the query. `mine` is the
    caller's names when the query already has them (slug plus every mailbox);
    without it they are looked up by slug, bound with `ph` like _party_clause.
    """
    if (principal.role or "") in _READS_WHOLE_NODE:
        return "TRUE", []
    # An already-running agent can read before the routing poll has blocked
    # a conflicting message. Do not expose it to either receiver in that gap.
    unique = ("(m.direction <> 'inbound' OR m.channel_type IN ('internal','system') "
              "OR (SELECT count(*) FROM sn_config.agent_channels dup "
              "JOIN sn_config.agents receiver ON receiver.id=dup.agent_id "
              "JOIN sn_config.channels channel ON channel.id=dup.channel_id "
              "WHERE channel.slug=m.channel_type AND dup.instance_ref=m.recipient "
              "AND dup.enabled AND receiver.enabled AND receiver.archived_at IS NULL) <= 1)")
    if not allow_own_outbound:
        return "(" + unique + " AND ms.status <> 'blocked')", []
    if mine is not None:
        return ("(" + unique + " AND (ms.status <> 'blocked' OR (m.direction = 'outbound' "
                "AND m.sender = ANY(" + ph + "))))"), [list(mine)]
    me = principal.username
    return ("(" + unique + " AND (ms.status <> 'blocked' OR (m.direction = 'outbound' AND m.sender = "
            + ph + ")))", [me])


def _refuse_outside_wake(cur, principal, message_id: int) -> None:
    """409 when the calling agent is in a wake for one chat (agent_runtime.wake_scope,
    written by the Runner's claim) and this message is not one of that wake's.
    Each chat is worked in its own context: in one wake an agent read and
    answered nine other chats by their ids, all in the same session (2026-10-29).
    The scope holds only while that claim stands: same ref, busy, lease valid."""
    if (principal.role or "") != "agent":
        return
    cur.execute(
        "SELECT wake_scope FROM sn_core.agent_runtime WHERE agent_slug = %s "
        "  AND status = 'busy' AND lease_until > now() "
        "  AND wake_scope->>'ref' = current_ref",
        (principal.username,),
    )
    row = cur.fetchone()
    ids = ((row or {}).get("wake_scope") or {}).get("ids") or []
    if ids and int(message_id) not in ids:
        raise HTTPException(409, "message %s belongs to another chat and gets a turn of its "
                                 "own; in this turn answer only the messages you were woken "
                                 "for (%s)" % (message_id, ", ".join("#%s" % i for i in ids)))


def _visible_message(cur, principal, message_id: int, direction: str | None = None):
    """The message if the caller may act on it, else None.

    dismiss and reply took any id: an agent could close, or answer, a letter
    addressed to someone else, or one the gate refused -- Content agent dismissed two of
    the blocked letters she should never have seen. Acting is now bound by the
    same rule as reading: a party to the message and not a refused one. The
    whole-node seats act on anything, as before."""
    refused, rp = _refused_hidden(principal, allow_own_outbound=False)
    sql = ("SELECT m.id FROM sn_core.messages m "
           "JOIN sn_core.message_state ms ON ms.message_id = m.id "
           "WHERE m.id = %s AND " + refused)
    params = [message_id, *rp]
    if direction:
        sql += " AND m.direction = %s"
        params.append(direction)
    if (principal.role or "") not in _READS_WHOLE_NODE:
        party, pp = _party_clause(principal.username)
        sql += " AND " + party
        params.extend(pp)
        if not principal.cross_tenant:
            sql += " AND m.tenant_id = %s"
            params.append(principal.tenant_id)
    cur.execute(sql, params)
    return cur.fetchone()


@router.get("", response_model=dict)
def list_messages(
    limit: int = 200,
    direction: str | None = None,
    channel_type: str | None = None,
    status: str | None = None,
    thread_id: str | None = None,
    mine: bool = False,
    principal: Principal = Depends(requires("messages.read")),
):
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit must be 1..1000")
    if direction is not None and direction not in ("inbound", "outbound"):
        raise HTTPException(400, "direction must be 'inbound' or 'outbound'")
    if status is not None and status not in _QUEUE_STATES:
        raise HTTPException(400, "status must be one of: " + "/".join(_QUEUE_STATES))

    # ms.error beside ms.status: a `blocked` row arrives with the sentence that
    # says by what rule, which is the only way an agent learns its send failed.
    sql = (
        "SELECT m.id, m.thread_id, m.channel_type, m.direction, m.sender, m.recipient, m.content, m.received_at, "
        "       ms.status, ms.error, "
        "       m.metadata->>'subject'     AS subject, "
        "       m.metadata->>'in_reply_to' AS in_reply_to, "
        "       m.metadata->>'from_name'   AS from_name "
        "FROM sn_core.messages m "
        "JOIN sn_core.message_state ms ON ms.message_id = m.id "
    )
    where: list[str] = []
    params: list = []
    if not principal.cross_tenant:
        where.append("m.tenant_id = %s")
        params.append(principal.tenant_id)
    if direction:
        where.append("m.direction = %s")
        params.append(direction)
    if channel_type:
        where.append("m.channel_type = %s")
        params.append(channel_type)
    if status:
        where.append("ms.status = %s")
        params.append(status)
    if thread_id:
        where.append("m.thread_id = %s")
        params.append(thread_id)
    if _must_scope(principal, mine):
        # `mine` is still the inbox, addressed to me (see _inbox_clause: the
        # caller marks what it lists as delivered). Every other scoped read is
        # the correspondence this principal is part of, either end, by slug and
        # by every mailbox it holds: comm.sh thread now shows the part
        # of a thread the agent is on, including what it sent and was refused.
        clause, clause_params = (_inbox_clause if mine else _party_clause)(principal.username)
        where.append(clause)
        params.extend(clause_params)
    refused, refused_params = _refused_hidden(principal)
    if refused != "TRUE":
        where.append(refused)
        params.extend(refused_params)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY m.received_at DESC, m.id DESC LIMIT %s"
    params.append(limit)

    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return {"messages": rows, "generated_at": datetime.now(timezone.utc).isoformat()}


# Window (hours) -> bucket width (seconds), the same steps as the events chart.
_MSG_HIST_BUCKETS = {1: 120, 6: 900, 24: 3600, 168: 6 * 3600}
# Channels drawn as their own colour; anything else is "other".
_MSG_HIST_CHANNELS = ("web-chat", "telegram", "email", "voice", "internal")


@router.get("/histogram", response_model=dict)
def messages_histogram(hours: int = 24,
                       principal: Principal = Depends(requires("messages.read"))):
    """Message counts per time bucket, by channel and direction, for the panel.

    The message table only holds the newest couple of hundred rows, so a day's or
    a week's traffic is counted here. Every bucket in the window is returned
    (zeros included) so the columns line up in time; blocked and failed come from
    message_state, the same place the table's Status column reads.
    """
    if hours not in _MSG_HIST_BUCKETS:
        raise HTTPException(400, "hours must be one of 1, 6, 24, 168")
    width = _MSG_HIST_BUCKETS[hours]
    params: list = [width, width, hours]
    tenant_sql = ""
    if not principal.cross_tenant:
        tenant_sql = "AND m.tenant_id = %s"
        params.append(principal.tenant_id)
    sql = f"""
        SELECT floor(extract(epoch FROM m.received_at) / %s)::bigint * %s AS b,
               m.channel_type AS ch, m.direction AS dir,
               count(*) AS n,
               count(*) FILTER (WHERE ms.status = 'blocked') AS blocked,
               count(*) FILTER (WHERE ms.status = 'failed')  AS failed
        FROM sn_core.messages m
        LEFT JOIN sn_core.message_state ms ON ms.message_id = m.id
        WHERE m.received_at > now() - make_interval(hours => %s) {tenant_sql}
        GROUP BY 1, 2, 3
    """
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    chans = list(_MSG_HIST_CHANNELS) + ["other"]
    now = int(datetime.now(timezone.utc).timestamp())
    first = (now - hours * 3600) // width * width
    buckets = {}
    for t in range(first, now + 1, width):
        b = {"t": t, "blocked": 0, "failed": 0}
        for c in chans:
            b["in_" + c] = 0
            b["out_" + c] = 0
        buckets[t] = b
    for r in rows:
        bk = buckets.get(int(r["b"]))
        if bk is None:
            continue
        ch = r["ch"] if r["ch"] in _MSG_HIST_CHANNELS else "other"
        side = "in_" if r["dir"] == "inbound" else "out_"
        bk[side + ch] += int(r["n"])
        bk["blocked"] += int(r["blocked"])
        bk["failed"] += int(r["failed"])
    return {
        "hours": hours,
        "bucket_seconds": width,
        "channels": chans,
        "buckets": [buckets[t] for t in sorted(buckets)],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _stopped_channel_receiver(cur, channel_type, instance_ref):
    """The stopped agent an enabled binding of this channel instance points at,
    or None. Only one: two stopped agents on one mailbox is the ambiguous case
    the enabled path already refuses, and guessing between them is worse."""
    cur.execute(
        "SELECT a.slug FROM sn_config.agent_channels ac "
        "  JOIN sn_config.channels c ON c.id = ac.channel_id "
        "  JOIN sn_config.agents a ON a.id = ac.agent_id "
        " WHERE c.slug = %s AND ac.instance_ref = %s AND ac.enabled "
        "   AND NOT a.enabled AND a.archived_at IS NULL",
        (channel_type, instance_ref))
    rows = cur.fetchall()
    return rows[0]["slug"] if len(rows) == 1 else None


HELD_TAG = "held-recipient-stopped"


@router.post("/{message_id}/held", response_model=dict,
             dependencies=[Depends(requires("internal.messages.routing"))])
def message_held(message_id: int):
    """A message waits because its recipient agent is stopped. Says so once.

    Idempotent: the first call tags the row and records `message.held`; when the
    message came from another agent (internal), that agent gets a system note
    in its inbox, so it can tell whoever is waiting or try another colleague.
    Later calls change nothing and return told=false. The row stays 'new', so
    the message is handled the moment the agent is started again.
    A person on a channel is told by the channel's own dispatcher, which is the
    only thing that can answer on that channel.
    """
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "UPDATE sn_core.message_state SET tags = array_append(tags, %s), "
            "       updated_at = now() "
            " WHERE message_id = %s AND status = 'new' AND NOT (%s = ANY(tags)) "
            "RETURNING message_id", (HELD_TAG, message_id, HELD_TAG))
        if cur.fetchone() is None:
            return {"ok": True, "told": False}
        cur.execute("SELECT channel_type, sender, recipient, tenant_id "
                    "  FROM sn_core.messages WHERE id = %s", (message_id,))
        m = cur.fetchone()
        stopped = m["recipient"]
        if m["channel_type"] not in ("internal", "system"):
            stopped = _stopped_channel_receiver(cur, m["channel_type"], m["recipient"]) or m["recipient"]
        emit(cur, actor="component:message-watcher", event_type="message.held",
             entity_type="message", entity_id=str(message_id),
             payload={"reason": "recipient-stopped", "agent": stopped,
                      "channel": m["channel_type"], "from": m["sender"]})
        told_agent = None
        if m["channel_type"] == "internal" and m["sender"]:
            cur.execute("SELECT slug FROM sn_config.agents WHERE slug = %s "
                        "AND enabled AND archived_at IS NULL", (m["sender"],))
            if cur.fetchone():
                told_agent = m["sender"]
                cur.execute(
                    "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, "
                    "  direction, sender, recipient, content, content_type, metadata, tenant_id) "
                    "VALUES ('internal', %s, %s, 'inbound', 'system', %s, %s, 'text/plain', %s, %s)",
                    (told_agent, "<held-%s@smart-node.local>" % message_id, told_agent,
                     "Your message %s to %s is waiting: %s is stopped. It will be "
                     "delivered when %s is started again. If it cannot wait, tell "
                     "whoever is waiting, or ask someone else."
                     % (message_id, stopped, stopped, stopped),
                     Json({"source": "recipient-stopped", "about_message_id": message_id}),
                     m["tenant_id"]))
    return {"ok": True, "told": True, "agent": stopped, "told_agent": told_agent}


@router.get("/resolve-recipient", response_model=dict,
            dependencies=[Depends(requires("internal.messages.routing"))])
def resolve_recipient(channel_type: str, recipient: str):
    """Routing helper for the dispatcher. Given a message's (channel_type, recipient),
    return the agent permitted to read that channel, or null + a reason. The wake rule
    keys off this: an agent is woken only if it is the permitted recipient.

    - internal/system: `recipient` is already an agent slug; permitted iff that agent is
      active (enabled, not archived).
    - email/telegram/...: `recipient` is the channel instance (mailbox/handle); the
      permitted agent is the one with an ENABLED agent_channels binding to it.
    - a recipient that is an agent which is stopped (disabled, not archived) is
      `recipient-stopped`, with the agent in `stopped`. That is NOT a denial:
      the message waits for the agent to be started again, and the sender is
      told once (POST /{id}/held). It used to be dead-lettered as no-agent /
      not-permitted, and marked processed: the message was lost and nobody knew.
    Admitted by trusted origin: the caller is the node service that routes
    inbound mail, not an account, and no role holds this permission.
    """
    rec = (recipient or "").strip()
    if not rec:
        return {"agent": None, "reason": "no-recipient"}
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        if channel_type in ("internal", "system"):
            cur.execute("SELECT slug FROM sn_config.agents "
                        "WHERE slug=%s AND enabled AND archived_at IS NULL", (rec,))
            row = cur.fetchone()
            if row:
                return {"agent": row["slug"], "reason": "direct"}
            cur.execute("SELECT (archived_at IS NOT NULL) AS arch "
                        "FROM sn_config.agents WHERE slug=%s", (rec,))
            r2 = cur.fetchone()
            if r2 and not r2["arch"]:
                return {"agent": None, "reason": "recipient-stopped", "stopped": rec}
            return {"agent": None,
                    "reason": "recipient-archived" if (r2 and r2["arch"]) else "no-agent"}
        # A channel and mailbox must identify exactly one enabled receiver.
        receivers = channel_receivers(cur, channel_type, rec)
        if len(receivers) > 1:
            block_ambiguous_inbound(cur)
            return {"agent": None, "reason": "multiple-receivers", "receivers": receivers}
        if receivers:
            return {"agent": receivers[0], "reason": "channel-permitted"}
        stopped = _stopped_channel_receiver(cur, channel_type, rec)
        if stopped:
            return {"agent": None, "reason": "recipient-stopped", "stopped": stopped}
        cur.execute("SELECT EXISTS (SELECT 1 FROM sn_config.agent_channels ac "
                    "JOIN sn_config.channels c ON c.id=ac.channel_id "
                    "WHERE c.slug=%s AND ac.instance_ref=%s) AS bound", (channel_type, rec))
        return {"agent": None,
                "reason": "not-permitted" if cur.fetchone()["bound"] else "no-binding"}


class MessageStateUpdate(BaseModel):
    status: str           # one of _MARKABLE_STATES
    error: str | None = None


class MessageDismiss(BaseModel):
    message_id: int
    auto: bool = False    # the Runner closing what a finished wake left open,
                          # as opposed to the agent deciding no answer is owed


_INBOX_VIEWS = ("full", "new", "thread")


@router.get("/inbox", response_model=dict)
def my_inbox(limit: int = 20, view: str = "full", thread_id: str | None = None,
             principal: Principal = Depends(requires("messages.inbox.read"))):
    """The calling agent's recent CONVERSATION -- every message it sent or
    received, newest-first capped at `limit`, returned oldest-first with state.
    This is the agent's context: it sees the whole back-and-forth (both sides),
    so it can tell a fresh request from a courtesy that is already winding down,
    and decide whether a new message needs a reply or just a dismiss. The agent
    is identified by Remote-User (principal.username).

    `view`. The full window is what the Runner, the ledger and the voice
    relay read, and they keep it. A MODEL reading it is another matter: every
    character it reads stays in its session and is re-read by each later request.
    Measured on example-node over 24 hours, the agent tool's call was 38% of all the
    text agents read, 28k characters a call, and a typical call held 20 rows of
    which 19 were already handled. So:
      new    -- only what is addressed to the caller and still waiting (new or
                routed), plus relay duties. What the model must act on, nothing else.
      thread -- one conversation (thread_id), bounded by `limit`: the history,
                asked for when the model needs it rather than handed to it always.
    Both drop the metadata blob; subject and sender name stay.

    A message the inbound gate REFUSED is absent from every view. Before,
    the gate decided only whether the agent was woken; the letter itself was
    still returned here on its next wake from any cause, so anyone could put text
    in front of an agent by mailing its box. Its author still sees it: a refused
    send is how an agent learns it was refused."""
    if view not in _INBOX_VIEWS:
        raise HTTPException(400, "view must be one of: " + "/".join(_INBOX_VIEWS))
    if view == "thread" and not (thread_id or "").strip():
        raise HTTPException(400, "view=thread needs a thread_id")
    limit = max(1, min(int(limit or 20), 1000))
    me = principal.username
    with cursor() as cur:
        # An agent's own mail is part of its inbox, and matching the slug alone
        # left it out. An internal message names the agent BY SLUG; a channel
        # message names the MAILBOX it arrived at (maya.node@..., a telegram
        # chat id), and agent_channels is what ties the two together. So this
        # endpoint showed an agent everything except the letters actually
        # addressed to it.
        #
        # The dispatcher hid that for as long as it answered mail on the
        # agent's behalf. The moment mail went to the back office (the
        # FAST_CHANNELS half of this change), agents were woken for letters
        # they could not see, found an empty inbox, and said so -- which reads
        # as "nothing to do" rather than as a defect. It was first observed on
        # a field node.
        cur.execute(
            "SELECT ac.instance_ref FROM sn_config.agent_channels ac "
            "  JOIN sn_config.agents a ON a.id = ac.agent_id "
            " WHERE a.slug = %s AND ac.enabled "
            "   AND ac.instance_ref IS NOT NULL AND ac.instance_ref <> ''",
            (me,),
        )
        mine = [me] + [r["instance_ref"] for r in cur.fetchall() if r["instance_ref"]]
        refused, refused_params = _refused_hidden(principal, mine)
        where = ["(m.sender = ANY(%s) OR m.recipient = ANY(%s))",
                 "(%s OR m.tenant_id = %s)", refused]
        params = [mine, mine, principal.cross_tenant, principal.tenant_id, *refused_params]
        if view == "new":
            # Addressed TO me and still waiting. The recipient end only: an
            # internal message I send is stored as an inbound row with me as the
            # sender (see _inbox_clause), and it is my colleague's to act on.
            # awaiting_reply: a request this agent already saw and has not
            # answered. It stays in front of it until it is answered or dismissed.
            where.append("m.direction = 'inbound' "
                         "AND ms.status IN ('new', 'routed', 'awaiting_reply') "
                         "AND m.recipient = ANY(%s)")
            params.append(mine)
        elif view == "thread":
            where.append("COALESCE(m.thread_id, m.external_id) = %s")
            params.append(thread_id.strip())
        cur.execute(
            # received_at is not decoration: without it a caller asking "when did
            # he last write" has nothing to answer from. Asked exactly that on
            # 2026-08-06, an agent called a 32-second-old message "this morning".
            "SELECT m.id, m.channel_type, m.direction, m.sender, m.recipient, "
            "       m.content, ms.status, m.thread_id, m.metadata, m.received_at, "
            "       COALESCE(m.thread_id, m.external_id) AS thread_key "
            "FROM sn_core.messages m "
            "JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY m.id DESC LIMIT %s",
            (*params, limit),
        )
        rows = cur.fetchall()
    rows = list(reversed(rows))
    new_ids = [r["id"] for r in rows
               if r["status"] in ("new", "routed") and r["direction"] == "inbound"]
    owed_ids = [r["id"] for r in rows
                if r["status"] == "awaiting_reply" and r["direction"] == "inbound"
                and r["recipient"] == me]
    to_relay = []
    voice_answers = []
    for r in rows:
        if r["status"] == "new" and r["direction"] == "inbound":
            org = (r.get("metadata") or {}).get("origin")
            # ONE MALFORMED ROW MUST NOT TAKE DOWN THE WHOLE INBOX.
            # `origin` is written by several producers and at least one of them
            # (an owner-console injection) writes a plain STRING. `org.get` then
            # raised AttributeError, which is a 500 on /inbox -- so every agent
            # whose mailbox held one such row could not read ANY of its messages,
            # woke, failed, and left everything at 'new'. Reported from a live
            # node where it blocked all message-driven work.
            #
            # Say it rather than swallow it: a non-conforming origin means a
            # relay that will silently never happen, and that is worth a line in
            # the log even though it is no longer worth a 500.
            if org is not None and not isinstance(org, dict):
                log.warning("message %s has a non-dict metadata.origin (%s); "
                            "it cannot be relayed back to its conversation",
                            r["id"], type(org).__name__)
                org = None
            _rm = r.get("metadata") or {}
            # Never on voice, and never the agent's own hand-off to itself (#9).
            # The voice line hands work to the agent's back office with to=self;
            # that request carries the origin but is work to do, and listing it
            # here told a background turn to send its own hand-off to the caller.
            # A voice answer is spoken by the voice line while the call is live
            # and mailed to the owner by the node once it has ended; an agent that
            # sent it on voice produced a row nothing can deliver.
            _own_request = (r["sender"] == r["recipient"]
                            and not _rm.get("reply_to_message_id"))
            if org and org.get("channel") == "voice" and not _own_request:
                voice_answers.append(r["id"])
            # Only an ANSWER is relayed: a reply to something this agent sent. A
            # colleague's question carries the colleague's origin too, and listed
            # here it told the agent to send the question itself into that chat
            # as "here is the answer" (2026-10-29, nora).
            _answer_to_me = bool(_rm.get("reply_to_message_id")) and r["recipient"] == me
            if (org and org.get("conversation") and not _rm.get("relayed_by_system")
                    and _answer_to_me
                    and not _own_request and org.get("channel") != "voice"):
                to_relay.append({
                    "message_id": r["id"],
                    "from": r["sender"],
                    "answer": r["content"],
                    "origin": org,
                    "do_now": ("Relay this answer to the person who asked: call "
                               "send-message with to=%s, channel=%s, and a framed "
                               "body (e.g. 'You asked X; I checked with %s, here is "
                               "the answer: ...'). Then call inbox-dismiss with "
                               "message_id=%s so it does not come back."
                               % (org.get("conversation"), org.get("channel"),
                                  r["sender"], r["id"]))})
    if view == "full":
        guidance = ("Read the conversation for context. Reply only to a new id that "
                    "needs an answer or action; a pure thanks or acknowledgment -- "
                    "dismiss it.")
    else:
        # Computed above from the full rows (relays read metadata.origin); only
        # what the model is handed is trimmed.
        rows = [_lean_row(r) for r in rows]
        guidance = (("These are the messages waiting for you, nothing older. "
                     "Reply only to one that needs an answer or action; a pure thanks "
                     "or acknowledgment -- dismiss it. If you need the earlier "
                     "conversation of one of them, read that thread by its thread_id.")
                    if view == "new" else
                    ("One conversation, oldest first, the last %d messages at most." % limit))
    if voice_answers:
        guidance = ("Messages %s answer a request made on a call. Do not send them "
                    "anywhere: the voice line speaks them while the call is live, and "
                    "the node mails them to the owner once it has ended. Dismiss them. "
                    % ", ".join(str(i) for i in voice_answers)) + guidance
    if owed_ids:
        guidance = ("Messages %s asked you for a reply and have none yet; the sender "
                    "is waiting. Answer each with inbox_reply, or inbox_dismiss it if "
                    "no answer is owed. " % ", ".join(str(i) for i in owed_ids)) + guidance
    if to_relay:
        guidance = ("YOU HAVE ANSWERS TO RELAY. For EACH item in to_relay you MUST "
                    "now do its do_now: send-message to its origin, then "
                    "inbox-dismiss it. Do this before ending your turn. " + guidance)
    return {"conversation": rows, "new_message_ids": new_ids,
            "to_relay": to_relay, "guidance": guidance}


def _lean_row(r):
    """A message as a model needs it to act: no metadata blob (headers, return
    paths, address lists), which on an email is a tenth of the row and is read
    again on every later request of the session."""
    meta = r.get("metadata") or {}
    out = {k: r[k] for k in ("id", "channel_type", "direction", "sender", "recipient",
                             "content", "status", "received_at")}
    # The key view=thread matches on, so the model can always ask for the history:
    # a pre-thread row has no thread_id and is keyed by its own Message-ID.
    out["thread_id"] = r.get("thread_key") or r.get("thread_id")
    if isinstance(meta, dict):
        for k in ("subject", "from_name"):
            if meta.get(k):
                out[k] = meta[k]
    return out


@router.get("/snapshot", response_model=dict)
def inbox_snapshot(limit: int = 15,
                   principal: Principal = Depends(requires("messages.inbox.read"))):
    """the per-iteration conversation BOARD -- bounded and READ-ONLY.
    One line per thread (most-recent-by-activity, plus any with unread), so the
    per-turn prompt is bounded by thread COUNT, not by total history. It NEVER
    marks anything read: it is the only PURE reader (principle 6 -- one read
    owner). To read a thread's history, and clear its unread, the agent calls
    /read_thread. Threads are keyed by thread_id (falling back to a message's own
    Message-ID for pre-thread rows)."""
    me = principal.username
    # These two read by slug alone, so the caller's own names are just `me`.
    refused, _ = _refused_hidden(principal, [me], "%(me_names)s")
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "WITH mine AS ("
            "  SELECT m.id, COALESCE(m.thread_id, m.external_id) AS tkey, m.channel_type, "
            "         m.direction, m.sender, m.recipient, m.content, m.received_at, ms.status "
            "  FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "  WHERE (m.sender = %(me)s OR m.recipient = %(me)s) "
            "    AND (%(x)s OR m.tenant_id = %(tid)s) AND " + refused + ""
            "), agg AS ("
            "  SELECT tkey, max(id) AS last_id, max(received_at) AS last_at, "
            "         count(*) FILTER (WHERE status IN ('new', 'awaiting_reply') AND direction='inbound' "
            "                          AND recipient=%(me)s) AS unread "
            "  FROM mine GROUP BY tkey"
            ") "
            "SELECT a.tkey, a.last_id, a.unread, lm.channel_type, lm.sender, "
            "       lm.recipient, lm.content "
            "FROM agg a JOIN mine lm ON lm.id = a.last_id "
            "ORDER BY (a.unread > 0) DESC, a.last_at DESC LIMIT %(lim)s",
            {"me": me, "me_names": [me], "x": principal.cross_tenant,
             "tid": principal.tenant_id, "lim": max(1, min(limit, 50))},
        )
        rows = cur.fetchall()
    threads = []
    for r in rows:
        other = r["recipient"] if r["sender"] == me else r["sender"]
        unread = r["unread"] or 0
        item = {"thread_id": r["tkey"], "with": other, "channel": r["channel_type"],
                "unread": unread,
                # awaiting_them = I spoke last and am waiting; new = they spoke to me.
                "status": ("new" if unread else
                           ("awaiting_them" if r["sender"] == me else "idle"))}
        if unread:
            item["latest"] = (r["content"] or "").strip()[:200]
        threads.append(item)
    return {"threads": threads,
            "guidance": ("Your conversation board. A thread with unread>0 has a new "
                         "message for you -- call read_thread(thread_id) to read its "
                         "history (that also marks it read), then reply or act. This "
                         "board does not mark anything; only read_thread does.")}


class ThreadRead(BaseModel):
    thread_id: str
    limit: int = 10


@router.post("/read_thread", response_model=dict)
def read_thread(body: ThreadRead, principal: Principal = Depends(requires("messages.inbox.write"))):
    """The single read-and-mark-read path: the SINGLE read-and-mark-read path. Returns the last
    `limit` messages of one thread (oldest-first) AND atomically marks this agent's
    unread inbound messages in that thread as read (processed). Nothing else flips
    read-state -- the snapshot only shows, this is what clears 'new'. This is also
    what the Runner relies on: it wakes on 'unread to me', cleared only here by a
    genuine read (no ghost-processing, no watermark orphans)."""
    me = principal.username
    lim = max(1, min(body.limit, 50))
    refused, _ = _refused_hidden(principal, [me], "%(me_names)s")
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "SELECT m.id, m.channel_type, m.direction, m.sender, m.recipient, "
            "       m.content, ms.status, m.received_at "
            "FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "WHERE COALESCE(m.thread_id, m.external_id) = %(tk)s "
            "  AND (m.sender = %(me)s OR m.recipient = %(me)s) "
            "  AND (%(x)s OR m.tenant_id = %(tid)s) AND " + refused + " "
            "ORDER BY m.id DESC LIMIT %(lim)s",
            {"tk": body.thread_id, "me": me, "me_names": [me], "x": principal.cross_tenant,
             "tid": principal.tenant_id, "lim": lim},
        )
        rows = list(reversed(cur.fetchall()))
        if not rows:
            raise HTTPException(404, "thread not found or not addressed to you")
        # Reading is not answering: a message that asked for a reply is held
        # open first, so the mark-read below no longer finds it.
        cur.execute(
            "SELECT m.id FROM sn_core.messages m "
            "JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "WHERE COALESCE(m.thread_id, m.external_id) = %(tk)s "
            "  AND m.direction = 'inbound' AND m.recipient = %(me)s "
            "  AND ms.status = 'new' AND m.metadata->>'needs_reply' = 'true'",
            {"tk": body.thread_id, "me": me},
        )
        _keep_reply_obligation(cur, [row["id"] for row in cur.fetchall()])
        cur.execute(
            "UPDATE sn_core.message_state ms SET status='processed', "
            "       processed_at = now(), updated_at = now() "
            "FROM sn_core.messages m "
            "WHERE ms.message_id = m.id "
            "  AND COALESCE(m.thread_id, m.external_id) = %(tk)s "
            "  AND m.direction = 'inbound' AND m.recipient = %(me)s "
            "  AND ms.status = 'new' "
            "RETURNING ms.message_id",
            {"tk": body.thread_id, "me": me},
        )
        marked = [row["message_id"] for row in cur.fetchall()]
    return {"thread_id": body.thread_id, "messages": rows, "marked_read": marked}


@router.post("/dismiss", response_model=dict)
def dismiss_message(body: MessageDismiss,
                    principal: Principal = Depends(requires("messages.inbox.write"))):
    """Close an inbound message WITHOUT replying -- for a pure acknowledgment or
    anything that needs no answer. Marks it processed so it is not re-surfaced
    and so no courtesy loop forms. Only a message still in new/routed changes."""
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        # Same answer for "no such message" and "not yours to close", so the id
        # space of other agents' and refused mail cannot be probed.
        if _visible_message(cur, principal, body.message_id) is None:
            raise HTTPException(404, "message not found")
        # The Runner closes whatever a finished wake left open. For a message
        # that asked for a reply that closure is exactly how the request was
        # lost, so it is held open instead. The agent's own dismiss still
        # closes it: deciding no answer is owed is its call to make.
        if body.auto and _keep_reply_obligation(cur, [body.message_id]):
            return {"ok": True, "message_id": body.message_id, "dismissed": False,
                    "awaiting_reply": True}
        cur.execute(
            "UPDATE sn_core.message_state SET status = 'processed', "
            "processed_at = now(), updated_at = now() "
            "WHERE message_id = %s AND status IN ('new', 'routed', 'awaiting_reply')",
            (body.message_id,))
    return {"ok": True, "message_id": body.message_id, "dismissed": True}


@router.patch("/{message_id}/state", response_model=dict)
def mark_message_state(
    message_id: int,
    body: MessageStateUpdate,
    principal: Principal = Depends(requires("messages.inbox.write")),
):
    """Advance an inbound message's work-queue state (sn_core.message_state).

    Lets a consumer (secretary / brin) mark a message routed/processed/failed via
    the API instead of raw SQL. Tenant-scoped via the join to sn_core.messages;
    processed_at is stamped only for terminal states.
    """
    if body.status not in _MARKABLE_STATES:
        raise HTTPException(400, f"status must be one of: {', '.join(_MARKABLE_STATES)}")

    sql = (
        "UPDATE sn_core.message_state ms "
        "SET status = %s, error = %s, "
        "    processed_at = CASE WHEN %s IN ('processed', 'failed') "
        "                        THEN now() ELSE ms.processed_at END, "
        "    updated_at = now() "
        "FROM sn_core.messages m "
        "WHERE m.id = ms.message_id AND ms.message_id = %s "
    )
    params: list = [body.status, body.error, body.status, message_id]
    if not principal.cross_tenant:
        sql += "AND m.tenant_id = %s "
        params.append(principal.tenant_id)
    sql += "RETURNING ms.message_id, ms.status, ms.processed_at"

    with cursor(commit=True) as cur:
        # State changes are actions too: tenant scope alone let one agent alter
        # another agent's blocked inbound row and erase release evidence. Use
        # the same party/blocked guard as dismiss and reply; whole-node seats
        # retain their evidence-management access.
        if _visible_message(cur, principal, message_id) is None:
            raise HTTPException(404, "message not found")
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "message not found")
    return {"ok": True, **row}


def _quote_original(src, meta):
    """Build a natural Gmail-style quoted-history block for an email reply.

    The agent composes only its own prose; the original message is appended
    underneath as an attribution line plus '> '-prefixed quote, exactly as a
    human reply from a mail client would look. Returns "" when there is no
    original body to quote (e.g. an empty inbound)."""
    orig = (src.get("content") or "").strip()
    if not orig:
        return ""
    sender = (src.get("sender") or "").strip()
    name = (meta.get("from_name") or "").strip()
    if name and sender:
        who = "%s <%s>" % (name, sender)
    else:
        who = sender or name or "the sender"
    dt = src.get("received_at")
    if dt:
        hour12 = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        # Exact Gmail plain-text attribution: no leading zero on hour, no comma
        # between the time and the sender. e.g.
        # "On Sun, Jun 21, 2026 at 9:02 PM Jane Doe <a@b> wrote:"
        when = "%s, %s %d, %d at %d:%02d %s" % (
            dt.strftime("%a"), dt.strftime("%b"), dt.day, dt.year,
            hour12, dt.minute, ampm)
        attrib = "On %s %s wrote:" % (when, who)
    else:
        attrib = "%s wrote:" % who
    quoted = "\n".join("> " + ln for ln in orig.splitlines())
    return attrib + "\n" + quoted


def _wire_in_reply_to(src, meta):
    """The In-Reply-To an email reply carries: the Message-ID as it arrived.

    NOT the row's external_id. Since 38e3440 an inbound email row is
    identified as "<mailbox>|<Message-ID>", so that one letter to three of our
    boxes is three rows and not two dedup casualties. That value is a row id, not
    a header: a reply threaded against it carries In-Reply-To "box@x|<id@y>" and
    the reader's client starts a new conversation. dispatch.py moved to
    metadata.message_id in that commit and this route was missed.

    Rows ingested before the scoping carry the bare Message-ID in external_id and
    still thread through the fallback. A 'uid:' placeholder is not a real handle,
    scoped or not, and threads nothing.
    """
    wire = ((meta or {}).get("message_id") or "").strip()
    if not wire:
        ext = (src.get("external_id") or "").strip()
        if ext and "|" not in ext:
            wire = ext
    if not wire or wire.startswith("uid:"):
        return None
    return wire


def _audience_of(src, meta):
    """The room a letter was addressed to, which a reply keeps.

    A mail client can offer reply-all because the message in front of it carries
    To and Cc. Ours carries them in metadata since 38e3440: `recipient` is the
    BOX this copy landed in, not the header (one letter to three of our agents
    is three rows, all with the same To). Everyone in that room is a copy on the
    answer except our own box, because we are the one replying, and the sender,
    because they are already the To. The same rule as the fast path in
    communication/dispatch.py; the two services share no code, so it is stated
    twice and tested here.

    Returns [] for a row that never carried an audience: mail ingested before
    the headers were kept, and every non-email channel.
    """
    mine = (src.get("recipient") or "").strip().lower()
    frm = (src.get("sender") or "").strip().lower()
    out, seen = [], set()
    for addr in list((meta or {}).get("to_addrs") or []) + list((meta or {}).get("cc_addrs") or []):
        one = (addr or "").strip().lower()
        if not one or one in (mine, frm) or one in seen:
            continue
        seen.add(one)
        out.append(one)
    return out


class MessageReply(BaseModel):
    to_message_id: int    # the inbound message being answered
    body: str             # the composed reply text (prose only)
    cc: list[str] | None = None   # copies, email only. An EXPLICIT
                                  # audience, and it wins over the default
                                  # below. Every address is gated like the
                                  # primary one, and a single refusal refuses
                                  # the whole reply.
    only_sender: bool = False     # answer the person who wrote and drop the
                                  # rest of the room: the deliberate private
                                  # reply. The default keeps the room, see
                                  # _audience_of.


class FinalText(BaseModel):
    message_ids: list[int]    # what the turn owed an answer when it began
    body: str                 # what the agent said at the end of it


@router.post("/final-text", response_model=dict)
def final_text(body: FinalText, principal: Principal = Depends(requires("messages.send"))):
    """The agent's final text after a wake: its answer to the newest message it owed
    one, unless it replied with its tool during the turn, when the text is only a
    summary (dispatch.py does the same on a chat turn). Whether the message is still
    open does not matter: an agent that wrote its answer and dismissed the message
    has still answered, and the text is all the person would get."""
    if not body.message_ids:
        raise HTTPException(400, "no messages")
    with cursor() as cur:
        cur.execute("SELECT 1 FROM sn_core.messages WHERE metadata->>'reply_to_message_id' = ANY(%s) LIMIT 1",
                    ([str(i) for i in body.message_ids],))
        if cur.fetchone():
            return {"ok": True, "sent": False}
    out = reply_to_message(MessageReply(to_message_id=max(body.message_ids), body=body.body), principal)
    return dict(out, sent=True)


@router.post("/reply", response_model=dict)
def reply_to_message(
    body: MessageReply,
    principal: Principal = Depends(requires("messages.send")),
):
    """Record a threaded reply to an inbound message as an OUTBOUND row, and
    mark the inbound message processed.

    Channel-agnostic: the reply inherits the original's channel and threads
    against it (email -> Re:/In-Reply-To/References). The outbound row is left
    in its seed 'new' state for the communication outbound-pass to transmit;
    this endpoint never sends. The RFC822 Message-ID is minted here and stored
    as the row's external_id so the sender transmits with the same id.
    """
    if not body.body.strip():
        raise HTTPException(400, "reply body is empty")

    with cursor(commit=True) as cur:
        # The seed-state trigger on sn_core.messages INSERTs into message_state
        # unqualified, so this connection needs sn_core on its search_path for the
        # outbound INSERT below to fire (core-api's pool sets no search_path).
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "SELECT id, channel_type, sender, recipient, external_id, thread_id, "
            "       content, received_at, metadata, tenant_id "
            "FROM sn_core.messages "
            "WHERE id = %s AND direction = 'inbound'",
            (body.to_message_id,),
        )
        src = cur.fetchone()
        if src is None:
            raise HTTPException(404, "inbound message not found")
        if not principal.cross_tenant and src["tenant_id"] != principal.tenant_id:
            raise HTTPException(403, "message belongs to another tenant")
        # Answering a letter the gate refused is answering the stranger it was
        # refused from; answering someone else's is speaking for them.
        if _visible_message(cur, principal, body.to_message_id, "inbound") is None:
            raise HTTPException(404, "inbound message not found")
        _refuse_outside_wake(cur, principal, body.to_message_id)

        meta = src["metadata"] or {}

        # Internal agent-to-agent mail has no outbound transport: a reply is simply
        # a new internal message back to the original sender. Keep comm reply
        # symmetric with comm send for internal (do not strand an unsendable
        # internal outbound row, which the outbound-pass would mark failed).
        if src["channel_type"] == "internal":
            ext = email.utils.make_msgid(domain=_NODE_DOMAIN)
            # Internal messaging: internal replies thread exactly like email -- carry the
            # conversation's thread_id (the root's; fall back to the parent's own
            # Message-ID for pre-thread rows), set In-Reply-To against the parent, and
            # extend References. This was deliberately skipped before, which is the
            # reason internal mail was a stream of context-less one-shots: a multi-turn
            # exchange (price -> docs -> delivery) could not accumulate as one thread.
            thread_id = src["thread_id"] or src["external_id"]
            parent_ext = src["external_id"] or ""
            in_reply_to = parent_ext if (parent_ext and not parent_ext.startswith("uid:")) else None
            refs = (meta.get("references") or "").strip()
            references = ((refs + " " + in_reply_to).strip() if in_reply_to else (refs or ""))
            r_meta = {"source": "comm-send", "reply_to_message_id": src["id"],
                      "in_reply_to": in_reply_to or "", "references": references}
            org = (src["metadata"] or {}).get("origin")
            if org:
                r_meta["origin"] = org
            _relay_to = None if (_answers_an_answer(meta) or _already_relayed(cur, src["id"])) \
                else _relay_desk_for(cur, org, src["sender"], meta, answering=src["recipient"])
            # An answer to a request deep in work from a chat: the person sees it, the
            # work itself, whatever the asker then says; and the asker is still woken.
            _deep = False
            if (not _relay_to and meta.get("needs_reply") and not _already_relayed(cur, src["id"])
                    and isinstance(org, dict) and str(org.get("conversation") or "").startswith("desk:")
                    and _desk_owner(cur, org["conversation"])[0]):
                _relay_to, _deep = org["conversation"], True
            if _relay_to:
                r_meta["relayed_by_system"] = True
            cur.execute(
                "INSERT INTO sn_core.messages "
                "  (channel_type, channel_id, external_id, thread_id, direction, sender, "
                "   recipient, content, content_type, metadata, tenant_id) "
                "VALUES ('internal', %s, %s, %s, 'inbound', %s, %s, %s, 'text/plain', %s, %s) "
                "RETURNING id",
                (src["sender"], ext, thread_id, src["recipient"], src["sender"], body.body,
                 Json(r_meta), src["tenant_id"]),
            )
            rid = cur.fetchone()["id"]
            # Inbound for whoever is being replied to -- same shape as /send's
            # internal branch, so a two-agent exchange reads as a chain in the
            # journal instead of disappearing between turns.
            emit(cur, actor=f"agent:{src['recipient']}",
                 event_type="message.inbound",
                 entity_type="message", entity_id=str(rid),
                 payload={"from": src["recipient"], "to": src["sender"],
                          "channel": "internal",
                          "preview": (body.body or "")[:120],
                          "in_reply_to_message": src["id"]})
            if _relay_to:
                # Own line: the agent answering a message from itself (a front
                # line it is also on) or from the system about its own chat (a due
                # reminder); its chat needs no "Answer from" in front of it.
                own = src["sender"] in (src["recipient"], "system")
                _system_relay(cur, _relay_to, rid, src["recipient"], org,
                              body.body, src["tenant_id"], own=own)
                # The chat is the answer's delivery, whoever asked. For a person
                # who mentioned the agent there is no agent inbox (left 'new',
                # the watcher dead-lettered it as no-agent on every answer). For
                # the chat's own agent that asked a colleague, it is not news:
                # left 'new' it woke that agent, which told the chat again what
                # the colleague had just said there (2026-10-29). The colleague
                # answers in the chat; the asker stays out of it.
                if not _deep:
                    cur.execute(
                        "UPDATE sn_core.message_state SET status = 'processed', "
                        "processed_at = now(), updated_at = now() WHERE message_id = %s",
                        (rid,),
                    )
            _after_voice_answer(cur, rid, org)
            cur.execute(
                "UPDATE sn_core.message_state SET status = 'processed', "
                "processed_at = now(), updated_at = now() WHERE message_id = %s",
                (src["id"],),
            )
            return {"ok": True, "outbound_id": rid, "message_id": ext,
                    "to": src["sender"], "internal": True, "threaded": bool(in_reply_to)}

        # Threading: reply goes back to the original sender, from the mailbox it
        # arrived on. Subject gets a single Re:; In-Reply-To/References chain
        # against the original Message-ID (a 'uid:' placeholder is not a real
        # handle and is not threaded).
        subject = (meta.get("subject") or "").strip()
        if not subject.lower().startswith("re:"):
            subject = ("Re: " + subject) if subject else "Re: your message"
        in_reply_to = _wire_in_reply_to(src, meta)
        refs = (meta.get("references") or "").strip()
        if in_reply_to:
            references = (refs + " " + in_reply_to).strip()
        else:
            references = refs or None

        mailbox = src["recipient"]            # node mailbox = outbound sender
        domain = (mailbox.split("@")[-1]
                  if mailbox and "@" in mailbox else _NODE_DOMAIN)
        out_msgid = email.utils.make_msgid(domain=domain)

        out_meta = {
            "subject": subject,
            "in_reply_to": in_reply_to or "",
            "references": references or "",
            "reply_to_message_id": src["id"],
        }

        # Who stays on copy. Keeping the room is what a mail client does by
        # default, and it is the only behaviour an agent can get right on its
        # own: the alternative is reading addresses out of the quoted text and
        # retyping them, which is how a copy list silently loses people (
        # an earlier field report). An explicit cc from the caller wins; only_sender is the
        # deliberate private answer.
        if body.cc is not None:
            _cc = list(body.cc)
        elif body.only_sender:
            _cc = []
        else:
            _cc = _audience_of(src, meta)

        # Natural reply: the agent's prose on top, the original quoted beneath,
        # the way a human reply from a mail client reads.
        quoted = _quote_original(src, meta) if src.get("channel_type") == "email" else ""
        email_body = (body.body.rstrip() + "\n\n" + quoted) if quoted else body.body

        cur.execute(
            "INSERT INTO sn_core.messages "
            "  (channel_type, channel_id, external_id, thread_id, direction, "
            "   sender, recipient, recipients, content, content_type, metadata, tenant_id) "
            "VALUES (%s, %s, %s, %s, 'outbound', %s, %s, %s, %s, 'text/plain', %s, %s) "
            "RETURNING id",
            (src["channel_type"], mailbox, out_msgid, src["thread_id"],
             mailbox, src["sender"],
             Json(_recipient_rows(src["sender"], _cc)) if _cc else None,
             email_body, Json(out_meta), src["tenant_id"]),
        )
        out_id = cur.fetchone()["id"]

        # The reply is composed and queued. `agent` is the identity that
        # actually answered -- the field needed to explain a misrouted reply (a turn
        # addressed to one agent, answered by another) invisible until two log
        # files were matched by eye.
        emit(cur, actor=f"agent:{principal.username}",
             event_type="message.sent",
             entity_type="message", entity_id=str(out_id),
             payload={"from": mailbox, "to": src["sender"],
                      "channel": src["channel_type"],
                      "agent": principal.username,
                      "chars": len(email_body or ""),
                      "in_reply_to_message": src["id"]})

        # The secretary has answered: close the inbound work item. The outbound
        # row's own 'new' state (seeded by trigger) tracks delivery separately.
        cur.execute(
            "UPDATE sn_core.message_state "
            "SET status = 'processed', processed_at = now(), updated_at = now() "
            "WHERE message_id = %s",
            (src["id"],),
        )

    return {
        "ok": True,
        "outbound_id": out_id,
        "message_id": out_msgid,
        "to": src["sender"],
        "subject": subject,
        "threaded": bool(in_reply_to),
    }


class InboundMessage(BaseModel):
    recipient: str                  # the agent slug (or address) this is for
    content: str                    # the wake/notification text
    channel_type: str = "system"    # internal channel by default
    sender: str = "system"
    metadata: dict | None = None


def _inbound_tenant(cur, principal: Principal | None, recipient: str) -> int:
    """The tenant an inbound row belongs to.

    An account's own tenant when an account made the call. A node service has
    no account and so no tenant of its own: the row then belongs to the tenant
    of the agent it is addressed to. A recipient that is no agent here gives the
    row nowhere to belong, and that is refused instead of filed under a guess.
    """
    if principal is not None:
        return principal.tenant_id
    cur.execute(
        "SELECT tenant_id FROM sn_config.agents "
        "WHERE slug = %s AND archived_at IS NULL",
        (recipient.strip(),),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(
            400, f"recipient '{recipient}' is not an agent on this node; a node "
                 "service can record an inbound message only for one")
    return row["tenant_id"]


@router.post("/inbound", response_model=dict)
def record_inbound(
    body: InboundMessage,
    principal: Principal | None = Depends(requires("internal.messages.inbound")),
):
    """Record an INBOUND message via API -- a webhook for internal producers
    (e.g. the planner telling an agent it has work). The row is seeded 'new'
    by the message_state trigger, so the dispatch watcher picks it up and wakes
    the recipient agent: the same path an external channel message takes. The
    secretary/email flow is unchanged; this is the system-channel entry point.
    """
    if not body.recipient.strip():
        raise HTTPException(400, "recipient is required")
    if not body.content.strip():
        raise HTTPException(400, "content is empty")

    # external_id is NOT NULL; mint a unique synthetic id for system messages.
    ext = email.utils.make_msgid(domain=_NODE_DOMAIN)

    with cursor(commit=True) as cur:
        # Seed-state trigger INSERTs into message_state unqualified -> this
        # connection needs sn_core on its search_path (core-api sets none).
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        tenant_id = _inbound_tenant(cur, principal, body.recipient)
        cur.execute(
            "INSERT INTO sn_core.messages "
            "  (channel_type, channel_id, external_id, direction, sender, "
            "   recipient, content, content_type, metadata, tenant_id) "
            "VALUES (%s, %s, %s, 'inbound', %s, %s, %s, 'text/plain', %s, %s) "
            "RETURNING id",
            (body.channel_type, body.recipient, ext, body.sender,
             body.recipient, body.content, Json(body.metadata or {}),
             tenant_id),
        )
        new_id = cur.fetchone()["id"]
        # Who this is from is decided once, at arrival.
        # Internal and system traffic is agent-to-agent and has no person behind
        # it, so it is not stamped. Never fatal: a verdict that cannot be written
        # must not cost the message it describes.
        if body.channel_type not in ("internal", "system"):
            try:
                stamp_identity(cur, new_id, body.channel_type, body.sender)
            except Exception as _ie:      # pragma: no cover -- diagnostics only
                log.warning("identity not stamped for inbound %s: %s", new_id, _ie)
        # Root of a work chain: external/system trigger enters here. Every other
        # route that writes a messages row emits too (/send, /reply) -- the note
        # that used to stand here, that agent-to-agent sends were recorded by
        # comm.sh instead, was never true: the communication component sees only
        # channel messages, never a row this service writes.
        actor = (f"agent:{body.sender}" if body.channel_type == "internal"
                 else (body.sender or "system"))
        emit(cur, actor=actor, event_type="message.inbound",
             entity_type="message", entity_id=str(new_id),
             payload={"from": body.sender, "to": body.recipient,
                      "channel": body.channel_type,
                      "preview": (body.content or "")[:120]})

    return {"ok": True, "id": new_id, "recipient": body.recipient,
            "channel_type": body.channel_type, "external_id": ext}


class MessageOriginate(BaseModel):
    to: str                       # recipient: an agent slug (internal) or an external address
    body: str                     # message content (prose)
    subject: str | None = None    # subject for a new external (e.g. email) thread
    channel: str | None = None    # external channel slug (email, telegram, ...); inferred when omitted
    origin: dict | None = None    # {channel, conversation} of the requester this handoff serves
    cc: list[str] | None = None   # copies, email only. Every address is
                                  # gated exactly like `to`, and one refusal
                                  # refuses the whole message.
    needs_reply: bool = False     # colleague messages only: the sender must get
                                  # an answer back. The node keeps the message
                                  # open until it is answered or dismissed,
                                  # reminds the recipient, and tells the sender
                                  # if it stays unanswered.


def gateway_warning_allowed(body: MessageOriginate) -> bool:
    """Limit the gateway's owner delegation to its budget warning format."""
    owner = os.environ.get("BUDGET_NOTIFY_ACTOR", "").strip()
    channel = os.environ.get("BUDGET_NOTIFY_CHANNEL", "").strip()
    return bool(owner and channel and body.to == owner and body.channel == channel
                and body.body.startswith("Budget pre-warning:")
                and len(body.body) <= 1000
                and not body.subject and not body.origin and not body.cc)


def _recipient_rows(to: str, cc):
    """The recipient set stored on a message: the primary first, then the copies.

    Shape is [{"addr","role"}] with role in (to, cc) -- see
    bootstrap/B02-sn-core-schema.sql. The set is channel-agnostic on purpose;
    the ROLE is not, and only the email adapter reads it. Written only when
    there are copies, so an ordinary one-to-one message keeps a NULL here and
    behaves exactly as it did before.
    """
    rows, seen = [], set()
    for addr, role in [(to, "to")] + [(a, "cc") for a in (cc or [])]:
        a = (addr or "").strip()
        if not a or a.lower() in seen:
            continue
        seen.add(a.lower())
        rows.append({"addr": a, "role": role})
    return rows


def _resolve_person(who: str, channel: str) -> str:
    """Resolve a person token ('owner' or a users.handle) to their registered
    address on `channel`. Unknown tokens pass through unchanged (raw chat ids /
    external addresses still work); a known person lacking that channel is a 400.
    Source of truth: sn_config.users + sn_config.user_channels."""
    w = (who or '').strip()
    if not w:
        return who
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_config, public")
        if w.lower() == 'owner':
            cur.execute(
                "SELECT id FROM sn_config.users "
                "WHERE role = 'super-admin' AND active AND archived_at IS NULL "
                "ORDER BY id LIMIT 1"
            )
        else:
            cur.execute(
                "SELECT id FROM sn_config.users "
                "WHERE handle = %s AND active AND archived_at IS NULL LIMIT 1",
                (w.lower(),),
            )
        u = cur.fetchone()
        if not u:
            return who
        cur.execute(
            "SELECT address FROM sn_config.user_channels "
            "WHERE user_id = %s AND channel_slug = %s AND enabled LIMIT 1",
            (u['id'], channel),
        )
        c = cur.fetchone()
    if not c:
        raise HTTPException(
            400, f"person '{who}' has no enabled '{channel}' channel registered")
    return c['address']


# ---- Desk delivery and colleague answers ----------------------------
# A desk is ONE agent's conversation with a person (sn_core.desks.metadata.agent).
# Writing a row is only intent; the delivery layer decides whether the person sees
# it (the owner's rule: "the agent only puts a record in
# the table, security lets it through or not").

def _desk_owner(cur, desk_addr):
    """(exists, owning agent slug) for 'desk:<uuid>'."""
    cur.execute("SELECT metadata->>'agent' AS agent FROM sn_core.desks WHERE id::text = %s",
                (str(desk_addr)[len("desk:"):],))
    row = cur.fetchone()
    return (row is not None, row["agent"] if row else None)


def _relay_desk_for(cur, origin, asker, meta=None, answering=None):
    """The desk to relay an answer into, or None. Only the asker's own
    conversation: a desk whose agent asked, the desk of the person who
    @mentioned the answering agent in it (a chat mention written by
    POST /api/desks/{id}/mentions, which only the desk's owner can write), or the
    desk of the answering agent when the system asked about it (a due reminder).
    The system relays nowhere else."""
    if not isinstance(origin, dict):
        return None
    conv = str(origin.get("conversation") or "")
    if not conv.startswith("desk:"):
        return None
    exists, owner = _desk_owner(cur, conv)
    if exists and owner == asker:
        return conv
    if exists and asker == "system" and answering and owner == answering:
        return conv
    if exists and _chat_mention_desk(meta) == conv and _desk_owned_by(cur, conv, asker):
        return conv
    return None


def _answers_an_answer(meta):
    """True when the message being answered is itself a reply. The answer to the
    chat's question was relayed already; what the agents say after it (a thanks,
    the answer to the thanks) is theirs. The origin rides along every reply, so
    each turn of that exchange reached the person as "Answer from nora" again
    (2026-10-29: three times in two minutes)."""
    return isinstance(meta, dict) and bool(meta.get("reply_to_message_id"))


def _already_relayed(cur, question_id) -> bool:
    """True when an answer to this question was relayed into a chat already.
    One question, one answer in the chat: an agent that answered twice (a send
    taken as the reply, then inbox_reply on the same question) put both in the
    person's chat, the second one invented (2026-10-29). The second still
    reaches the agent that asked."""
    cur.execute(
        "SELECT 1 FROM sn_core.messages WHERE metadata->>'reply_to_message_id' = %s "
        "AND metadata->>'relayed_by_system' = 'true' LIMIT 1",
        (str(question_id),),
    )
    return cur.fetchone() is not None


def _chat_mention_desk(meta):
    """'desk:<id>' when the message is a person's @mention in that chat, else None.
    All three must agree: kind, reason, and the origin naming the same desk."""
    if not isinstance(meta, dict) or meta.get("kind") != "chat" \
            or meta.get("reason") != "mentioned":
        return None
    org = meta.get("origin")
    conv = str(org.get("conversation") or "") if isinstance(org, dict) else ""
    return conv if conv == "desk:%s" % (meta.get("desk_id") or "") else None


def _desk_owned_by(cur, desk_addr, username):
    """True when the person `username` (sn_config.users.email, the login) owns the desk."""
    cur.execute(
        "SELECT 1 FROM sn_core.desks d JOIN sn_config.users u ON u.id = d.owner_user_id "
        "WHERE d.id::text = %s AND u.email = %s",
        (str(desk_addr)[len("desk:"):], username),
    )
    return cur.fetchone() is not None


def _open_question_from(cur, me, peer, tenant_id):
    """The newest question `peer` sent `me` that is still open and carries an
    origin. A /send from `me` to `peer` while this exists is an answer to it: the
    prompt used to teach `send` for answers, and a send drops the origin, so the
    answer never reached the person who was waiting (2026-09-23, max -> integration-agent)."""
    cur.execute(
        "SELECT m.id, m.external_id, m.thread_id, m.metadata "
        "FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id = m.id "
        "WHERE m.channel_type = 'internal' AND m.direction = 'inbound' "
        "  AND m.sender = %s AND m.recipient = %s AND m.tenant_id = %s "
        "  AND ms.status IN ('new', 'routed', 'awaiting_reply') AND m.metadata ? 'origin' "
        "  AND m.received_at > now() - interval '7 days' "
        "ORDER BY m.id DESC LIMIT 1",
        (peer, me, tenant_id),
    )
    return cur.fetchone()


def _open_question_for_desk(cur, me, desk, owner, tenant_id):
    """The newest open question the desk's own agent `owner` sent `me` from that
    desk. A send from `me` to `desk` while this exists is the answer to it:
    agents were taught to write to origin.conversation, so they addressed the
    asker's desk instead of the asker, the delivery gate blocked the row after it
    was written, and the sender was told ok. Seen 2026-09-25: max -> agent-b x2,
    max -> web-agent x2, http-agent -> web-agent."""
    cur.execute(
        "SELECT m.id, m.external_id, m.thread_id, m.metadata "
        "FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id = m.id "
        "WHERE m.channel_type = 'internal' AND m.direction = 'inbound' "
        "  AND m.sender = %s AND m.recipient = %s AND m.tenant_id = %s "
        "  AND ms.status IN ('new', 'routed', 'awaiting_reply') "
        "  AND m.metadata->'origin'->>'conversation' = %s "
        "  AND m.received_at > now() - interval '7 days' "
        "ORDER BY m.id DESC LIMIT 1",
        (owner, me, tenant_id, desk),
    )
    return cur.fetchone()


_NOTIFY_SENDER = os.environ.get("SN_NOTIFY_SENDER") or None


def _voice_live_origin(cur, origin):
    """The origin of a hand-off made on a call, pointed at the call's real address.

    The voice line used to stamp `voice:<uid>`, the id its browser sends when the
    socket opens (#9). The call's turns are recorded on the caller's voice address
    from user_channels, the same lookup /record-voice-turns makes, so an answer
    addressed to the uid went somewhere the call never was. When the line names
    the caller, the conversation is that address; the call's session id stays in
    `call`, which is how an answer is later matched to a call that has ended.
    """
    if not isinstance(origin, dict) or origin.get("channel") != "voice":
        return origin
    caller = str(origin.get("caller") or "").strip()
    if not caller:
        return origin
    cur.execute(
        "SELECT uc.address FROM sn_config.user_channels uc "
        "JOIN sn_config.users u ON u.id = uc.user_id "
        "WHERE uc.channel_slug = 'voice' AND uc.enabled "
        "AND (u.email = %s OR u.handle = %s OR uc.address = %s) "
        "AND u.active AND u.archived_at IS NULL LIMIT 1",
        (caller, caller.lower(), caller),
    )
    row = cur.fetchone()
    if row and row["address"]:
        return {**origin, "conversation": row["address"]}
    return origin


def _voice_call_ended(cur, call) -> bool:
    """True once the voice line has recorded voice.call.ended for this call."""
    if not call:
        return False
    cur.execute(
        "SELECT 1 FROM sn_core.events WHERE event_type = 'voice.call.ended' "
        "AND entity_type = 'call' AND entity_id = %s LIMIT 1",
        (str(call),),
    )
    return cur.fetchone() is not None


def _tell_agent(cur, agent, text, about_id, tenant_id, source):
    """Put a short internal note from the node into an agent's inbox.

    Used when something the agent believes was delivered was not: the note is
    what lets it say so instead of assuming the message went out.
    """
    ext = email.utils.make_msgid(domain=_NODE_DOMAIN)
    cur.execute(
        "INSERT INTO sn_core.messages "
        "  (channel_type, channel_id, external_id, thread_id, direction, sender, "
        "   recipient, content, content_type, metadata, tenant_id) "
        "VALUES ('internal', 'system', %s, %s, 'inbound', 'system', %s, %s, "
        "        'text/plain', %s, %s) RETURNING id",
        (ext, ext, agent, text, Json({"source": source, "about_message_id": about_id}),
         tenant_id),
    )
    return cur.fetchone()["id"]


class MessageUnhandled(BaseModel):
    reason: str = "wake-exhausted"   # why the recipient could not handle it
    attempts: int | None = None


UNHANDLED_TAG = "unhandled"


@router.post("/{message_id}/unhandled", response_model=dict,
             dependencies=[Depends(requires("internal.messages.routing"))])
def message_unhandled(message_id: int, body: MessageUnhandled):
    """The recipient was woken for this message and could not get through it:
    every wake ended (cut by the turn limit, failed) with the message still
    open, and the Runner has stopped waking it for it.

    Said once: the first call tags the row and records `message.unhandled`;
    when another agent wrote the message, that agent gets a note, so it can ask
    again, ask someone else or tell whoever is waiting. The row is NOT closed --
    it stays readable in the recipient's inbox, and a person or a later wake
    can still answer it. A sender with no inbox (the operator seat) has the
    journal entry, as with an unanswered reply obligation.
    """
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "UPDATE sn_core.message_state SET tags = array_append(tags, %s), updated_at = now() "
            " WHERE message_id = %s AND NOT (%s = ANY(tags)) RETURNING status",
            (UNHANDLED_TAG, message_id, UNHANDLED_TAG))
        row = cur.fetchone()
        if row is None:
            return {"ok": True, "told": False}
        cur.execute("SELECT sender, recipient, tenant_id, received_at "
                    "  FROM sn_core.messages WHERE id = %s", (message_id,))
        m = cur.fetchone()
        cur.execute("SELECT 1 FROM sn_config.agents WHERE slug = %s AND enabled "
                    "AND archived_at IS NULL", (m["sender"],))
        sender_is_agent = cur.fetchone() is not None
        if sender_is_agent:
            _tell_agent(
                cur, m["sender"],
                "%s could not get through your message #%d (sent %s): it was woken "
                "for it %s and every turn ended without an answer (%s). The node has "
                "stopped waking it for this. Decide what happens next: ask again, ask "
                "someone else, or tell whoever is waiting that there is no answer yet."
                % (m["recipient"], message_id, m["received_at"].strftime("%Y-%m-%d %H:%M %Z"),
                   ("%d times" % body.attempts) if body.attempts else "several times",
                   body.reason),
                message_id, m["tenant_id"], "unhandled")
        emit(cur, actor="component:runner", event_type="message.unhandled",
             entity_type="message", entity_id=str(message_id),
             payload={"from": m["sender"], "to": m["recipient"], "reason": body.reason,
                      "attempts": body.attempts, "status": row["status"],
                      "sender_told": sender_is_agent})
    return {"ok": True, "told": sender_is_agent}


def _mail_late_voice_answer(cur, answer_id):
    """The node mails the owner an answer whose call has already ended (#9).

    The voice line speaks an answer only while the call is live. After it the
    answer goes to the owner by email, sent by the node itself the way the
    budget-limit letter is: not every agent may email the owner, and the answer
    must not depend on the agent choosing to. Once per answer. When the node
    cannot send it (no SN_NOTIFY_SENDER, or the owner has no email channel), the
    answering agent is told so and an event says why: never dropped silently.
    Returns what happened, for the caller and the tests.
    """
    cur.execute(
        "SELECT id, sender, recipient, content, metadata, tenant_id, received_at "
        "FROM sn_core.messages WHERE id = %s FOR UPDATE",
        (answer_id,),
    )
    ans = cur.fetchone()
    if ans is None:
        return "missing"
    meta = ans["metadata"] or {}
    # messages is append-only, so "already handled" is the event this function
    # writes, not a mark on the row; the FOR UPDATE above serialises two callers.
    cur.execute(
        "SELECT 1 FROM sn_core.events WHERE entity_type = 'message' AND entity_id = %s "
        "AND event_type IN ('voice.answer.mailed', 'voice.answer.undelivered') LIMIT 1",
        (str(ans["id"]),),
    )
    if cur.fetchone() is not None:
        return "already"
    org = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
    question = ""
    if meta.get("reply_to_message_id"):
        cur.execute("SELECT content FROM sn_core.messages WHERE id = %s",
                    (meta["reply_to_message_id"],))
        q = cur.fetchone()
        question = ((q or {}).get("content") or "").split(chr(10) * 2 + "When this is done")[0]
    cur.execute(
        "SELECT u.tenant_id, uc.address FROM sn_config.users u "
        "JOIN sn_config.user_channels uc ON uc.user_id = u.id "
        "WHERE u.role = 'super-admin' AND u.active AND u.archived_at IS NULL "
        "AND uc.channel_slug = 'email' AND uc.enabled ORDER BY u.id LIMIT 1"
    )
    owner = cur.fetchone()
    agent = ans["sender"]
    outcome, mail_id = None, None
    if not _NOTIFY_SENDER:
        outcome = "not sent: SN_NOTIFY_SENDER is unset, the node has no mailbox to write from"
    elif owner is None or not owner["address"]:
        outcome = "not sent: the node owner has no enabled email channel"
    else:
        para = chr(10) * 2
        text = ("An answer arrived after your call with %s had ended, so it is sent "
                "here instead." % agent) + para
        if question:
            text += "You asked:" + chr(10) + question.strip() + para
        text += "The answer:" + chr(10) + (ans["content"] or "").strip() + chr(10)
        cur.execute(
            "INSERT INTO sn_core.messages "
            "  (channel_type, channel_id, external_id, direction, sender, recipient, "
            "   content, content_type, metadata, tenant_id) "
            "VALUES ('email', %s, %s, 'outbound', %s, %s, %s, 'text/plain', %s, %s) "
            "RETURNING id",
            (_NOTIFY_SENDER, email.utils.make_msgid(domain=_NODE_DOMAIN),
             _NOTIFY_SENDER, owner["address"], text,
             Json({"subject": "Answer to what you asked on a call with %s" % agent,
                   "source": "voice-late-answer", "from_name": "Smart Node",
                   "answer_message_id": ans["id"], "call": org.get("call")}),
             owner["tenant_id"]),
        )
        mail_id = cur.fetchone()["id"]
        outcome = "mailed"
    if mail_id is None:
        emit(cur, actor="component:core-api", event_type="voice.answer.undelivered",
             entity_type="message", entity_id=str(ans["id"]),
             payload={"reason": outcome, "call": org.get("call"), "agent": agent})
        _tell_agent(cur, agent,
                    "Your answer (message %s) to a caller could not be delivered: the "
                    "call had ended and it was %s. Tell the person another way, or "
                    "ask the owner to configure the node's mail." % (ans["id"], outcome),
                    ans["id"], ans["tenant_id"], "voice-late-answer-undelivered")
    else:
        emit(cur, actor="component:core-api", event_type="voice.answer.mailed",
             entity_type="message", entity_id=str(ans["id"]),
             payload={"mail_id": mail_id, "call": org.get("call"), "agent": agent})
    return outcome


def _after_voice_answer(cur, answer_id, origin):
    """An answer carrying a voice origin: the line speaks it while the call is
    live; once the call has ended the node mails it to the owner."""
    if (isinstance(origin, dict) and origin.get("channel") == "voice"
            and _voice_call_ended(cur, origin.get("call"))):
        return _mail_late_voice_answer(cur, answer_id)
    return None


def _system_relay(cur, desk, answer_id, answered_by, origin, text, tenant_id, own=False):
    """Write the answer into the asker's desk as the SYSTEM. It is an ordinary
    intent row: the watcher still decides, and passes it because the sender is
    'system'. The first line names who answered (rule 3 of the ruling), except
    on the agent's own line (own=True): the chat IS that agent, so "Answer from
    <itself>" only stood in front of a reminder it was telling the person."""
    cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
    ext = email.utils.make_msgid(domain=_NODE_DOMAIN)
    cur.execute(
        "INSERT INTO sn_core.messages "
        "  (channel_type, channel_id, external_id, thread_id, direction, sender, "
        "   recipient, content, content_type, metadata, provenance_chain, tenant_id) "
        "VALUES ('internal', %s, %s, %s, 'inbound', 'system', %s, %s, 'text/plain', %s, %s, %s) "
        "RETURNING id",
        (desk, ext, ext, desk, text if own else "Answer from %s:\n\n%s" % (answered_by, text),
         Json({"source": "system-relay", "relay_of": answer_id}),
         Json({"acting_service": "relay", "source_message_id": answer_id,
               "answered_by": answered_by, "origin": origin}),
         tenant_id),
    )
    rid = cur.fetchone()["id"]
    emit(cur, actor="component:relay", event_type="message.relayed",
         entity_type="message", entity_id=str(rid),
         payload={"source_message_id": answer_id, "answered_by": answered_by,
                  "target_conversation": desk,
                  # the chat by its id, as the live stream finds an event's chat
                  "desk": str(desk).removeprefix("desk:")})
    return rid


@router.post("/send", response_model=dict)
def send_message(
    body: MessageOriginate,
    principal: Principal = Depends(requires("messages.send")),
):
    """Originate a NEW message (start a thread) -- the agent-initiated counterpart
    of /reply. One verb, routed by recipient:

      - recipient is a bare agent slug  -> an INTERNAL agent-to-agent message:
        channel_type='internal', direction='inbound', sender=<originating agent>.
        It behaves like real mail (the recipient reads it from the messages queue
        and may reply), not a fire-and-forget system doorbell.

      - recipient is an external address (or an explicit external channel) -> an
        OUTBOUND row on that channel from the originating agent's mailbox, with a
        freshly minted Message-ID and no In-Reply-To (a new thread). The
        communication outbound-pass transmits it; this endpoint never sends.

    No schema change: origin is encoded by channel_type (internal vs external),
    sender, and metadata.source='comm-send'.
    """
    if principal.acting_service == "gateway" and not gateway_warning_allowed(body):
        raise HTTPException(403, "gateway may send only a budget pre-warning to the configured owner")
    if not body.to.strip():
        raise HTTPException(400, "recipient (to) is required")
    if not body.body.strip():
        raise HTTPException(400, "message body is empty")

    channel = (body.channel or "").strip().lower()
    # Person-recipient resolution: 'owner' or a users.handle -> that person's
    # registered address on the requested channel (source of truth =
    # sn_config.users + user_channels; no hardcoded chat ids / emails). Unknown
    # tokens pass through unchanged so raw addresses still work.
    if "@" not in body.to and channel and channel not in ("internal", "system"):
        body.to = _resolve_person(body.to, channel)
    is_external = ("@" in body.to) or (channel and channel not in ("internal", "system"))
    if body.needs_reply:
        if is_external or body.to.startswith("desk:"):
            raise HTTPException(
                400, "needs_reply is for a message to a colleague agent; a person "
                     "or a chat cannot be reminded by the node")
        if _reply_obligation_config() is None:
            raise HTTPException(
                503, "reply obligations are not configured on this node "
                     "(SN_REPLY_WAKE_WINDOW_SECONDS, SN_REPLY_MAX_ATTEMPTS): "
                     "nothing was sent. Send without needs_reply, or ask the operator.")

    result = None
    with cursor(commit=True) as cur:
        # Seed-state trigger INSERTs into message_state unqualified -> this
        # connection needs sn_core on its search_path (core-api sets none).
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        body.origin = _voice_live_origin(cur, body.origin)

        # A desk is a real internal destination with a real reader, and it has no
        # agent process by design: desk_history folds internal messages addressed
        # to `desk:<id>` into the conversation as assistant turns,
        # and the browser is what reads them. The agent check below must not apply
        # to it -- refusing a desk broke the relay path and the acceptance run
        # caught it as A2 and A6 (HTTP 400, "no active agent 'desk:...'").
        _to_a_desk = body.to.startswith("desk:")

        # An agent may write into its OWN desk through the authenticated relay path and
        # nowhere else. Refuse a foreign desk HERE, before a row exists, so the
        # agent reads the refusal in its tool result; the delivery gate alone
        # blocked the row afterwards and the sender believed it had answered.
        # Except when it is plainly an answer: the desk's own agent asked this
        # agent something from that desk and is still waiting. Then the send is
        # re-addressed to the asker and becomes the reply below, which the system
        # relays into the desk.
        # The same holds on a chat channel (channel=web-chat, to=desk:<id>): the
        # outbound compose path renders into that desk too (architect review
        # the prior write path). There the refusal is unconditional: an answer to a
        # colleague is internal mail, never a chat line written as someone else.
        _desk_question = None
        if _to_a_desk and (principal.role or "") == "agent":
            _desk_exists, _desk_agent = _desk_owner(cur, body.to)
            if _desk_agent != principal.username:
                if _desk_exists and _desk_agent and not is_external:
                    _desk_question = _open_question_for_desk(
                        cur, principal.username, body.to, _desk_agent,
                        principal.tenant_id)
                if _desk_question is None:
                    raise HTTPException(
                        403,
                        f"'{principal.username}' may not write into {body.to}: "
                        + (f"that chat belongs to '{_desk_agent}'. "
                           if _desk_exists else "no such chat. ")
                        + "To answer a colleague, reply to their message: "
                        "inbox_reply(<their message id>, <answer>) or comm.sh reply "
                        "--to <their message id>. The system puts your answer into "
                        "the chat of whoever is waiting.")
                body.to = _desk_agent
                _to_a_desk = False

        if not is_external:
            # Verify the recipient is a live agent BEFORE inserting. A row that is
            # accepted here but refused at dispatch gives the sender a false "ok"
            # and silently drops the message -- the exact failure that caused two
            # completion reports to disappear on 2026-08-31.
            if _to_a_desk:
                # A desk has no agent process by design and is still a real
                # destination: the person's browser reads it. Treat it as present
                # so the checks below (which exist to catch a mistyped agent slug)
                # do not refuse a delivery path the product depends on.
                _agent_row = {"slug": body.to, "archived": False}
            else:
                cur.execute(
                    "SELECT slug, (archived_at IS NOT NULL) AS archived "
                    "FROM sn_config.agents WHERE slug = %s",
                    (body.to,),
                )
                _agent_row = cur.fetchone()
            if _agent_row is None:
                # Check whether the token names a known user (operator / super-admin)
                # with no agent process -- the common case after the operator added
                # routing for the `claude` principal.
                cur.execute(
                    "SELECT handle, role FROM sn_config.users "
                    "WHERE handle = %s AND role IN ('operator', 'super-admin') LIMIT 1",
                    (body.to,),
                )
                _user_row = cur.fetchone()
                if _user_row:
                    raise HTTPException(
                        400,
                        f"'{body.to}' is a {_user_row['role']} with no agent inbox. "
                        "Internal messages require an agent process to deliver to. "
                        "Put task updates in a task comment -- operators read those.",
                    )
                raise HTTPException(
                    400,
                    f"no active agent '{body.to}' on this node: "
                    "internal message cannot be delivered. "
                    "Check the recipient slug against sn_config.agents.",
                )
            if _agent_row["archived"]:
                raise HTTPException(
                    400,
                    f"agent '{body.to}' is archived: internal message cannot be delivered",
                )
            # Internal agent-to-agent mail: lands as an inbound for the recipient
            # agent, woken by the dispatch watcher like any other inbound.
            ext = email.utils.make_msgid(domain=_NODE_DOMAIN)
            # Internal messaging: a new internal conversation IS a thread. The root
            # message's own Message-ID seeds thread_id (the channel-agnostic
            # conversation spine), exactly as an email thread is keyed by its root
            # Message-ID. /reply then carries this thread_id so the back-and-forth
            # accumulates as one conversation instead of disconnected one-shots.
            # WHO CAUSED THIS, written down. `provenance_chain` has been
            # in the schema since B02, commented "parent/task/flow", and was `{}`
            # on every row ever written -- declared, never populated, so the
            # journal could say what happened and never why. Two facts belong
            # here that live nowhere else: the SERVICE that carried the identity
            # when one did, and the ORIGIN the request arrived on. Without the
            # first, "alex sent this" and "the voice line sent this as alex
            # because a person was on a call" are the same row.
            _prov = {}
            if getattr(principal, "acting_service", None):
                _prov["acting_service"] = principal.acting_service
            if body.origin:
                _prov["origin"] = body.origin
            _meta = {"source": "comm-send", **({"origin": body.origin} if body.origin else {})}
            if body.needs_reply:
                _meta["needs_reply"] = True
            _thread = ext
            _question = None
            _relay_to = None
            if _desk_question is not None:
                _question = _desk_question
            elif (not _to_a_desk and (principal.role or "") == "agent"
                  and body.to != principal.username):
                # Never to itself (#9): the voice line hands work to the agent's own
                # back office with to=self, so a second hand-off on the same call
                # found the first still open, was taken as its answer, and the line
                # read the new request back to the caller as the result.
                _question = _open_question_from(cur, principal.username, body.to,
                                                principal.tenant_id)
            if _question:
                # treated as the reply it is. Same fields /reply writes.
                q_meta = _question["metadata"] or {}
                q_ext = _question["external_id"] or ""
                _thread = _question["thread_id"] or q_ext or ext
                _meta.update({"reply_to_message_id": _question["id"],
                              "in_reply_to": q_ext, "references": q_ext,
                              "origin": q_meta.get("origin"), "coerced_reply": True})
                _relay_to = None if (_answers_an_answer(q_meta)
                                     or _already_relayed(cur, _question["id"])) \
                    else _relay_desk_for(cur, q_meta.get("origin"), body.to)
                if _relay_to:
                    _meta["relayed_by_system"] = True
            cur.execute(
                "INSERT INTO sn_core.messages "
                "  (channel_type, channel_id, external_id, thread_id, direction, sender, "
                "   recipient, content, content_type, metadata, provenance_chain, tenant_id) "
                "VALUES ('internal', %s, %s, %s, 'inbound', %s, %s, %s, 'text/plain', %s, %s, %s) "
                "RETURNING id",
                (body.to, ext, _thread, principal.username, body.to, body.body,
                 Json(_meta), Json(_prov), principal.tenant_id),
            )
            new_id = cur.fetchone()["id"]
            if _question:
                cur.execute(
                    "UPDATE sn_core.message_state SET status = 'processed', "
                    "processed_at = now(), updated_at = now() WHERE message_id = %s",
                    (_question["id"],),
                )
            # A message back to the asker answers what it asked. Agents answer
            # with send as often as with reply, and a request left open after
            # that would remind an agent that has already answered and then
            # report it to the asker as silent.
            cur.execute(
                "UPDATE sn_core.message_state ms SET status = 'processed', "
                "       processed_at = now(), updated_at = now() "
                "FROM sn_core.messages m "
                "WHERE ms.message_id = m.id AND m.channel_type = 'internal' "
                "  AND m.direction = 'inbound' AND m.sender = %s AND m.recipient = %s "
                "  AND m.tenant_id = %s AND ms.status = 'awaiting_reply'",
                (body.to, principal.username, principal.tenant_id),
            )
            if _relay_to:
                _system_relay(cur, _relay_to, new_id, principal.username,
                              _meta.get("origin"), body.body, principal.tenant_id)
                # Delivered in the chat: not news for the agent that asked (see /reply).
                cur.execute(
                    "UPDATE sn_core.message_state SET status = 'processed', "
                    "processed_at = now(), updated_at = now() WHERE message_id = %s",
                    (new_id,),
                )
            if _question:
                _after_voice_answer(cur, new_id, _meta.get("origin"))
            # An agent-to-agent send is inbound FOR THE RECIPIENT, so it is
            # recorded exactly as /inbound records one. Without this the
            # internal bus was the one message path leaving no trace at all --
            # a handoff between two agents was invisible everywhere -- and the
            # note at /inbound claiming these "emit from comm.sh" was untrue:
            # the communication component only ever sees channel messages,
            # never a row core-api writes.
            emit(cur, actor=f"agent:{principal.username}",
                 event_type="message.inbound",
                 entity_type="message", entity_id=str(new_id),
                 payload={"from": principal.username, "to": body.to,
                          "channel": "internal",
                          "preview": (body.body or "")[:120]})
            result = {"ok": True, "id": new_id, "channel_type": "internal",
                      "direction": "inbound", "sender": principal.username,
                      "to": body.to, "external_id": ext}
        else:
            # External outbound: resolve the originating agent's mailbox on this
            # channel; the outbound-pass transmits the queued row.
            chan = channel or "email"
            if chan == "voice":
                # A voice caller is answered by the voice line, not by an outbound
                # row: voice has no transport that sends, so the row was queued,
                # reported as sent, and blocked or failed later where the agent
                # never saw it (#9). Say so now, with the path that works.
                raise HTTPException(
                    400,
                    "voice is not a channel an agent sends on: answer the hand-off "
                    "from the call with inbox_reply (message_id of that hand-off). "
                    "The voice line speaks the reply while the call is live, and "
                    "the node mails it to the owner if the call has ended.")
            cur.execute(
                "SELECT ac.instance_ref FROM sn_config.agents a "
                "JOIN sn_config.agent_channels ac ON ac.agent_id = a.id "
                "JOIN sn_config.channels c ON c.id = ac.channel_id "
                "WHERE a.slug = %s AND c.slug = %s AND ac.enabled",
                (principal.username, chan),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    400, f"no enabled '{chan}' mailbox for agent '{principal.username}'")
            mailbox = row["instance_ref"]
            if not (mailbox or "").strip():
                # An empty sending address is not a mailbox. Queued anyway, the row
                # went out with sender '' and the contact gate matched it to
                # whichever agent also had an empty binding (#9).
                raise HTTPException(
                    400, f"agent '{principal.username}' has no sending address on "
                         f"'{chan}': nothing was queued")
            domain = mailbox.split("@")[-1] if "@" in mailbox else _NODE_DOMAIN
            out_msgid = email.utils.make_msgid(domain=domain)
            subject = (body.subject or "").strip() or "Message from smart-node"
            out_meta = {"subject": subject, "in_reply_to": "", "references": "",
                        "source": "comm-send", "agent": principal.username}
            # Web chat is addressed by DESK, and the desk lives in thread_id.
            # A mail or telegram row needs no thread to be delivered -- the
            # address carries it -- but the browser fetches history per desk,
            # so a chat row without one can never be rendered. Setting it here
            # is what lets an agent START a conversation in the chat instead of
            # only answering inside one; leaving it null produced a row
            # that looked sent and reached nobody.
            out_thread = body.to if str(body.to).startswith("desk:") else None
            if chan == "web-chat" and out_thread is None:
                # The person was named by handle, so `to` is their chat address
                # and not a desk. Answer "which desk" the only way that cannot
                # put this agent's line into a conversation someone else is
                # having: the most recent desk THIS agent itself sat in. A desk
                # belongs to an agent -- brin keys the session
                # '<agent>:desk:<id>' -- and the owner may have several open at
                # once, so picking the latest desk by ADDRESS would have dropped
                # John's message into the chat the owner had with the finance
                # agent.
                cur.execute(
                    "SELECT s.metadata->>'conversation' AS conv "
                    "FROM sn_core.ai_sessions s "
                    "JOIN sn_config.agents a ON a.id = s.agent_id "
                    "WHERE a.slug = %s AND s.tenant_id = %s "
                    "  AND s.metadata->>'conversation' LIKE %s "
                    "ORDER BY s.last_activity_at DESC LIMIT 1",
                    (principal.username, principal.tenant_id, "%desk:%"),
                )
                drow = cur.fetchone()
                conv = (drow or {}).get("conv") or ""
                idx = conv.find("desk:")
                out_thread = conv[idx:] if idx != -1 else None
                if out_thread is None:
                    # Refuse rather than write a row nothing can render. This is
                    # the whole defect being fixed: a chat message with no desk
                    # was recorded, reported as sent, and shown to nobody.
                    raise HTTPException(
                        400,
                        f"agent '{principal.username}' has no chat desk with "
                        f"'{body.to}' yet, so there is nowhere to put this "
                        f"message; the person has to open a chat with them first")
            cur.execute(
                "INSERT INTO sn_core.messages "
                "  (channel_type, channel_id, external_id, thread_id, direction, sender, "
                "   recipient, recipients, content, content_type, metadata, tenant_id) "
                "VALUES (%s, %s, %s, %s, 'outbound', %s, %s, %s, %s, 'text/plain', %s, %s) "
                "RETURNING id",
                (chan, mailbox, out_msgid, out_thread, mailbox, body.to,
                 Json(_recipient_rows(body.to, body.cc)) if body.cc else None,
                 body.body, Json(out_meta), principal.tenant_id),
            )
            out_id = cur.fetchone()["id"]
            # Composed and queued -- message.sent, the same event the
            # communication component records when it queues a reply, carrying
            # the same fields. Transmission is a separate fact and belongs to
            # whoever puts it on the wire (message.transmitted).
            emit(cur, actor=f"agent:{principal.username}",
                 event_type="message.sent",
                 entity_type="message", entity_id=str(out_id),
                 payload={"from": mailbox, "to": body.to, "channel": chan,
                          "agent": principal.username,
                          "chars": len(body.body or "")})
            result = {"ok": True, "id": out_id, "channel_type": chan,
                      "direction": "outbound", "sender": mailbox, "to": body.to,
                      "subject": subject, "external_id": out_msgid}

    return result


# A conversation turn is stored whole. It used to be cut at 4000 characters when
# it was written, so the long messages (the owner's pasted plans and quotes, the
# agent's long answers) lived in the record as their first 4000 characters, and
# every later recap, search and audit read the cut copy: 67 of the owner's web-chat
# messages and 45 replies were stored that way, with no mark that anything was
# missing. The column is unbounded text; this cap only keeps a pasted log from
# filling the table.
_STORED_TURN_CHARS = 200_000


def _stored_turn(text) -> str:
    return (text or "")[:_STORED_TURN_CHARS]


class _VoiceTurn(BaseModel):
    role: str
    text: str


class _VoiceRecord(BaseModel):
    identity: str
    turns: list[_VoiceTurn]
    session_id: str | None = None   # Correlation: stamp ai_session_id
    agent: str | None = None   # the agent this voice door served; resolved here when absent


@router.post("/record-voice", response_model=dict,
             dependencies=[Depends(requires("internal.messages.voice.write"))])
def record_voice(body: _VoiceRecord):
    """Persist a voice session's transcript turns into the bus as channel_type
    'voice' so other channels' cross-channel view includes spoken conversations.
    Rows land pre-processed (never wake an agent). Admitted by trusted origin."""
    ident = (body.identity or "").strip()
    agent = (body.agent or "").strip()
    turns = [t for t in (body.turns or []) if (t.text or "").strip()]
    if not ident or not turns:
        return {"ok": False, "recorded": 0}
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        if not agent:
            # File under the agent that answers the shared line -- the oldest
            # enabled one. No slug is named in this file: a node names its own
            # agents and none of them is guaranteed to exist here.
            cur.execute("SELECT slug FROM sn_config.agents WHERE enabled "
                        "AND archived_at IS NULL ORDER BY id LIMIT 1")
            row0 = cur.fetchone()
            if row0 is None:
                return {"ok": False, "recorded": 0, "reason": "no agent on this node"}
            agent = row0["slug"]
        cur.execute(
            "SELECT uc.address, uc.tenant_id FROM sn_config.user_channels uc "
            "JOIN sn_config.users u ON u.id = uc.user_id "
            "WHERE uc.channel_slug = 'voice' AND uc.enabled "
            "AND (u.email = %s OR u.handle = %s OR uc.address = %s) "
            "AND u.active AND u.archived_at IS NULL LIMIT 1",
            (ident, ident.lower(), ident),
        )
        row = cur.fetchone()
        if row is None:
            return {"ok": False, "recorded": 0, "reason": "no voice channel for identity"}
        addr = row["address"]
        tenant = row["tenant_id"]
        n = 0
        for t in turns:
            is_user = (t.role or "").lower().startswith("u")
            sender = addr if is_user else agent
            recipient = agent if is_user else addr
            direction = "inbound" if is_user else "outbound"
            ext = email.utils.make_msgid(domain=f"voice.{_NODE_DOMAIN}")
            cur.execute(
                "INSERT INTO sn_core.messages "
                "(channel_type, channel_id, external_id, direction, sender, recipient, "
                " content, content_type, metadata, tenant_id) "
                "VALUES ('voice', %s, %s, %s, %s, %s, %s, 'text/plain', %s, %s) RETURNING id",
                (addr, ext, direction, sender, recipient, _stored_turn(t.text),
                 Json({"source": "voice-transcript", "voice_session": body.session_id}), tenant),
            )
            mid = cur.fetchone()["id"]
            if is_user:
                # The voice door authenticated the caller before the call
                # existed, so the proof is the session itself, not the handle.
                try:
                    stamp_identity(cur, mid, "voice", addr, proof="session",
                                   evidence={"voice_session": body.session_id})
                except Exception as _ie:  # pragma: no cover -- diagnostics only
                    log.warning("identity not stamped for voice %s: %s", mid, _ie)
            cur.execute(
                "UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = %s",
                (mid,),
            )
            n += 1
    return {"ok": True, "recorded": n}


class _VoiceTurnSeq(BaseModel):
    seq: int                            # the turn's index in the call transcript
    role: str
    text: str


class _VoiceTurnsRecord(BaseModel):
    identity: str
    session_id: str
    turns: list[_VoiceTurnSeq]
    agent: str | None = None


class _VoiceCallEnded(BaseModel):
    call: str
    spoken: list[int] = []


@router.post("/voice-call-ended", response_model=dict,
             dependencies=[Depends(requires("internal.messages.voice.write"))])
def voice_call_ended(body: _VoiceCallEnded):
    """The voice line hangs up: answers it never got to speak go to the owner (#9).

    An answer that arrives while the call is live is spoken by the line and not
    mailed; one that arrives after the call ended is mailed at /reply time. This
    closes the gap between: an answer that arrived during the call but was not
    spoken before hang-up. The line names what it did speak; every other answer
    to this call's hand-offs is mailed once. Acts only after voice.call.ended is
    recorded for the call, so it cannot pre-empt a live line.
    """
    call = (body.call or "").strip()
    if not call:
        raise HTTPException(400, "call is required")
    spoken = {int(i) for i in (body.spoken or [])}
    out = {"call": call, "mailed": [], "not_sent": [], "skipped_spoken": sorted(spoken)}
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        if not _voice_call_ended(cur, call):
            raise HTTPException(409, "voice.call.ended is not recorded for this call yet")
        cur.execute(
            "SELECT id FROM sn_core.messages "
            "WHERE channel_type = 'internal' AND direction = 'inbound' "
            "  AND metadata->'origin'->>'channel' = 'voice' "
            "  AND metadata->'origin'->>'call' = %s "
            "  AND metadata ? 'reply_to_message_id' "
            "  AND NOT EXISTS (SELECT 1 FROM sn_core.events e "
            "                   WHERE e.entity_type = 'message' "
            "                     AND e.entity_id = sn_core.messages.id::text "
            "                     AND e.event_type IN ('voice.answer.mailed', "
            "                                          'voice.answer.undelivered')) "
            "ORDER BY id",
            (call,),
        )
        for r in cur.fetchall():
            if r["id"] in spoken:
                continue
            what = _mail_late_voice_answer(cur, r["id"])
            (out["mailed"] if what == "mailed" else out["not_sent"]).append(r["id"])
    return out


@router.post("/record-voice-turns", response_model=dict,
             dependencies=[Depends(requires("internal.messages.voice.write"))])
def record_voice_turns(body: _VoiceTurnsRecord):
    """Record voice turns AS THEY HAPPEN, idempotently. One row per turn,
    keyed `voice:<session>:<seq>`, so the live per-turn write and the end-of-call
    reconciliation cannot double-write -- whichever runs first wins, the other is
    a no-op. Rows land pre-processed (voice answers live; no agent to wake).
    Admitted by trusted origin. Same identity resolution as record-voice."""
    ident = (body.identity or "").strip()
    sess = (body.session_id or "").strip()
    turns = [t for t in (body.turns or []) if (t.text or "").strip()]
    if not ident or not sess or not turns:
        return {"ok": False, "recorded": 0}
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        agent = (body.agent or "").strip()
        if not agent:
            cur.execute("SELECT slug FROM sn_config.agents WHERE enabled "
                        "AND archived_at IS NULL ORDER BY id LIMIT 1")
            row0 = cur.fetchone()
            if row0 is None:
                return {"ok": False, "recorded": 0, "reason": "no agent on this node"}
            agent = row0["slug"]
        cur.execute(
            "SELECT uc.address, uc.tenant_id FROM sn_config.user_channels uc "
            "JOIN sn_config.users u ON u.id = uc.user_id "
            "WHERE uc.channel_slug = 'voice' AND uc.enabled "
            "AND (u.email = %s OR u.handle = %s OR uc.address = %s) "
            "AND u.active AND u.archived_at IS NULL LIMIT 1",
            (ident, ident.lower(), ident),
        )
        row = cur.fetchone()
        if row is None:
            return {"ok": False, "recorded": 0, "reason": "no voice channel for identity"}
        addr = row["address"]
        tenant = row["tenant_id"]
        n = 0
        for t in turns:
            is_user = (t.role or "").lower().startswith("u")
            sender = addr if is_user else agent
            recipient = agent if is_user else addr
            direction = "inbound" if is_user else "outbound"
            ext = "voice:%s:%d" % (sess, t.seq)
            cur.execute(
                "INSERT INTO sn_core.messages "
                "(channel_type, channel_id, external_id, direction, sender, recipient, "
                " content, content_type, metadata, tenant_id) "
                "SELECT 'voice', %s, %s, %s, %s, %s, %s, 'text/plain', %s, %s "
                "WHERE NOT EXISTS (SELECT 1 FROM sn_core.messages WHERE external_id = %s) "
                "RETURNING id",
                (addr, ext, direction, sender, recipient, _stored_turn(t.text),
                 Json({"source": "voice-transcript", "voice_session": sess, "seq": t.seq}),
                 tenant, ext),
            )
            got = cur.fetchone()
            if got:
                if is_user:
                    try:
                        stamp_identity(cur, got["id"], "voice", addr, proof="session",
                                       evidence={"voice_session": sess, "seq": t.seq})
                    except Exception as _ie:   # pragma: no cover -- diagnostics
                        log.warning("identity not stamped for voice turn %s: %s",
                                    got["id"], _ie)
                cur.execute(
                    "UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = %s",
                    (got["id"],),
                )
                n += 1
    return {"ok": True, "recorded": n}


class _WebchatTurn(BaseModel):
    role: str
    text: str


class _WebchatRecord(BaseModel):
    identity: str                   # the operator identity brin holds (ctx user_id)
    turns: list[_WebchatTurn]
    session_id: str | None = None   # the conversation/desk id -> the thread key
    agent: str | None = None        # the agent this desk served; resolved when absent


@router.post("/record-webchat", response_model=dict,
             dependencies=[Depends(requires("internal.messages.voice.write"))])
def record_webchat(body: _WebchatRecord):
    """Persist a browser web-chat exchange into the bus as channel_type
    'web-chat', so the one registry holds every channel and a person is
    recalled across channels by identity -- the same contract record-voice
    gives spoken conversations. Rows land pre-processed (never wake an agent,
    never draw a second reply). Admitted by trusted origin; called best-effort
    from brin, so it must never raise on ordinary bad input -- it returns a
    count instead."""
    ident = (body.identity or "").strip()
    agent = (body.agent or "").strip()
    turns = [t for t in (body.turns or []) if (t.text or "").strip()]
    if not turns:
        return {"ok": False, "recorded": 0}
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        if not agent:
            # File under the agent that answers the shared line -- the oldest
            # enabled one. No slug is named here: a node names its own agents.
            cur.execute("SELECT slug FROM sn_config.agents WHERE enabled "
                        "AND archived_at IS NULL ORDER BY id LIMIT 1")
            row0 = cur.fetchone()
            if row0 is None:
                return {"ok": False, "recorded": 0, "reason": "no agent on this node"}
            agent = row0["slug"]
        # Resolve the person address. Prefer the web-chat user_channel keyed by
        # the identity brin passed; fall back to the node owner's web-chat
        # channel; last, a synthetic address so the row still lands rather than
        # being dropped (single-owner reality -- the browser operator IS the
        # owner). Never silently discard: an unrecorded turn is the bug.
        cur.execute(
            "SELECT uc.address, uc.tenant_id FROM sn_config.user_channels uc "
            "JOIN sn_config.users u ON u.id = uc.user_id "
            "WHERE uc.channel_slug = 'web-chat' AND uc.enabled "
            "AND (uc.address = %s OR u.email = %s OR u.handle = %s "
            "     OR (u.role = 'super-admin' AND %s = 'owner')) "
            "AND u.active AND u.archived_at IS NULL LIMIT 1",
            (ident, ident, ident.lower(), ident),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT uc.address, uc.tenant_id FROM sn_config.user_channels uc "
                "JOIN sn_config.users u ON u.id = uc.user_id "
                "WHERE uc.channel_slug = 'web-chat' AND uc.enabled "
                "AND u.active AND u.archived_at IS NULL ORDER BY uc.user_id LIMIT 1",
                (),
            )
            row = cur.fetchone()
        if row is not None:
            addr, tenant = row["address"], row["tenant_id"]
        else:
            addr, tenant = ("web:%s" % (ident or "owner")), 1
            # Self-heal: register this synthetic address so cross-channel-history
            # can find messages written here. Idempotent via ON CONFLICT.
            # user_channels has UNIQUE (user_id, channel_slug), so this is safe.
            if ident.lower() == "owner":
                cur.execute(
                    "SELECT id, tenant_id FROM sn_config.users "
                    "WHERE role = 'super-admin' AND active AND archived_at IS NULL "
                    "ORDER BY id LIMIT 1"
                )
            else:
                cur.execute(
                    "SELECT id, tenant_id FROM sn_config.users "
                    "WHERE (email = %s OR handle = %s) AND active AND archived_at IS NULL "
                    "LIMIT 1",
                    (ident, ident.lower()),
                )
            heal_row = cur.fetchone()
            if heal_row:
                heal_tid = heal_row["tenant_id"] or 1
                cur.execute(
                    "INSERT INTO sn_config.user_channels "
                    "(tenant_id, user_id, channel_slug, address, enabled) "
                    "VALUES (%s, %s, 'web-chat', %s, true) "
                    "ON CONFLICT (user_id, channel_slug) DO NOTHING",
                    (heal_tid, heal_row["id"], addr),
                )
                tenant = heal_tid
        # One thread per conversation so a desk's turns chain together and
        # read_thread can walk them; fall back to the person when brin sends no
        # session id.
        thread = ("desk:%s" % body.session_id) if body.session_id else ("web:%s" % addr)
        n = 0
        for t in turns:
            is_user = (t.role or "").lower().startswith("u")
            sender = addr if is_user else agent
            recipient = agent if is_user else addr
            direction = "inbound" if is_user else "outbound"
            ext = email.utils.make_msgid(domain=f"web.{_NODE_DOMAIN}")
            cur.execute(
                "INSERT INTO sn_core.messages "
                "(channel_type, channel_id, external_id, thread_id, direction, sender, "
                " recipient, content, content_type, metadata, tenant_id) "
                "VALUES ('web-chat', %s, %s, %s, %s, %s, %s, %s, 'text/plain', %s, %s) "
                "RETURNING id",
                (addr, ext, thread, direction, sender, recipient,
                 _stored_turn(t.text),
                 Json({"source": "web-chat", "desk": body.session_id}), tenant),
            )
            mid = cur.fetchone()["id"]
            if is_user:
                # Same door, same reasoning as the voice line: brin only reaches
                # here for a desk turn a signed-in person produced.
                try:
                    stamp_identity(cur, mid, "web-chat", addr, proof="session",
                                   evidence={"desk": body.session_id})
                except Exception as _ie:  # pragma: no cover -- diagnostics only
                    log.warning("identity not stamped for web-chat %s: %s", mid, _ie)
            cur.execute(
                "UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = %s",
                (mid,),
            )
            n += 1
    return {"ok": True, "recorded": n, "thread_id": thread}


# `metadata.source` of the messages the node itself writes to a person from an
# agent's mailbox. Not a filter on what an agent says: an agent's own mail carries
# no source, or one of the channels ("web-chat", "voice-transcript").
_SYSTEM_NOTICE_SOURCES = ("notify", "event-digest", "budget-exhaustion")


@router.get("/by-contact", response_model=dict)
def messages_by_contact(identity: str = None, contact_id: int = None, limit: int = 15,
                        thread: str = None, thread_channel: str = None,
                        max_age_days: int = None, skip_notices: int = 0,
                        principal: Principal = Depends(requires("messages.read"))):
    """The person's recent conversation across EVERY channel, newest-first capped
    at `limit`, oldest-first out. This is the earlier change's read side: cross-channel history
    is a query over the CONTACT, not over a channel handle. The contact is the one
    record of a person (contact_channels is the one handle map after this
    release), so one call gives the whole back-and-forth whether it happened on
    telegram, email, the web chat or the voice UI. brin folds it into every turn
    with a known person and voice into every call, so the agent recalls the
    person by identity instead of digging with tools.

    ASKED AS SOMEBODY. This took no principal at all: any caller named any
    person and received their whole conversation, every channel, bodies included,
    so each agent was handed the owner's conversations with its colleagues. The
    whole-node seats still see the whole contact; every other principal sees the
    part it was on an end of, by the same rule as the list (_must_scope,
    _party_clause). brin and voice call it as a service acting for the agent.

    Give it a contact_id, or an `identity` (a handle, an account email, or a
    users.handle) which it resolves to the contact through the one map + accounts.

    `max_age_days` drops rows older than that many days, per channel, before the
    per-channel cap is applied. Without it the newest rows of a channel that has
    been silent for weeks are returned as if they were recent, and the prompt
    that folds this block in shows a month-old line next to this morning's.
    Omitted, the answer is unchanged, so voice and every other caller keep
    their behaviour."""
    party, party_params = "TRUE", []
    if _must_scope(principal, False):
        party, party_params = _party_clause(principal.username)
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cid, tz = contact_id, None
        if cid is None:
            ident = (identity or "").strip()
            if not ident:
                return {"contact": None, "messages": []}
            # The person's clock (set by their Studio), for brin's [time]
            # block: known from the account even when no contact is linked yet.
            cur.execute("SELECT timezone FROM sn_config.users WHERE (email = %s OR lower(handle) = %s) "
                        "AND timezone IS NOT NULL LIMIT 1", (ident, ident.lower()))
            tz = (cur.fetchone() or {}).get("timezone")
            # resolve identity -> contact: the handle map first, then the account.
            cur.execute(
                "SELECT contact_id FROM sn_config.contact_channels "
                "WHERE handle = %s OR lower(handle) = %s LIMIT 1",
                (ident, ident.lower()),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT contact_id FROM sn_config.users "
                    "WHERE (email = %s OR lower(handle) = %s) AND contact_id IS NOT NULL "
                    "LIMIT 1",
                    (ident, ident.lower()),
                )
                row = cur.fetchone()
            if row is None:
                return {"contact": None, "timezone": tz, "messages": []}
            cid = row["contact_id"]
        # every handle this person is known by, on every channel
        cur.execute(
            "SELECT array_agg(handle) AS handles FROM sn_config.contact_channels "
            "WHERE contact_id = %s AND enabled",
            (cid,),
        )
        handles = (cur.fetchone() or {}).get("handles") or []
        if not handles:
            return {"contact": cid, "timezone": tz, "messages": []}
        # Balance ACROSS channels: the recent N per channel, so a chatty channel
        # (a voice session runs to hundreds of turns) does not crowd email/telegram
        # out of the person's view -- the same reason the search endpoint balances.
        # This history is folded into the agent's PROMPT on every turn with a known
        # person (brin, voice), so a refused letter here is the same leak as in
        # the inbox, only without the agent even asking.
        refused, refused_params = _refused_hidden(principal)
        # One channel can hold several parallel conversations -- two Studio desks
        # with the same agent, two mail threads -- and this block goes straight
        # into the prompt. Without a thread the newest rows of that channel win
        # whichever conversation they belong to, and the agent reads another one
        # as this one. Named thread: on THAT channel only that thread.
        # Every other channel is untouched, which is the point of the block.
        thread_sql, thread_params = "", []
        if thread and thread_channel:
            thread_sql = (" AND NOT (m.channel_type = %s "
                          "          AND m.thread_id IS DISTINCT FROM %s) ")
            thread_params = [thread_channel, thread]
        age_sql, age_params = "", []
        if max_age_days and max_age_days > 0:
            age_sql = " AND m.received_at >= now() - make_interval(days => %s) "
            age_params = [min(int(max_age_days), 3650)]
        # The node's own notices (a task assigned, a mention, a digest, a budget
        # warning) go out from the agent's mailbox, so they read as that agent's
        # mail. On one mailbox 78 of 100 outbound mails in three days were such
        # notices, and "the newest twelve email lines" were all of them: the owner's
        # real thread had been pushed out of the recap. Only the recap asks for
        # them to be left out; a search still finds them.
        notice_sql = ""
        if skip_notices:
            notice_sql = (" AND COALESCE(m.metadata->>'source', '') NOT IN ("
                          + ", ".join("'%s'" % s for s in _SYSTEM_NOTICE_SOURCES) + ") ")
        cur.execute(
            "SELECT channel_type, direction, sender, recipient, content, received_at "
            "FROM (SELECT m.channel_type, m.direction, m.sender, m.recipient, "
            "             m.content, m.received_at, m.id, "
            "             ROW_NUMBER() OVER (PARTITION BY m.channel_type "
            "                                ORDER BY m.id DESC) AS rn "
            "      FROM sn_core.messages m "
            "      JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "      WHERE (m.sender = ANY(%s) OR m.recipient = ANY(%s)) AND " + party +
            "        AND " + refused + thread_sql + age_sql + notice_sql + ") t "
            "WHERE rn <= %s ORDER BY id DESC",
            (handles, handles, *party_params, *refused_params, *thread_params,
             *age_params, max(4, min(limit, 12))),
        )
        rows = list(reversed(cur.fetchall()))
        cur.execute("SELECT display_name FROM sn_config.contacts WHERE id = %s", (cid,))
        nm = cur.fetchone()
    return {"contact": cid, "timezone": tz,
            "display_name": nm["display_name"] if nm else None,
            "messages": rows}


# A search of a dozen words is a pasted paragraph; the rest would only slow the query.
_SEARCH_MAX_WORDS = 8


def _min_words(n: int) -> int:
    """How many of a query's words an item must hold. A query of one or two words
    needs them all; a longer one, as an agent types it ("candidates outreach list
    document created"), needs about three in five, because one word that the text
    happens to put differently must not hide it. Items holding more of the words
    rank first."""
    return n if n <= 2 else -(-3 * n // 5)


def _search_words(q: str | None) -> list[str]:
    """The words of a text query as ILIKE patterns: every one must occur in a
    message, in any order. `%`, `_` and a backslash are text, not wildcards."""
    out = []
    for w in (q or "").split()[:_SEARCH_MAX_WORDS]:
        out.append("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return out


def _stem_patterns(cur, q: str | None) -> list[str]:
    """Like _search_words, but each word is cut back to its stem first, so that a
    query in one grammatical form finds the other forms: "contacts" finds
    "contact", "publishing" finds "publish". The stem comes from the database's
    own English text-search dictionary and is matched as the START of a word (a regular expression, `\\m` + stem): a
    full text-search match over every message took three seconds, and a plain
    substring let a short word match inside a longer one ("eta" inside "beta"). A
    stem that is not the start of the word, or a word the dictionary drops, is used
    as typed. The patterns are for the `~*` operator."""
    out = []
    for w in (q or "").split()[:_SEARCH_MAX_WORDS]:
        stem = w
        try:
            cur.execute("SELECT lexeme FROM unnest(to_tsvector('english'::regconfig, %s)) LIMIT 1", (w,))
            row = cur.fetchone()
            cand = (row or {}).get("lexeme") or ""
            if len(cand) >= 3 and w.lower().startswith(cand):
                stem = w[:len(cand)]
        except Exception:  # a dictionary problem must not cost the search
            stem = w
        out.append("\\m" + re.sub(r"[^\w]", lambda m: "\\" + m.group(0), stem))
    return out


class _MessageSearch(BaseModel):
    identity: str = ""                  # who to look up: a handle, an email, a users.handle
    q: str | None = None                # words to find in the text, any order; all must occur
    limit: int = 20                     # how many messages ("look deeper" -> raise it)
    since: str | None = None            # ISO date/datetime lower bound ("since yesterday")
    until: str | None = None            # ISO date/datetime upper bound
    channel: str | None = None          # restrict to one channel_type, e.g. "telegram"


@router.post("/search", response_model=dict)
def messages_search(body: _MessageSearch,
                    principal: Principal = Depends(requires("messages.read"))):
    """Search ONE person's conversation across every channel, with parameters --
    or, with `q`, find messages by the words in them. With `q` and no `identity` it
    searches every conversation the caller is a party to (the whole-node seats see
    all); with both it searches that person's. Words may come in any order; one or
    two words must all occur, a longer query needs about three in five of them, and
    the messages holding the most words come first. This is the catalog-tool form of by-contact, callable by ANY agent from
    the tool catalogue, in the voice line and the desk chat alike. Resolves
    `identity` (a channel handle, an account email, or a users.handle) to the one
    contact, then returns that contact's messages, newest-first capped at `limit`,
    returned oldest-first. `since`/`until` bound the time window ("look at
    yesterday"); `channel` restricts to one channel_type ("on telegram"). Params
    arrive as the JSON body (the catalog api dispatcher sends them so).

    Asked AS the agent: brin's catalog dispatcher sends the authenticated service
    proof for core-api tools. It used to take no principal, so any agent could
    read any person's conversations with every colleague. The whole-node seats
    see the whole contact; every other principal the part it was on an end of."""
    ident = (body.identity or "").strip()
    words = _search_words(body.q)
    if not ident and not words:
        return {"contact": None, "messages": []}
    cid = None
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        where = []
        args = {"lim": max(1, min(body.limit, 100))}
        if ident:
            cur.execute(
                "SELECT contact_id FROM sn_config.contact_channels "
                "WHERE handle = %s OR lower(handle) = %s LIMIT 1",
                (ident, ident.lower()),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT contact_id FROM sn_config.users "
                    "WHERE (email = %s OR lower(handle) = %s) AND contact_id IS NOT NULL LIMIT 1",
                    (ident, ident.lower()),
                )
                row = cur.fetchone()
            if row is None:
                # Last resort: a display name. Agents often pass the person's name
                # rather than a handle; resolve it if it is unambiguous.
                cur.execute(
                    "SELECT id AS contact_id FROM sn_config.contacts "
                    "WHERE lower(display_name) = lower(%s) OR display_name ILIKE %s "
                    "ORDER BY id LIMIT 1",
                    (ident, "%" + ident + "%"),
                )
                row = cur.fetchone()
            if row is None:
                return {"contact": None, "messages": []}
            cid = row["contact_id"]
            cur.execute(
                "SELECT array_agg(handle) AS handles FROM sn_config.contact_channels "
                "WHERE contact_id = %s AND enabled",
                (cid,),
            )
            handles = (cur.fetchone() or {}).get("handles") or []
            if not handles:
                return {"contact": cid, "messages": []}
            where.append("(m.sender = ANY(%(h)s) OR m.recipient = ANY(%(h)s))")
            args["h"] = handles
        score_sql = None
        if words:
            pats = _stem_patterns(cur, body.q)
            conds = []
            for i, w in enumerate(pats):
                conds.append(f"(CASE WHEN m.content ~* %(w{i})s THEN 1 ELSE 0 END)")
                args[f"w{i}"] = w
            score_sql = " + ".join(conds)
            where.append(f"({score_sql}) >= {_min_words(len(pats))}")
        if _must_scope(principal, False):
            # The list's rule, bound by name like the rest of this query.
            _party, _ = _party_clause(principal.username, "%(me)s")
            where.append(_party)
            args["me"] = principal.username
        _refused, _ = _refused_hidden(principal, ph="%(me)s")
        if _refused != "TRUE":
            where.append(_refused)
            args["me"] = principal.username
        if (body.channel or "").strip():
            where.append("m.channel_type = %(ch)s")
            args["ch"] = body.channel.strip()
        _since = (body.since or "").strip()
        if _since:
            where.append("m.received_at >= %(since)s")
            args["since"] = _since
        _until = (body.until or "").strip()
        if _until:
            # A bare date as an upper bound means the WHOLE of that day. Compared
            # as a timestamp it is midnight (day start), which drops everything
            # that day -- so "until yesterday" silently excluded yesterday's
            # messages. Extend a bare date to end-of-day.
            if len(_until) == 10 and _until.count("-") == 2:
                _until = _until + " 23:59:59"
            where.append("m.received_at <= %(until)s")
            args["until"] = _until
        if words or (body.channel or "").strip():
            # A channel was named, or words were given -> a flat newest-first window.
            # Balancing across channels would hide a match in a quiet one. Floor the
            # limit: the model sometimes passes a nonsensical 1.
            cur.execute(
                "SELECT m.channel_type, m.direction, m.sender, m.recipient, m.content, "
                "       m.received_at "
                "FROM sn_core.messages m "
                "JOIN sn_core.message_state ms ON ms.message_id = m.id "
                "WHERE " + " AND ".join(where) +
                " ORDER BY " + (f"({score_sql}) DESC, " if score_sql else "") + "m.id DESC LIMIT %(lim)s",
                {**args, "lim": max(5, min(body.limit or 20, 100))},
            )
        else:
            # No channel named -> BALANCE across channels, so a chatty one (a voice
            # session can run to hundreds of turns) does not crowd email/telegram
            # out of the window: return the recent N PER channel. This is what makes
            # "what about email" answerable even when the model forgets to set
            # channel=email. Floored, because the model sometimes passes limit=1.
            cur.execute(
                "SELECT channel_type, direction, sender, recipient, content, received_at "
                "FROM (SELECT m.channel_type, m.direction, m.sender, m.recipient, "
                "             m.content, m.received_at, m.id, "
                "             ROW_NUMBER() OVER (PARTITION BY m.channel_type "
                "                                ORDER BY m.id DESC) AS rn "
                "      FROM sn_core.messages m "
                "      JOIN sn_core.message_state ms ON ms.message_id = m.id "
                "      WHERE " + " AND ".join(where) + ") t "
                "WHERE rn <= %(perch)s ORDER BY id DESC",
                {**args, "perch": max(6, min(body.limit or 20, 15))},
            )
        # with words the rows come best match first; without, oldest first as ever
        rows = cur.fetchall() if score_sql else list(reversed(cur.fetchall()))
        nm = None
        if cid is not None:
            cur.execute("SELECT display_name FROM sn_config.contacts WHERE id = %s", (cid,))
            nm = cur.fetchone()
    return {"contact": cid,
            "display_name": nm["display_name"] if nm else None,
            "count": len(rows),
            "messages": rows}


# ------------------------------------------------------------------- recall
#
# One question, every place an agent keeps what it knows. An agent that looks for
# a thing by habit asks the one place it thinks of, finds something plausible and
# stops; the list it was after lived in another place. This returns the candidates
# from tasks, conversations and memory side by side, says which sources it read and
# which it did not, and widens its time window by itself when a narrow one is empty.

_RECALL_WINDOWS_DAYS = (3, 14, 90, None)    # None: no bound
_RECALL_ENOUGH = 3                          # hits in tasks + messages that stop the widening
_RECALL_SNIPPET = 240
_RECALL_MEMORY_SKIP_TAGS = ("type:handoff", "type:session-snapshot")


def _snippet(text, words, n: int = _RECALL_SNIPPET) -> str:
    """The text around the first of the words that occurs in it, on one line."""
    t = " ".join(str(text or "").split())
    low = t.lower()
    pos = -1
    for w in words:
        p = low.find(str(w).lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return t[:n] + ("..." if len(t) > n else "")
    start = max(0, pos - n // 3)
    return ("..." if start else "") + t[start:start + n] + ("..." if start + n < len(t) else "")


def _pick_window(ages_days, enough: int = _RECALL_ENOUGH):
    """The narrowest window (in days, None = all time) that holds `enough` hits."""
    for w in _RECALL_WINDOWS_DAYS:
        if w is None or sum(1 for a in ages_days if a <= w) >= enough:
            return w
    return None


class _Recall(BaseModel):
    q: str
    limit: int = 5                      # hits per source


def _recall_memory(slug: str, q: str, limit: int, words, stems) -> dict:
    """The agent's OWN mesh workspace, in two lists. `hits` are notes that contain
    every word (as a stem), the same evidence as the other sources. `related` are
    the nearest by meaning: the embedding has no score that separates a real match
    from nonsense (0.78 for gibberish, 0.82 for a real one), so they are offered as
    leads and never counted as a finding. Session handoff notes are left out of
    both: they summarise earlier turns and crowd out documents."""
    from lib import project_state as _ps
    if not _ps.MESH_URL:
        return {"status": "not_read", "reason": "mesh is not configured", "hits": [], "related": []}
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute("SELECT mesh_workspace FROM sn_config.agents WHERE slug = %s", (slug,))
        row = cur.fetchone()
    ws = (row or {}).get("mesh_workspace")
    if not ws:
        return {"status": "not_read", "reason": "the agent has no mesh workspace", "hits": [], "related": []}

    def mesh(method, path, payload=None):
        req = urllib.request.Request(
            _ps.MESH_URL + path, method=method,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"X-Workspace": ws, "Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))

    def skip(d):
        return any(t in (d.get("tags") or []) for t in _RECALL_MEMORY_SKIP_TAGS)

    def hit(d):
        return {"guid": d.get("guid"), "created_at": d.get("created_at"),
                "tags": [t for t in (d.get("tags") or []) if t.startswith(("type:", "project:"))][:3],
                "text": _snippet(d.get("content"), words)}

    hits, related, seen = [], [], set()
    try:
        if stems:
            key = max(stems, key=len)
            data = mesh("GET", "/keyword?" + urllib.parse.urlencode({"q": key, "limit": 100}))
            for d in data.get("results") or []:
                low = (d.get("content") or "").lower()
                if skip(d) or sum(1 for st in stems if st.lower() in low) < _min_words(len(stems)):
                    continue
                seen.add(d.get("guid"))
                hits.append(hit(d))
                if len(hits) >= limit:
                    break
        data = mesh("POST", "/search", {"query": q, "limit": 25})
        for d in data.get("results") or []:
            if skip(d) or d.get("guid") in seen:
                continue
            related.append(hit(d))
            if len(related) >= 3:
                break
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as ex:
        log.warning("recall: mesh read failed: %s", ex)
        return {"status": "not_read", "reason": "mesh did not answer", "hits": [], "related": []}
    return {"status": "read", "hits": hits, "related": related,
            "related_note": "nearest by meaning, not word matches: leads, not findings"}


@router.post("/recall", response_model=dict)
def messages_recall(body: _Recall, principal: Principal = Depends(requires("messages.read"))):
    """Find what you know about something, in every place at once.

    Words, any order. Searches tasks (title, description, comments), the caller's
    conversations (the party rule of /search applies) and the caller's own memory.
    Documents are not searched yet and the answer says so. The time window starts
    at 3 days and widens to 14 and 90 days, then to all time, until a few hits
    are found; the answer names the window that produced them. A source that could
    not be read is reported as such: "nothing found" is only true when every
    source that was searched was read."""
    from lib import project_state as _ps
    q = (body.q or "").strip()
    if not q:
        raise HTTPException(status_code=422, detail="q must not be blank")
    limit = max(1, min(int(body.limit or 5), 20))
    words = q.split()[:_SEARCH_MAX_WORDS]
    now = datetime.now(timezone.utc)

    def age_days(ts):
        t = ts if isinstance(ts, datetime) else _ps._parse_ts(ts)
        if t is None:
            return 10 ** 6
        return (now - (t if t.tzinfo else t.replace(tzinfo=timezone.utc))).total_seconds() / 86400.0

    sources = {}
    # memory goes out first and runs beside the other two: it waits on an embedding,
    # the slowest of the three, and the call as a whole should cost the slowest
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    mem_future = None
    if getattr(principal, "actor_kind", None) == "agent":
        with cursor() as cur:
            cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
            stems = [re.sub(r"\\(.)", r"\1", p[1:-1]) for p in _stem_patterns(cur, q)]
        mem_future = pool.submit(_recall_memory, principal.username, q, limit, words, stems)
    # tasks
    rows = _ps._planner_get("api/v1/tasks/search?" + urllib.parse.urlencode({"q": q, "limit": 50}))
    if rows is None:
        sources["tasks"] = {"status": "not_read", "reason": "the planner did not answer", "hits": []}
        task_hits = []
    else:
        task_hits = [{"id": t.get("id"), "title": t.get("title"), "status": t.get("status"),
                      "assignee": t.get("assignee"), "updated_at": t.get("updated_at"),
                      "_age": age_days(t.get("updated_at"))} for t in rows if isinstance(t, dict)]
    # conversations, newest first, bounded later so one query serves every window
    try:
        found = messages_search(_MessageSearch(q=q, limit=100), principal)
        msg_hits = [{"channel": m["channel_type"], "direction": m["direction"], "from": m["sender"],
                     "to": m["recipient"], "at": str(m["received_at"]),
                     "text": _snippet(m["content"], words), "_age": age_days(m["received_at"])}
                    for m in (found.get("messages") or [])]
        msg_status = None
    except Exception as ex:  # a failed source is reported, not hidden
        log.warning("recall: conversations read failed: %s", ex)
        msg_hits, msg_status = [], "not_read"

    window = _pick_window([h["_age"] for h in task_hits] + [h["_age"] for h in msg_hits])

    def within(hits):
        keep = [h for h in hits if window is None or h["_age"] <= window]
        for h in keep:
            h.pop("_age", None)
        return keep

    if rows is not None:
        kept = within(task_hits)
        sources["tasks"] = {"status": "read", "count": len(kept), "hits": kept[:limit]}
    if msg_status is None:
        kept = within(msg_hits)
        sources["messages"] = {"status": "read", "count": len(kept), "hits": kept[:limit]}
    else:
        sources["messages"] = {"status": "not_read", "reason": "the conversation search failed", "hits": []}

    if mem_future is not None:
        try:
            sources["memory"] = mem_future.result(timeout=20)
        except Exception as ex:  # a failed source is reported, not hidden
            log.warning("recall: memory read failed: %s", ex)
            sources["memory"] = {"status": "not_read", "reason": "memory could not be read",
                                 "hits": [], "related": []}
    else:
        sources["memory"] = {"status": "not_read", "reason": "memory is read per agent and the caller is not one",
                             "hits": []}
    pool.shutdown(wait=False)
    sources["documents"] = {"status": "not_searched",
                            "note": "documents are not searched by this call; list the likely folder"}

    read = [k for k, v in sources.items() if v["status"] == "read"]
    unread = [k for k, v in sources.items() if v["status"] != "read"]
    total = sum(len(sources[k]["hits"]) for k in read)
    summary = "%d candidate(s) from %s; window: %s." % (
        total, ", ".join(read) or "no source", "all time" if window is None else "%d days" % window)
    if unread:
        summary += " Not searched or not read: %s. Do not conclude that something does not exist." % ", ".join(unread)
    return {"q": q, "window_days": window, "summary": summary, "sources": sources}


def _desk_of_message(request: Request) -> str:
    """The desk a message is addressed to: the object of the delivery permission.

    The request names a message, not a desk, so the desk is read from the row.
    A message that does not exist, or is not addressed to a desk, gets the
    answer the handler gives for it; there is no desk to check a grant against.
    """
    try:
        mid = int(request.path_params["mid"])
    except (KeyError, ValueError):
        raise HTTPException(422, "message id must be an integer")
    with cursor() as cur:
        cur.execute("SELECT recipient FROM sn_core.messages WHERE id = %s", (mid,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "message not found")
    recipient = str(row["recipient"] or "")
    if not recipient.startswith("desk:"):
        raise HTTPException(400, "not a desk message")
    return recipient


#: One chat turn handed to an agent is cut here: the agent asks for what it
#: needs, and a pasted log in somebody's chat should not fill its session.
_CHAT_TURN_CHARS = 4000


@router.get("/{message_id}/chat-history", response_model=dict)
def chat_history_for_message(
    message_id: int,
    limit: int = 20,
    before: int | None = None,
    principal: Principal = Depends(requires("messages.inbox.read")),
):
    """The Studio chat a mention came from, for the agent that was mentioned.

    Bound to the MESSAGE, not to a desk id the caller names: the caller must be
    the recipient of a chat mention (metadata.kind 'chat', reason 'mentioned')
    still visible to it, and it reads that mention's desk and no other. The
    mention grants nothing else: no attachments, no other chat of the person,
    no write. The turns are the desk history (GET /api/desks/{id}/history),
    paged from the end: `limit` turns ending before index `before` (default:
    the latest), oldest first, each with its index.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit must be 1..100")
    with cursor() as cur:
        if _visible_message(cur, principal, message_id, "inbound") is None:
            raise HTTPException(404, "message not found")
        _refuse_outside_wake(cur, principal, message_id)
        cur.execute(
            "SELECT m.recipient, m.metadata, m.tenant_id, d.title "
            "FROM sn_core.messages m "
            "LEFT JOIN sn_core.desks d ON 'desk:' || d.id::text = m.metadata->'origin'->>'conversation' "
            "WHERE m.id = %s",
            (message_id,),
        )
        m = cur.fetchone()
    meta = (m or {}).get("metadata") or {}
    conv = _chat_mention_desk(meta)
    if m is None or conv is None:
        raise HTTPException(400, "message %s is not a mention in a Studio chat" % message_id)
    if m["recipient"] != principal.username and (principal.role or "") not in _READS_WHOLE_NODE:
        raise HTTPException(404, "message not found")
    # Imported here: routes.desks is a sibling router, and this is its one reader.
    from types import SimpleNamespace
    from routes.desks import desk_history
    turns = desk_history(conv[len("desk:"):],
                         SimpleNamespace(tenant_id=m["tenant_id"]))["messages"]
    total = len(turns)
    end = total if before is None else max(0, min(int(before), total))
    start = max(0, end - limit)
    out = []
    for i in range(start, end):
        t = turns[i]
        content = t.get("content") or ""
        if len(content) > _CHAT_TURN_CHARS:
            content = content[:_CHAT_TURN_CHARS] + "\n[... cut, %d characters in all]" % len(content)
        out.append({"index": i, "role": t.get("role"), "content": content,
                    "created_at": t.get("created_at")})
    return {
        "chat": conv, "chat_title": m.get("title"), "chat_agent": meta.get("desk_agent"),
        "person": meta.get("studio_user"), "total": total, "turns": out,
        "older": ("call again with before=%d for older turns" % start) if start > 0 else None,
        "note": ("role 'user' is the person; role 'assistant' is the chat's agent, or a "
                 "colleague's answer when it starts with 'Answer from <name>'."),
    }


@router.post("/{mid}/deliver-desk", response_model=dict)
def deliver_desk(
    mid: int,
    principal: Principal = Depends(
        requires("messages.deliver_desk", object=_desk_of_message)),
):
    """The delivery decision for one row addressed to desk:<id>. Called by
    the message watcher; idempotent. Deliver iff the sender is the desk's own agent,
    the system relay, or not an agent at all (a person, the operator). Otherwise the
    row is blocked and never shown. Status and event are written in one transaction
    (the row is the proof)."""
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "SELECT m.id, m.sender, m.recipient, ms.status "
            "FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "WHERE m.id = %s FOR UPDATE OF ms",
            (mid,),
        )
        m = cur.fetchone()
        if m is None:
            raise HTTPException(404, "message not found")
        if not str(m["recipient"] or "").startswith("desk:"):
            raise HTTPException(400, "not a desk message")
        if m["status"] not in ("new", "routed"):
            return {"decision": "noop", "status": m["status"]}
        exists, owner = _desk_owner(cur, m["recipient"])
        sender = m["sender"] or ""
        cur.execute("SELECT 1 FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
                    (sender,))
        sender_is_agent = cur.fetchone() is not None
        if not exists:
            decision, reason = "deny", "no-desk"
        elif sender == owner or sender == "system" or not sender_is_agent:
            decision, reason = "deliver", "own-desk" if sender == owner else (
                "system-relay" if sender == "system" else "person")
        else:
            decision, reason = "deny", "not_owner"
        status = "processed" if decision == "deliver" else "blocked"
        cur.execute(
            "UPDATE sn_core.message_state SET status = %s, processed_at = now(), "
            "updated_at = now() WHERE message_id = %s",
            (status, mid),
        )
        emit(cur, actor="component:message-watcher",
             event_type="message.delivered" if decision == "deliver" else "message.routing_denied",
             entity_type="desk", entity_id=m["recipient"],
             payload={"message_id": mid, "from": sender, "desk_agent": owner, "reason": reason})
    return {"decision": decision, "status": status, "reason": reason}


def sweep_reply_obligations(cur):
    """Remind agents of requests they saw and left unanswered; report the silent ones.

    Runs inside the Runner's poll (agents_with_pending_work), in its transaction,
    because that is the one place that already decides who is woken. It does not
    wake anyone itself: a reminder is an ordinary internal message, and the poll
    that follows wakes the agent for it the way it wakes it for any other, with
    the reason in its inbox.

    An agent in the middle of a run is left alone and the attempt is not spent:
    it may be writing the answer. The reminder goes out once it is free and the
    request is still open. When the attempts are used up, or the recipient no
    longer exists as an agent, the sender is told and the request is closed with
    the tag obligation:unanswered.
    """
    cfg = _reply_obligation_config()
    if cfg is None:
        return
    window, max_attempts = cfg
    cur.execute(
        "SELECT m.id, m.sender, m.recipient, m.content, m.tenant_id, m.received_at, m.metadata, ms.wake_attempts, "
        "       (a.slug IS NOT NULL) AS reachable, COALESCE(a.title, m.recipient) AS recipient_name "
        "FROM sn_core.message_state ms "
        "JOIN sn_core.messages m ON m.id = ms.message_id "
        "LEFT JOIN sn_config.agents a ON a.slug = m.recipient AND a.enabled "
        "                            AND a.archived_at IS NULL "
        "LEFT JOIN sn_core.agent_runtime r ON r.agent_slug = m.recipient "
        "WHERE ms.status = 'awaiting_reply' AND ms.wake_next_at <= now() "
        "  AND (a.slug IS NULL OR NOT (COALESCE(r.status, 'free') = 'busy' "
        "       AND (r.lease_until IS NULL OR r.lease_until >= now()))) "
        "ORDER BY m.id FOR UPDATE OF ms SKIP LOCKED")
    for row in cur.fetchall():
        mid = row["id"]
        if row["reachable"] and row["wake_attempts"] < max_attempts:
            attempt = row["wake_attempts"] + 1
            _tell_agent(
                cur, row["recipient"],
                "Reminder from the node (attempt %d of %d): message #%d from %s asked "
                "you for a reply and has none yet. Answer it with inbox_reply("
                "to_message_id=%d), or close it with inbox_dismiss(message_id=%d) if no "
                "answer is owed." % (attempt, max_attempts, mid, row["sender"], mid, mid),
                mid, row["tenant_id"], "reply-obligation")
            cur.execute(
                "UPDATE sn_core.message_state SET wake_attempts = %s, "
                "       wake_next_at = now() + make_interval(secs => %s), updated_at = now() "
                "WHERE message_id = %s AND status = 'awaiting_reply'",
                (attempt, window, mid))
            continue
        cur.execute(
            "UPDATE sn_core.message_state SET status = 'processed', processed_at = now(), "
            "       updated_at = now(), tags = array_append(tags, 'obligation:unanswered') "
            "WHERE message_id = %s AND status = 'awaiting_reply'",
            (mid,))
        cur.execute(
            "SELECT 1 FROM sn_config.agents WHERE slug = %s AND enabled "
            "AND archived_at IS NULL", (row["sender"],))
        sender_is_agent = cur.fetchone() is not None
        why = ("after %d wake(s)" % row["wake_attempts"] if row["reachable"]
               else "and is no longer an active agent on this node")
        meta = row["metadata"] or {}
        chat = _relay_desk_for(cur, meta.get("origin"), row["sender"], meta, answering=row["recipient"]) \
            if row["sender"] == "system" else None
        if chat:
            # The system asked on a person's behalf: they are the one left waiting.
            _system_relay(cur, chat, mid, row["recipient"], meta.get("origin"),
                          "Not done, no answer came: %s" % (meta.get("reminder_text") or "message #%d" % mid),
                          row["tenant_id"], own=True)
        elif row["sender"] == "system":
            # Its chat is gone, and the person asked for this.
            tell_owner(cur, row["tenant_id"],
                       "%s did not do this, and its chat was deleted: %s"
                       % (row["recipient_name"], meta.get("reminder_text") or row["content"][:200]),
                       "overdue:%d" % mid)
        elif sender_is_agent:
            _tell_agent(
                cur, row["sender"],
                "%s did not answer your message #%d (sent %s) %s. The node has stopped "
                "reminding it. Decide what happens next: ask again, ask someone else, or "
                "tell whoever is waiting on this that there is no answer."
                % (row["recipient"], mid, row["received_at"].strftime("%Y-%m-%d %H:%M %Z"),
                   why),
                mid, row["tenant_id"], "reply-obligation")
        else:
            # A sender with no inbox (the operator seat) cannot be told by message.
            tell_owner(cur, row["tenant_id"],
                       "%s did not answer: %s" % (row["recipient_name"], row["content"][:200]),
                       "overdue:%d" % mid)
        emit(cur, actor="component:core-api", event_type="message.reply_overdue",
             entity_type="message", entity_id=str(mid),
             payload={"from": row["sender"], "to": row["recipient"],
                      "attempts": row["wake_attempts"], "recipient_active": row["reachable"],
                      "sender_told": sender_is_agent})
