"""Fast-path dispatch layer -- Smart Node communication component.

Live-channel fast-path. Channel adapters only ingest: they land inbound rows
in the `messages` registry and stop. For the LIVE channels (a person is
waiting) this loop closes the gap synchronously: it reads the new row and hands
the REAL message text straight to brin -- one assembled call to the Gateway,
not a wake + inbox tool-loop.

What is fast here is the THINKING, not the sending. This loop queues the reply;
it never puts bytes on the wire. `send_outbound` (the timer) is the SOLE
transmitter, for every channel alike -- see the note below.

Loop, every DISPATCH_INTERVAL seconds:
  1. scan `message_state` for status='new' + direction='inbound' on a FAST channel
  2. advance new -> routed
  3. resolve the channel's bound agent (agent_channels.instance_ref = recipient)
  4. ask the contact gate whether this sender may wake that agent; if
     not, the row goes to `blocked` and the model is never called
  5. POST the real text to brin -- /api/message {agent, message, conversation_id}
  6. QUEUE brin's reply as an outbound `messages` row on the originating
     channel, unless this turn explicitly sent and committed that channel reply
     through messaging already
  7. advance routed -> processed (or failed, with the error recorded)

ONE EXIT. Every deliverer here records an outbound row and
returns; nothing transmits. the outbound safety rule makes that load-bearing rather than
stylistic: "the agent does NOT send physically -- it drops the reply in the
outbox; delivery is the separate service's job. So the only thing we control is
WHAT lands in the outbox and WHETHER it is released." A second transmit path
leaves the outbound guard and the egress allowlist with nowhere to stand, which
is how a node with an empty allowlist mailed seven external addresses.
The latency this cost is ~2s of a 5-10s reply: own measurement put 47s
of a 57s round trip in the wake-and-inbox loop, which is the part this loop
still removes.

PARTITION: this loop OWNS the FAST channels;
core-api /with-pending-work excludes them, so the Runner never wakes an agent
for the same row. Email + system/task messages are NOT fast channels and stay
on the Runner wake-loop, untouched.

FAST_CHANNELS is a comma list (e.g. "telegram"); empty = this loop is idle and
every channel keeps the wake-loop (a clean, reversible off-switch).

brin is reached over loopback HTTP; URL + API key from the environment
(BRIN_API_URL, BRIN_API_KEY).

Run as a daemon:  python -m dispatch
"""
import contextlib
import uuid
import email.utils
import threading
import time

import requests

from common import contacts, db, mailrules
from common.config import LOG, get, require

# Adapters are imported LAZILY, inside their deliverer.
#
# Importing them here binds every channel's configuration at process start:
# adapters.telegram.transport builds its API URL from require("TG_TOKEN") at
# import time, so a node with only email -- mail-node's shape -- cannot start the
# dispatch layer at all. The channel is optional in the installer; it has to be
# optional in the runtime too, or "optional" means nothing.
#
# A channel that is genuinely misconfigured still fails, but only when a message
# for it arrives, and _handle records that against the row instead of
# crash-looping the whole daemon.
#
# This was fixed once, lost when dispatch was rewritten, and found again by
# deploying to a node that has no Telegram. If you add a channel here,
# import it in its deliverer, not at module level.

_TAG = "fast-dispatch"
_TAG_ACTOR = "component:fast-dispatch"


def _at_stage(exc, stage):
    """Tag an exception with the stage it died at, and return it to be raised.

    One message.failed is emitted per dead message, in the scan loop, and it
    carries this stage. 'route', 'think', 'deliver' and 'transmit' fail for
    entirely unrelated reasons and are fixed in different components -- which of
    them it was is the first question anyone asks and the one message_state
    cannot answer."""
    exc.sn_stage = stage
    return exc

BRIN_API_URL = get("BRIN_API_URL", "http://127.0.0.1:8101").rstrip("/")
DISPATCH_INTERVAL = int(get("DISPATCH_INTERVAL", "1"))

# How long a person's message may take to answer: the whole turn, not a slice
# of it. brin is asked to have the gateway wait this long before it answers
# 202 "still working", and this loop waits a minute more for brin. It used to
# give up after 120 s, below the gateway's own 285 s window, so every turn
# longer than two minutes was marked failed while the agent went on working,
# and its answer had nowhere to go (a node in the field, 2026-10-14: twelve
# times in one evening). The gateway's turn limit ends a turn that runs past
# this, so the wait is bounded by the same number.
TURN_WAIT = int(get("DISPATCH_TURN_WAIT", "3600"))

# Chat channels: what a person writes while the agent is busy is read in the
# agent's next turn, together, the way a person reads a chat after a call.
# Email is not batched: each letter is answered on its own thread.
_BATCHED_CHANNELS = {"telegram"}

# Live channels this loop owns. Empty -> idle (everything stays on the wake-loop).
FAST_CHANNELS = [c.strip() for c in get("FAST_CHANNELS", "").split(",") if c.strip()]

# Inbound work queue: every 'new' inbound message on a FAST channel, oldest
# first. Outbound rows are excluded (direction filter) so the loop never
# answers its own replies.
SCAN_SQL = """
    SELECT ms.message_id, m.channel_type, m.sender, m.recipient, m.content,
           m.metadata, m.thread_id, m.external_id, m.channel_id
      FROM message_state ms
      JOIN messages m ON m.id = ms.message_id
     WHERE ms.status = 'new'
       AND (ms.wake_next_at IS NULL OR ms.wake_next_at <= now())
       AND m.direction = 'inbound'
       AND m.channel_type = ANY(%s)
     ORDER BY ms.message_id
"""

# Which DB agent owns this channel instance. Same rule core-api uses for the
# email/chat branch of /with-pending-work: the inbound row's `recipient` is the
# channel instance_ref; agent_channels maps it to the agent. One routing rule,
# no per-message column to maintain.
RESOLVE_SQL = """
    SELECT a.slug
      FROM sn_config.agent_channels ac
      JOIN sn_config.agents a ON a.id = ac.agent_id
      JOIN sn_config.channels c ON c.id = ac.channel_id
     WHERE ac.instance_ref = %s AND c.slug = %s AND ac.enabled
       AND a.enabled AND a.archived_at IS NULL
"""

# The agent bound to this instance when that agent is STOPPED (disabled, not
# archived). Its messages wait for it instead of being dead-lettered.
STOPPED_SQL = """
    SELECT a.slug
      FROM sn_config.agent_channels ac
      JOIN sn_config.agents a ON a.id = ac.agent_id
      JOIN sn_config.channels c ON c.id = ac.channel_id
     WHERE ac.instance_ref = %s AND c.slug = %s AND ac.enabled
       AND NOT a.enabled AND a.archived_at IS NULL
"""

# The tag that says the person was told (message_state.tags) and the source
# that marks the notice row are contacts.HELD_TOLD_TAG / HELD_NOTICE_SOURCE:
# the gate that lets the notice out reads the same two names. Read at call
# time, not bound here, so this module imports without the contacts module.


def _stopped_receiver(recipient, channel_id, channel_type):
    for candidate in (recipient, channel_id):
        if not candidate:
            continue
        rows = db.query(STOPPED_SQL, (candidate, channel_type), fetch=True) or []
        if len(rows) == 1:
            return candidate, rows[0][0]
    return None, None


def _hold_for_stopped(msg, mailbox, agent):
    """The bound agent is stopped: leave the row 'new' (it is answered when the
    agent is started again) and tell the person once, on their own channel.

    Until now this was "no agent bound", the row failed, and the person heard
    nothing: from outside a stopped agent and a dead one looked the same."""
    told = db.query(
        """UPDATE message_state SET tags = array_append(tags, %s), updated_at = now()
            WHERE message_id = %s AND status = 'new' AND NOT (%s = ANY(tags))
        RETURNING message_id""",
        (contacts.HELD_TOLD_TAG, msg.message_id, contacts.HELD_TOLD_TAG), fetch=True)
    if not told:
        return
    LOG(_TAG, "held msg id=%s -- agent %s is stopped; telling the sender once"
        % (msg.message_id, agent))
    try:
        _DELIVERERS[msg.channel](
            "Your message was received and is waiting: this assistant is paused "
            "right now. It will answer when it is started again.",
            msg.sender, mailbox, msg.metadata, msg.thread_id, msg.external_id, agent,
            notice={"source": contacts.HELD_NOTICE_SOURCE, "about_message_id": msg.message_id,
                    "agent": agent})
    except Exception as exc:  # the notice is a courtesy; the message still waits
        LOG(_TAG, "held msg id=%s -- notice not queued: %r" % (msg.message_id, exc))


def _set_state(message_id, status, error=None):
    """Advance one message_state row. processed_at is stamped on the
    terminal states only."""
    db.query(
        """UPDATE message_state
              SET status = %s,
                  error = %s,
                  processed_at = CASE WHEN %s IN ('processed', 'failed')
                                      THEN now() ELSE processed_at END
            WHERE message_id = %s""",
        (status, error, status, message_id),
    )


class AmbiguousReceiver(RuntimeError):
    pass


def _resolve_mailbox(recipient, channel_id, channel_type):
    """Which of OUR channel instances received this, and which agent owns it:
    `(mailbox, agent)`, or `(None, None)` for the dead-letter. Multiple enabled
    receivers on this channel raise rather than selecting an owner or fallback.

    `recipient` is tried first -- that is the historical rule and the one
    core-api uses. `channel_id` is the fallback, and it exists because the two
    disagree the moment an ALIAS is involved: mail addressed to info@ and
    forwarded to support@ keeps `info@` in its To header forever, while the box it
    was actually fetched from -- the only address this node holds credentials
    for and the only one that can answer -- is support@. Routing on the header
    alone dead-letters every forwarded message with "no agent bound", which is
    true of the header and false of the message.

    Order matters and is not arbitrary: an address that IS bound wins over the
    box it happened to land in, so a direct message still routes by who it was
    addressed to even when several aliases point at one mailbox.

    The address that resolved is returned rather than assumed by the caller,
    because everything downstream must agree on it -- the contact gate asks
    "granted to the agent that owns THIS box", and the reply must leave AS a box
    this node can authenticate as. Answering as the alias would mean sending as
    an address the node may hold no password for.
    """
    for candidate in (recipient, channel_id):
        if not candidate:
            continue
        rows = db.query(RESOLVE_SQL, (candidate, channel_type), fetch=True) or []
        if len(rows) > 1:
            names = ", ".join(sorted({r[0] for r in rows}))
            raise AmbiguousReceiver("Mailbox %r on %s has multiple enabled receivers: %s. "
                                    "No recipient selected." % (candidate, channel_type, names))
        if rows:
            return candidate, rows[0][0]
    return None, None


def _ask_brin(channel, agent, conversation_id, content, principal=None):
    """POST one message to brin, as a single assembled turn, and return its
    reply text and tool actions. `agent` pins the DB agent (no fallback to the default);
    `conversation_id` keys the per-chat gateway session so the dialogue is
    remembered across messages. Raises on transport / non-2xx / empty reply."""
    resp = requests.post(
        BRIN_API_URL + "/api/message",
        json={
            "message": content,
            "agent": agent,
            "channel": channel,
            "context": {"user_id": conversation_id, **({"principal": principal} if principal else {})},
            "stream": False,
            "wait_seconds": TURN_WAIT,
        },
        headers={"X-API-Key": require("BRIN_API_KEY")},
        timeout=TURN_WAIT + 60,
    )
    resp.raise_for_status()
    data = resp.json()
    reply = data.get("final_answer")
    if (data.get("failed") or data.get("working") or data.get("stopped")
            or not isinstance(reply, str) or not reply.strip()):
        raise RuntimeError("brin returned no completed final answer")
    return {"final_answer": reply, "actions": data.get("actions") or []}


def _outbound_cursor():
    """Newest outbound row before a fast turn, or None when it cannot be read.

    Failure is deliberately fail-open for the ordinary reply: without a cursor
    we cannot prove that the tool committed a new row, so dispatch keeps its
    historical delivery path and does not swallow the person's answer.
    """
    try:
        rows = db.query(
            "SELECT COALESCE(MAX(id), 0) FROM messages "
            "WHERE direction = 'outbound'",
            fetch=True,
        ) or []
        return int(rows[0][0]) if rows else 0
    except Exception as exc:
        LOG(_TAG, "could not read outbound cursor (%r); final reply stays enabled" % exc)
        return None


# The tools an agent answers a message with. messaging's own MCP server calls it
# inbox_reply, the HTTP tool catalog messages_reply; both end in reply_to_message.
_REPLY_TOOLS = ("inbox_reply", "messages_reply")


def _explicit_channel_send(actions, channel):
    """Whether this turn explicitly invoked messaging send for this channel.

    Tool NAMES and arguments are structured facts from the gateway. The final
    prose is not inspected: a channel-action report today could be a perfectly
    legitimate sentence tomorrow. Both Claude MCP and HTTP-tool names are
    accepted.
    """
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        name = str(action.get("name") or "").lower().replace("-", "_")
        short = name.rsplit("__", 1)[-1]
        args = action.get("input") or {}
        if not isinstance(args, dict):
            continue
        if (short in ("send_message", "messages_send")
                and str(args.get("channel") or "").strip().lower() == channel):
            return True
        # An answer given with the reply tool names the message it answers, not a
        # channel: the channel is the one that message came in on. Whether it
        # really went out on THIS channel is settled by the committed row below.
        if short in _REPLY_TOOLS and args.get("to_message_id") is not None:
            return True
    return False


def _committed_tool_reply(after_id, actions, channel, agent, sender, thread_id):
    """Return the committed outbound id when the tool already answered.

    Invocation alone is not delivery: a refused/failed messaging call must not
    suppress the final answer. Confirmation is a new outbound row on the same
    channel, from this agent's channel identity, to this conversation's person:
    a `comm-send` row (send_message) or a row that records the message it
    answers (inbox_reply, which does not write a source). The agent lock held by _handle excludes a concurrent turn by the
    same agent, so the cursor makes this row belong to the current turn.
    """
    if after_id is None or not _explicit_channel_send(actions, channel):
        return None
    targets = sorted({
        str(v).strip() for v in (sender, thread_id)
        if v is not None and str(v).strip()
    })
    if not targets:
        return None
    try:
        rows = db.query(
            """SELECT m.id
                 FROM messages m
                 JOIN sn_config.agent_channels ac
                   ON ac.instance_ref = m.sender AND ac.enabled
                 JOIN sn_config.agents a
                   ON a.id = ac.agent_id
                  AND a.enabled AND a.archived_at IS NULL
                WHERE m.id > %s
                  AND m.direction = 'outbound'
                  AND m.channel_type = %s
                  AND m.recipient = ANY(%s)
                  AND (m.metadata->>'source' = 'comm-send'
                       OR m.metadata->>'reply_to_message_id' IS NOT NULL)
                  AND a.slug = %s
                ORDER BY m.id
                LIMIT 1""",
            (after_id, channel, targets, agent),
            fetch=True,
        ) or []
        return rows[0][0] if rows else None
    except Exception as exc:
        LOG(_TAG, "could not confirm tool reply (%r); final reply stays enabled" % exc)
        return None


# Every deliverer takes the same arguments -- (reply, sender, recipient,
# metadata, thread_id, external_id, agent) -- so _handle stays channel-agnostic.
# `agent` is the DB agent that produced the reply; it is carried down purely so
# the outbound registry row records WHO answered.
# `recipient` is the node's own channel instance that received the inbound
# message (for email: the mailbox), and it decides the identity the reply
# leaves under. Passing it is what stops a node answering as some default
# mailbox on another domain. It is the address _resolve_mailbox
# settled on, NOT the To header -- for mail that arrived through an alias
# those differ, and only the former is a box this node can authenticate as.

def _deliver_telegram(reply, sender, recipient, metadata, thread_id, external_id,
                      agent=None, notice=None):
    """Send a reply on telegram. chat_id is carried in the inbound row's
    metadata by the telegram adapter."""
    from adapters.telegram import adapter as telegram_adapter
    chat_id = metadata.get("chat_id")
    if chat_id is None:
        raise RuntimeError("telegram row has no chat_id in metadata")
    # Queue the reply as an outbound row; send-outbound (the timer) is the
    # SOLE transmitter. Sending inline here too double-sends, because
    # send-outbound also drains every 'new' outbound row.
    db.record_outbound(
        channel_type="telegram",
        channel_id=telegram_adapter._channel_id(),
        # A notice has its own id: "reply:<ext>" belongs to the real answer,
        # which would otherwise be dropped as a duplicate when it comes.
        external_id=("held:%s" if notice else "reply:%s") % (external_id or chat_id),
        thread_id=str(chat_id),
        sender=telegram_adapter._channel_id(),
        recipient=str(chat_id),
        content=reply,
        metadata={"chat_id": chat_id, **(notice or {})},
        agent=agent,
    )


def _deliver_email(reply, sender, recipient, metadata, thread_id, external_id,
                   agent=None, notice=None):
    """Queue a reply by email, to leave AS the mailbox that received it.

    `recipient` is that mailbox, and it becomes the outbound row's `sender` --
    the identity send_outbound authenticates as when it transmits. Recording the
    real receiving box (rather than whatever single mailbox the .env happens to
    name) is what stops mail addressed to one mailbox coming back signed by an
    unrelated one; the multi-mailbox poller stamps the same address as
    `recipient` on the inbound row, so the two agree by construction.

    This does NOT send. See the module docstring: send_outbound is the sole
    transmitter, and its egress allowlist and loop guards run there, at the one
    point where bytes leave. Symmetric with _deliver_telegram.
    """
    subject = (metadata.get("subject") or "").strip()
    if not subject.lower().startswith("re:"):
        subject = ("Re: " + subject) if subject else "Re: your message"
    # In-Reply-To must be the WIRE id, never the row id. external_id is scoped
    # to the receiving mailbox ("box|<msg-id>", adapter.py) so that one letter
    # delivered to three of our boxes is three rows; putting that scoped value
    # in the header would break threading in the reader's mail client. Rows
    # written before the scoping carry the bare Message-ID as external_id, so
    # fall back to it -- with the same "uid:" guard, which means we never saw a
    # Message-ID at all and there is nothing to thread to.
    in_reply_to = (metadata.get("message_id") or "").strip() or None
    if not in_reply_to and external_id and not external_id.startswith("uid:"):
        in_reply_to = external_id
    refs = (metadata.get("references") or "").strip()
    if in_reply_to:
        references = (refs + " " + in_reply_to).strip()
    else:
        references = refs or ""
    # Mint the RFC822 Message-ID here and store it as the row's external_id.
    # send_outbound transmits WITH that id, so the registry and the wire carry
    # the same identifier -- the property the transmit path relies on to keep a
    # thread joined. Previously the transport minted it mid-send, which is only
    # possible when the send happens inline.
    message_id = email.utils.make_msgid(domain=(recipient or "@node").rsplit("@", 1)[-1])
    # Reply to the room, not to the speaker. Whoever wrote to us chose an
    # audience: people in To, and people they wanted kept informed in Cc.
    # Answering the sender alone removes all of them silently, and the person
    # who set the copy list is the last to find out. Seen on a node in the field:
    # the owner copied himself on a question to an agent, the agent answered,
    # and the answer reached only the agent that had asked.
    #
    # Our own box is dropped (we are the one replying) and the sender is
    # dropped from the copies because they are already the To.
    _mine = (recipient or "").strip().lower()
    _from = (sender or "").strip().lower()
    _cc, _seen = [], set()
    # A notice goes to the writer only: it is about their message, and the
    # gate admits it for them alone.
    for _a in ([] if notice else list(metadata.get("to_addrs") or []) + list(metadata.get("cc_addrs") or [])):
        _al = (_a or "").strip().lower()
        if not _al or _al in (_mine, _from) or _al in _seen:
            continue
        _seen.add(_al)
        _cc.append(_al)
    db.record_outbound(
        channel_type="email",
        channel_id=recipient,
        external_id=message_id,
        thread_id=(thread_id or in_reply_to or message_id).strip("<> "),
        sender=recipient,
        recipient=sender,
        content=reply,
        metadata={"subject": subject,
                  "in_reply_to": in_reply_to or "",
                  "references": references, **(notice or {})},
        agent=agent,
        recipients=[{"addr": a, "role": "cc"} for a in _cc],
    )


# One model call plus delivery. Short on purpose: a lease that outlives a
# crashed dispatcher silences that agent's channel until it expires.
_CLAIM_LEASE_SECONDS = 300


def _claim_agent(agent, ref):
    """Take the node's agent lock, or report that somebody else holds it.

    Fail-closed by contract and by accident both: false when the agent is busy
    with a live lease, and false again if the call itself fails, because running
    a second turn on a guess is the failure this exists to prevent.
    """
    try:
        rows = db.query("SELECT sn_core.claim_agent(%s, %s, %s) AS claimed",
                        (agent, ref, _CLAIM_LEASE_SECONDS), fetch=True) or []
        return bool(rows and rows[0][0])
    except Exception as exc:
        LOG(_TAG, "claim failed for %s (%r) -- treating as busy" % (agent, exc))
        return False


def _release_agent(agent):
    """Give the lock back. Never raises: a turn that answered must not be turned
    into a failure by its own cleanup. A lost release costs one lease of silence
    for that agent, which is what the lease is for."""
    try:
        db.query("SELECT sn_core.release_agent(%s)", (agent,), fetch=True)
    except Exception as exc:
        LOG(_TAG, "release failed for %s (%r) -- lease will expire" % (agent, exc))


@contextlib.contextmanager
def _lease_kept(agent, ref):
    """Keep the claim alive while a turn that may run for an hour runs.

    The lease stays short, so a dispatcher that dies frees the agent within
    minutes; a live turn renews it every third of a lease instead. A busy row
    refuses a claim, so renewing is release and claim, the way brin's desk
    renews its own: the gap is one round trip. If the Runner takes the agent in
    it, this turn simply runs on, queued in the gateway behind nothing."""
    stop = threading.Event()

    def _loop():
        while not stop.wait(_CLAIM_LEASE_SECONDS / 3.0):
            _release_agent(agent)
            if not _claim_agent(agent, ref):
                LOG(_TAG, "lease of %s lost while its turn runs" % agent)
                return

    t = threading.Thread(target=_loop, name="lease-%s" % agent, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


_DELIVERERS = {
    "telegram": _deliver_telegram,
    "email": _deliver_email,
}


# A turn takes anything from ten seconds to a minute and a half: the model
# thinks, and on a long one it files a task or sends mail BEFORE it says
# anything. On a chat channel that silence is indistinguishable from a node that
# has stopped working. Measured on a node in the field: the owner asked "have you
# sent it or not" 59 seconds into a turn that was at that moment composing the
# mail it then sent successfully, and asked again afterwards whether anything
# had happened at all.
#
# Channels that have an activity hint get it held open for the length of the
# think. Email has none by nature and is left alone. The hint is looked up per
# channel beside the deliverers, so adding one later is an entry in a dict
# rather than a change to the loop.
def _hint_telegram(chat_id):
    from adapters.telegram import transport
    return transport.send_chat_action(chat_id, "typing")


_ACTIVITY_HINTS = {
    "telegram": _hint_telegram,
}

_HINT_INTERVAL = 4.0        # telegram clears "typing" after about five seconds


@contextlib.contextmanager
def _activity(channel, chat_id):
    """Hold the channel's activity hint open for the duration of the block.

    A daemon thread re-sends it and stops on the way out. Every failure is
    swallowed, in the transport and in the thread both: a courtesy that can
    break a reply is not a courtesy. A channel with no hint yields immediately
    and costs nothing.
    """
    hint = _ACTIVITY_HINTS.get(channel)
    if hint is None or not chat_id:
        yield
        return
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            try:
                hint(chat_id)
            except Exception:
                pass
            stop.wait(_HINT_INTERVAL)

    t = threading.Thread(target=_loop, name="activity-%s" % channel, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


class Msg:
    """One inbound row, as the scan reads it."""
    __slots__ = ("message_id", "channel", "sender", "recipient", "content", "metadata",
                 "thread_id", "external_id", "channel_id", "mailbox")

    def __init__(self, message_id, channel, sender, recipient, content, metadata,
                 thread_id, external_id, channel_id=None):
        self.message_id, self.channel, self.sender = message_id, channel, sender
        self.recipient, self.content, self.metadata = recipient, content, metadata or {}
        self.thread_id, self.external_id, self.channel_id = thread_id, external_id, channel_id
        self.mailbox = None


_BUSY = "busy"


def _handle(message_id, channel, sender, recipient, content, metadata,
            thread_id, external_id, channel_id=None):
    """Route one inbound message end to end: resolve -> brin -> reply -> registry."""
    msg = Msg(message_id, channel, sender, recipient, content, metadata,
              thread_id, external_id, channel_id)
    agent = _admit(msg)
    if agent:
        _run_turn(agent, [msg])


def _admit(msg, busy=None):
    """Decide whether a message goes to a turn, and whose: the agent's slug, or
    None when it was settled here (blocked, machine mail), or _BUSY when
    `busy(agent)` says the agent has a turn running in this process (the row is
    left 'new' and read with the next batch, and the gate is not asked twice).
    Raises for a message that can only fail (no deliverer, no agent)."""
    message_id, channel, sender, recipient = msg.message_id, msg.channel, msg.sender, msg.recipient
    metadata, thread_id, channel_id = msg.metadata, msg.thread_id, msg.channel_id
    deliver = _DELIVERERS.get(channel)
    if deliver is None:
        raise RuntimeError("no deliverer wired for channel %r" % channel)

    # `mailbox` is the address of OURS this arrived at, which is not always the
    # one in the To header -- see _resolve_mailbox. Everything below uses it
    # rather than `recipient`, so the gate and the reply identity cannot drift
    # apart from the routing decision.
    try:
        mailbox, agent = _resolve_mailbox(recipient, channel_id, channel)
    except AmbiguousReceiver as exc:
        why = str(exc)
        _set_state(message_id, "blocked", why)
        db.emit_routing_denied(channel, message_id, why, _TAG_ACTOR,
                               sender=sender, recipient=recipient)
        LOG(_TAG, "blocked msg id=%s -- %s" % (message_id, why))
        return
    # Ingest-side loop guard: machine mail is recorded (it is evidence) but
    # never answered. Without this a bounce notice reaches the model, the model
    # writes a courteous reply, and the reply bounces.
    if channel == "email" and mailrules.is_auto_generated(metadata, sender):
        _set_state(message_id, "processed")
        db.emit_routing_denied(channel, message_id,
                               "auto-generated mail -- recorded as evidence, never answered",
                               _TAG_ACTOR, sender=sender, recipient=recipient)
        LOG(_TAG, "skipped msg id=%s -- auto-generated mail from %s, not answered"
            % (message_id, sender))
        return

    if not agent:
        held_box, stopped = _stopped_receiver(recipient, channel_id, channel)
        if stopped:
            _hold_for_stopped(msg, held_box, stopped)
            return
        # No agent bound to this channel instance: dead-letter, do not stall the
        # queue (the Runner does not own fast channels, so there is no other
        # consumer to fall back to in v1).
        db.emit_routing_denied(channel, message_id,
                               "no agent bound to this channel instance",
                               _TAG_ACTOR, sender=sender, recipient=recipient)
        raise _at_stage(RuntimeError(
            "no agent bound to %s recipient %r (nor to the box it arrived in, "
            "%r)" % (channel, recipient, channel_id)), "route")

    if busy is not None and busy(agent):
        return _BUSY

    # The inbound gate. This is the WAKE point for every fast channel,
    # and the only place on this path where a model is about to be billed, so
    # it is where "should this agent be woken at all" has to be asked.
    #
    # The verdict is journalled either way, exactly as the outbound gate does:
    # with CONTACT_GATE_INBOUND off, a refusal is recorded and the message is
    # routed anyway, so an operator can read what WOULD have stopped waking
    # their agents before any of it does. Turning it on changes only whether
    # the `return` below is taken.
    #
    # Refused mail is `blocked`, not `processed`. Both mean "we did not answer
    # it", and telling them apart afterwards is the difference between a policy
    # decision and a message quietly going nowhere -- the state the auto-mail
    # guard above deliberately does NOT distinguish, and which cost an hour on
    # the misrouted reply to reconstruct.
    allowed, why = contacts.may_wake(channel, mailbox, sender)
    if not allowed:
        db.emit_blocked(channel, message_id, why, _TAG_ACTOR,
                        sender=sender, recipient=mailbox,
                        enforced=contacts.inbound_enabled())
        if contacts.inbound_enabled():
            _set_state(message_id, "blocked", why[:500])
            LOG(_TAG, "blocked msg id=%s -- %s" % (message_id, why))
            return
        LOG(_TAG, "inbound gate (report-only) would block msg id=%s -- %s"
            % (message_id, why))

    msg.mailbox = mailbox
    return agent


def _turn_text(msgs):
    """What the agent reads: the message, or every message the person wrote
    while it was busy, oldest first, as one turn."""
    if len(msgs) == 1:
        return msgs[0].content
    parts = ["%d. %s" % (i + 1, m.content) for i, m in enumerate(msgs)]
    return ("The person wrote %d messages while you were busy, oldest first. "
            "Read them together and answer once:\n\n%s" % (len(msgs), "\n\n".join(parts)))


# The final answer an agent gives to say nothing at all. Case, surrounding
# whitespace, brackets and a closing full stop are forgiven ("[no_reply].");
# any other text in the answer is an answer and is delivered.
NO_REPLY = "NO_REPLY"


def _is_no_reply(reply):
    return (reply or "").strip().strip("[]().").strip().upper() == NO_REPLY


def _run_turn(agent, msgs):
    """One turn of `agent` for `msgs` (one conversation, oldest first): claim,
    ask brin, deliver one reply to the newest message, settle every row."""
    last = msgs[-1]
    channel, sender, thread_id = last.channel, last.sender, last.thread_id
    ids = [m.message_id for m in msgs]
    for m in msgs:
        _set_state(m.message_id, "routed")
    LOG(_TAG, "routed msg id=%s channel=%s agent=%s conv=%s"
        % (",".join(str(i) for i in ids), channel, agent, thread_id))

    # ONE TURN PER AGENT. The Runner claims an agent before waking it and skips
    # when the claim is refused; this loop did not, because it talks to brin and
    # had no reason to know the runtime table existed. So the two paths ran the
    # same agent at once: a wake carrying the answer to a question, and a chat
    # turn answering that same question again, neither aware of the other. Seen
    # live on a node in the field 2026-09-09 -- the owner got the figures twice, in
    # two languages, with "I have no direct access to system metrics" in
    # between, said by the turn that did not know the other one was at that
    # moment carrying exactly that access.
    #
    # A refused claim is NOT an error and NOT a denial: the message goes back to
    # 'new' and the next tick, seconds away, tries again. Ordering holds and
    # nothing is lost. Deliberately not a wait -- blocking here would stall
    # every other conversation behind one slow agent.
    ref = "dispatch:%s" % last.message_id
    if not _claim_agent(agent, ref):
        for i in ids:
            _set_state(i, "new")
        LOG(_TAG, "agent %s busy -- msg id=%s back to new, retrying next tick"
            % (agent, ",".join(str(i) for i in ids)))
        return

    # The lock is held across the delivery too, not only the thinking. The
    # Runner holds it for a whole wake, and a second turn started while this one
    # is still writing its reply is the same defect with a smaller window.
    # The turn is recorded as the desk path and the Runner record theirs: a start
    # and an end carrying one turn id. This path used to run the agent with
    # nothing in the event log under its name, so a reader asking "was integration-agent
    # working at 11:03" got no, while integration-agent was answering an email.
    turn_id = uuid.uuid4().hex
    turn = {"via": "fast-dispatch", "turn_id": turn_id, "channel": channel,
            "message_id": last.message_id}
    if len(ids) > 1:
        turn["message_ids"] = ids
    db.emit_turn(agent, "agent.turn_started", last.message_id, turn)
    try:
        # conversation_id = thread_id (the chat) -> a stable per-conversation session.
        try:
            outbound_before = _outbound_cursor()
            with _activity(channel, sender), _lease_kept(agent, ref):
                answer = _ask_brin(channel, agent, thread_id or sender, _turn_text(msgs),
                                   principal=sender)
            reply = answer["final_answer"]
        except Exception as exc:
            raise _at_stage(exc, "think")
        try:
            tool_outbound_id = _committed_tool_reply(
                outbound_before, answer.get("actions"), channel, agent,
                sender, thread_id,
            )
            # NO_REPLY is the agent choosing silence: the mail was not for it, or
            # it has nothing to add. The turn completes and nothing is sent.
            # Before, the only ways to end a turn were an answer, which went to
            # the whole thread, or an empty one, which failed the turn.
            if _is_no_reply(reply):
                LOG(_TAG, "agent %s chose NO_REPLY; nothing delivered" % agent)
            elif tool_outbound_id is None:
                _DELIVERERS[channel](reply, sender, last.mailbox, last.metadata, thread_id,
                                     last.external_id, agent)
            else:
                LOG(
                    _TAG,
                    "reply already queued by messaging tool as outbound id=%s; "
                    "discarding final turn summary (%d chars)"
                    % (tool_outbound_id, len(reply)),
                )
        except Exception as exc:
            raise _at_stage(exc, "deliver")
    except Exception as exc:
        db.emit_turn(agent, "agent.turn_failed", last.message_id,
                     dict(turn, stage=getattr(exc, "sn_stage", "dispatch")))
        raise
    else:
        db.emit_turn(agent, "agent.turn_completed", last.message_id, turn)
    finally:
        _release_agent(agent)

    for i in ids:
        _set_state(i, "processed")
    LOG(_TAG, "processed msg id=%s -- reply queued (%d chars)"
        % (",".join(str(i) for i in ids), len(reply)))


# A turn that failed while the agent was THINKING (it produced no answer) is tried
# again, later, and the person is told when it still cannot be answered. Before, the
# message went to 'failed' and nothing else happened: the owner's Telegram messages to
# one agent (five in two days, the last at 11:30 on 21 Oct) were never answered,
# nobody was told, and nothing tried them again. A turn that failed while DELIVERING
# an answer it had is not run again: that would spend the turn twice.
# `wake_attempts` and `wake_next_at` are the retry count and the not-before time; the
# only other user of them is the reply-obligation scan, which reads rows in the
# 'awaiting_reply' state and never these.
RETRY_DELAYS = [int(x) for x in get("DISPATCH_RETRY_DELAYS", "60,300").split(",") if x.strip().isdigit()]
FAILED_TOLD_TAG = "failed-told"


def _retry_later(msg, exc):
    """Put a message whose turn failed while the agent was thinking back in the
    queue, to be read again after a delay. True when that was done."""
    if getattr(exc, "sn_stage", "") != "think" or not RETRY_DELAYS:
        return False
    rows = db.query("SELECT COALESCE(wake_attempts, 0) FROM message_state WHERE message_id = %s",
                    (msg.message_id,), fetch=True) or []
    tried = int(rows[0][0]) if rows else 0
    if tried >= len(RETRY_DELAYS):
        return False
    db.query(
        """UPDATE message_state
              SET status = 'new', error = %s, wake_attempts = %s,
                  wake_next_at = now() + make_interval(secs => %s), updated_at = now()
            WHERE message_id = %s""",
        (str(exc)[:500], tried + 1, RETRY_DELAYS[tried], msg.message_id),
    )
    LOG(_TAG, "msg id=%s: the agent gave no answer -- try %d of %d in %ds"
        % (msg.message_id, tried + 1, len(RETRY_DELAYS), RETRY_DELAYS[tried]))
    return True


def _tell_failed(msg, agent):
    """The person is told, once, that their message could not be answered, on
    their own channel and in the language they wrote in."""
    told = db.query(
        """UPDATE message_state SET tags = array_append(tags, %s), updated_at = now()
            WHERE message_id = %s AND NOT (%s = ANY(tags))
        RETURNING message_id""",
        (FAILED_TOLD_TAG, msg.message_id, FAILED_TOLD_TAG), fetch=True)
    if not told:
        return
    text = ("I could not answer this message: the assistant did not finish its answer after "
            "several tries. The message is saved. Send it again or ask in the web chat.")
    try:
        _DELIVERERS[msg.channel](
            text, msg.sender, msg.mailbox, msg.metadata, msg.thread_id, msg.external_id, agent,
            notice={"source": contacts.HELD_NOTICE_SOURCE, "about_message_id": msg.message_id,
                    "agent": agent})
        LOG(_TAG, "msg id=%s: told the sender it could not be answered" % msg.message_id)
    except Exception as exc:  # the notice is a courtesy; the message stays recorded as failed
        LOG(_TAG, "msg id=%s: failure notice not queued: %r" % (msg.message_id, exc))


def _fail(msg, exc):
    stage = getattr(exc, "sn_stage", "dispatch")
    LOG(_TAG, "FAIL msg id=%s stage=%s: %r" % (msg.message_id, stage, exc))
    _set_state(msg.message_id, "failed", str(exc)[:500])
    db.emit_failed(msg.channel, msg.message_id, stage, exc, _TAG_ACTOR)


# The turns running now, one thread per agent. A turn may run for an hour; the
# scan goes on beside it, so one agent's long turn holds nobody else's chat.
# The loop used to take one message at a time and wait for its answer, and on
# a node in the field every other agent's chat waited behind one slow agent.
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()


def _busy(agent):
    with _WORKERS_LOCK:
        t = _WORKERS.get(agent)
        return t is not None and t.is_alive()


def _worker(agent, msgs):
    try:
        _run_turn(agent, msgs)
    except Exception as exc:  # the turn failed: every message of it did
        gave_up = []
        for m in msgs:
            if not _retry_later(m, exc):
                _fail(m, exc)
                gave_up.append(m)
        if gave_up:
            _tell_failed(gave_up[-1], agent)   # once for the whole batch


def _scan_once():
    """Read every new message, settle what can be settled, and start one turn
    per idle agent. Messages of a chat channel from one conversation go into
    that turn together; the rest of an agent's messages wait for its next one."""
    batches = {}
    for row in db.query(SCAN_SQL, (FAST_CHANNELS,), fetch=True) or []:
        msg = Msg(*row)
        try:
            agent = _admit(msg, busy=_busy)
        except Exception as exc:  # one bad message must not stall the queue
            _fail(msg, exc)
            continue
        if not agent or agent == _BUSY:
            continue
        conv = (msg.channel, msg.thread_id or msg.sender)
        if msg.channel not in _BATCHED_CHANNELS:
            conv = (msg.channel, msg.message_id)
        queued = batches.setdefault(agent, {})
        if queued and conv not in queued:
            continue                    # another conversation: the agent's next turn
        queued.setdefault(conv, []).append(msg)
    for agent, convs in batches.items():
        (msgs,) = convs.values()
        t = threading.Thread(target=_worker, args=(agent, msgs), name="turn-%s" % agent,
                             daemon=True)
        with _WORKERS_LOCK:
            _WORKERS[agent] = t
        t.start()


def main():
    """Scan loop -- runs forever as a daemon."""
    if not FAST_CHANNELS:
        LOG(_TAG, "no FAST_CHANNELS configured -- idle (every channel stays on the wake-loop)")
    LOG(_TAG, "fast-dispatch up, brin=%s channels=%s interval=%ss turn_wait=%ss"
        % (BRIN_API_URL, FAST_CHANNELS or "(none)", DISPATCH_INTERVAL, TURN_WAIT))
    while True:
        try:
            if FAST_CHANNELS:
                _scan_once()
        except Exception as exc:  # transient DB hiccup -- log, back off, retry
            LOG(_TAG, "scan ERROR %r" % exc)
        time.sleep(DISPATCH_INTERVAL)


if __name__ == "__main__":
    main()
