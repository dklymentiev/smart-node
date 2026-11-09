"""Messages-registry writer for the communication component.

Every channel adapter records inbound and outbound messages here -- the
node's `messages` table (B02 core schema). That table is THE
cross-component contract: brin and the secretary read its `message_state`
work-queue. The AFTER INSERT trigger seeds a `message_state` row
(status 'new'); adapters never touch `message_state` directly, with one
exception: `mark_transmitted` settles a row the caller has just put on the
wire itself, because nothing else will (see its docstring).

Every write here also appends to the node's event journal (`sn_core.events`)
via `_emit`. That journal is the documented way to answer "what happened to
this message"; an emitter that fails warns and never blocks the message.

`messages` is append-only -- adapters only ever INSERT. Deduplication is
the `(channel_type, external_id)` unique index: a re-delivered update
hits ON CONFLICT DO NOTHING and is silently dropped.
"""
import logging

import psycopg2
from psycopg2.extras import Json

from common.config import DB_DSN

log = logging.getLogger(__name__)

__all__ = ["record_inbound", "record_inbound_with_attachments",
           "record_outbound", "mark_transmitted", "stamp_identity",
           "emit_transmitted", "emit_failed", "emit_routing_denied", "emit_dry_run",
           "adapter_actor", "query", "Json"]


def query(sql, args=None, fetch=False):
    """Run one statement on a fresh autocommit connection."""
    conn = psycopg2.connect(DB_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql, args or ())
        return cur.fetchall() if fetch else None
    finally:
        conn.close()


def _record(direction, channel_type, channel_id, external_id, thread_id,
            sender, recipient, content, content_type, metadata,
            received_at=None, initial_status=None, recipients=None):
    """INSERT one messages row. Returns the new id, or None on dedup conflict.

    `received_at` overrides the column default. The default is now(), which is
    when we POLLED -- for a mailbox being read for the first time that is not
    when the message arrived, and the difference is the whole of Pass
    the real arrival time when the channel knows it; leave it None when it does
    not, and now() stands.

    `initial_status` settles the state row that the AFTER INSERT trigger seeds
    as 'new', and it happens in the SAME transaction as the insert. That is not
    tidiness: the fast-dispatch loop and the Runner both scan for 'new', so a
    row committed as 'new' and corrected a moment later is a row they may
    already have picked up. Either it is never routable or the guard is a race.
    """
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn:  # commits on success, rolls back on exception
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO messages
                       (channel_type, channel_id, external_id, thread_id,
                        direction, sender, recipient, content, content_type,
                        metadata, received_at, recipients)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           COALESCE(%s, now()), %s)
                   ON CONFLICT (channel_type, external_id) DO NOTHING
                   RETURNING id""",
                (channel_type, channel_id, external_id, thread_id, direction,
                 sender, recipient, content, content_type, Json(metadata or {}),
                 received_at, Json(recipients) if recipients else None),
            )
            row = cur.fetchone()
            if row and initial_status:
                cur.execute(
                    """UPDATE message_state
                          SET status = %s, processed_at = now()
                        WHERE message_id = %s""",
                    (initial_status, row[0]),
                )
        return row[0] if row else None
    finally:
        conn.close()


def stamp_identity(channel_type, msg_id, handle, proof=None, evidence=None):
    """Who this message is from, decided once, at arrival.

    The rule is sn_core.stamp_message_identity() in the database, the same one
    core-api calls for the ingests it owns -- an adapter and a route must not
    answer "who is this" differently. `proof` is what the TRANSPORT established
    and nothing else: 'dkim' when the sender domain verified, None when the
    channel carries no proof at all, which is the honest answer for telegram
    and for our email path today.

    Warns and never raises: a verdict that cannot be written must not cost the
    message it describes.
    """
    try:
        query("SELECT sn_core.stamp_message_identity(%s, %s, %s, %s, %s)",
              (msg_id, channel_type, handle, proof, Json(evidence or {})))
    except Exception as e:                      # pragma: no cover -- diagnostics
        log.warning("identity not stamped for message %s: %s", msg_id, e)


def _emit(actor, event_type, msg_id, payload):
    """Best-effort: append one message-path fact to the node's event journal.

    The journal is what a reader queries to answer "what happened to this
    message" -- our own architecture glossary defines the chain as
    message.inbound -> agent.woken -> message.sent -> message.transmitted. Every
    emitter goes through here so the two things that are easy to get wrong are
    got right once:

    - the actor FK. sn_core.events.actor references sn_core.actors, so the
      component self-registers. ON CONFLICT DO NOTHING needs SELECT as well as
      INSERT on `actors`: without it Postgres answers "permission denied for
      table actors" while has_table_privilege(...,'INSERT') says true in the
      same session.
    - never silent. `except: pass` here is why the journal stayed empty for
      months while the node's own documentation told agents to read it. A
      failure that costs nothing to log must never be swallowed -- but it must
      also never block the message it describes, so it stays a warning.
    """
    try:
        query("INSERT INTO sn_core.actors (id, display_name, type, layer) "
              "VALUES (%s, %s, 'component', 'system') ON CONFLICT (id) DO NOTHING",
              (actor, actor))
        query("INSERT INTO sn_core.events "
              "  (actor, event_type, entity_type, entity_id, payload) "
              "VALUES (%s, %s, 'message', %s, %s)",
              (actor, event_type, str(msg_id), Json(payload)))
    except Exception as e:
        log.warning("%s not recorded for msg %s: %s", event_type, msg_id, e)


def adapter_actor(channel_type):
    """The journal actor for a channel's own adapter."""
    return "component:%s-adapter" % channel_type


def record_inbound(channel_type, channel_id, external_id, thread_id,
                   sender, recipient, content,
                   content_type="text/plain", metadata=None,
                   received_at=None, history=False):
    """Record a received message. Returns the messages.id, or None if the
    row already existed (the adapter saw this external_id before).

    `history=True` records the message and settles it so nothing will ever
    route it to an agent: it arrived before this channel instance was bound.
   The row is still written -- it is context and evidence, and the agent can read it -- and the journal gets a routing_denied telling anyone
    who asks later why no reply was composed.
    """
    msg_id = _record("inbound", channel_type, channel_id, external_id,
                     thread_id, sender, recipient, content, content_type,
                     metadata, received_at=received_at,
                     initial_status="history" if history else None)
    if msg_id:
        stamp_identity(channel_type, msg_id, sender)
        _emit(adapter_actor(channel_type), "message.inbound", msg_id,
              {"channel": channel_type, "from": sender, "to": recipient,
               "history": bool(history)})
        if history:
            emit_routing_denied(
                channel_type, msg_id,
                "predates the binding of this channel instance -- recorded as "
                "history, never answered",
                adapter_actor(channel_type), sender=sender, recipient=recipient)
    return msg_id


def record_inbound_with_attachments(
    channel_type, channel_id, external_id, thread_id,
    sender, recipient, content,
    content_type="text/plain", metadata=None,
    received_at=None, history=False,
    attachments=None,
):
    """Like record_inbound but also inserts attachment rows in ONE transaction.

    attachments is a list of dicts from common.attachments.prepare_attachment
    with '_data' already stripped (bytes must be flushed to disk BEFORE calling
    this function -- write order: bytes first, then metadata). Each dict must
    have: id, filename, content_type, size_bytes, content_hash, storage_path,
    received_at.

    Returns (message_id, was_new):
        message_id  always set -- the new or pre-existing row
        was_new     True only for rows inserted this call

    Crash safety: if the process dies and the message is re-polled,
    record_inbound returns None (dedup), but we still look up the existing
    message_id and retry the attachment inserts. The unique index
    (message_id, content_hash, filename) on sn_core.message_attachments
    makes those inserts idempotent.

    Every attachment passes the scan stage (common.attachment_scan) before its
    row is written, as a Studio chat upload does in core-api: the sender the
    contact gate trusts gets `approved`, anyone else `received`. Only an
    approved file is served by GET /api/attachments/{id}/content.
    """
    scan = scan_attachments(channel_type, recipient, sender) if attachments else None
    stored = []
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn:
            cur = conn.cursor()
            # Insert the message row; search_path=sn_core,... makes 'messages'
            # resolve to sn_core.messages (set in the DSN by communication.sh).
            cur.execute(
                """INSERT INTO messages
                       (channel_type, channel_id, external_id, thread_id,
                        direction, sender, recipient, content, content_type,
                        metadata, received_at)
                   VALUES (%s, %s, %s, %s, 'inbound', %s, %s, %s, %s, %s,
                           COALESCE(%s, now()))
                   ON CONFLICT (channel_type, external_id) DO NOTHING
                   RETURNING id, tenant_id""",
                (channel_type, channel_id, external_id, thread_id,
                 sender, recipient, content, content_type, Json(metadata or {}),
                 received_at),
            )
            row = cur.fetchone()
            was_new = row is not None
            if row:
                msg_id, tenant_id = row
                if history:
                    cur.execute(
                        "UPDATE message_state SET status = %s, processed_at = now()"
                        " WHERE message_id = %s",
                        ("history", msg_id),
                    )
            else:
                # Dedup: recover the existing row's id and tenant_id so we can
                # still insert attachment rows that the previous run may not have
                # committed (crash between message commit and mark_seen).
                cur.execute(
                    "SELECT id, tenant_id FROM messages"
                    " WHERE channel_type = %s AND external_id = %s",
                    (channel_type, external_id),
                )
                row2 = cur.fetchone()
                if row2:
                    msg_id, tenant_id = row2
                else:
                    msg_id, tenant_id = None, 1
            # Insert attachment rows. ON CONFLICT DO NOTHING for idempotency.
            # The unique index on these columns is PARTIAL (B02
            # message_attachments_dedup_idx, WHERE content_hash IS NOT NULL
            # AND filename IS NOT NULL), and PostgreSQL infers a partial index
            # only when ON CONFLICT repeats its predicate. Without it the
            # first attachment ever met raised "no unique or exclusion
            # constraint matching", and the mailbox stopped taking mail:
            # the message stayed unseen and every poll hit it again.
            for att in (attachments or []):
                if not msg_id:
                    break
                cur.execute(
                    """INSERT INTO sn_core.message_attachments
                           (id, message_id, tenant_id, channel_type, filename,
                            content_type, size_bytes, content_hash,
                            storage_path, status, scan_reason, received_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (message_id, content_hash, filename)
                           WHERE content_hash IS NOT NULL AND filename IS NOT NULL
                       DO NOTHING
                       RETURNING id""",
                    (att["id"], msg_id, tenant_id, channel_type,
                     att["filename"], att["content_type"], att["size_bytes"],
                     att["content_hash"], att["storage_path"],
                     scan[0], scan[1], att.get("received_at")),
                )
                if cur.fetchone():
                    stored.append(att)
    finally:
        conn.close()

    # EMAIL GOES THROUGH HERE, NOT THROUGH record_inbound. The upstream report stamped
    # only record_inbound (telegram and slack), which would have left the one
    # channel whose handle is a real, portable address unstamped -- the channel
    # the whole cross-channel identity story is about.
    #
    # Stamped outside the transaction above so a verdict can never roll back the
    # message it describes, and stamped even on a dedup hit: the insert is
    # ON CONFLICT DO NOTHING on message_id, so a repeat is free and a row whose
    # verdict went missing on an earlier crash gets one now.
    if msg_id:
        stamp_identity(channel_type, msg_id, sender)
    for att in stored:
        _emit(adapter_actor(channel_type), "attachment.stored", msg_id,
              {"attachment": str(att["id"]), "filename": att["filename"],
               "content_type": att["content_type"], "size_bytes": att["size_bytes"],
               "status": scan[0], "reason": scan[1]})
    return (msg_id, was_new)


def scan_attachments(channel_type, mailbox, sender):
    """(status, reason) for the attachments of one inbound message.

    `trusted` is the contact gate's answer for the sender, the same question it
    answers before an agent is woken (contacts.may_wake): a granted contact of
    the agent that owns the mailbox, or a colleague on this node. Deny by
    default: a gate that cannot answer leaves the files `received`, never
    approved.
    """
    from common import attachment_scan, contacts  # contacts imports this module
    try:
        trusted, why = contacts.may_wake(channel_type, mailbox, sender)
    except Exception as e:                      # pragma: no cover -- diagnostics
        log.warning("contact gate unavailable for attachments of %s to %s: %s",
                    sender, mailbox, e)
        trusted = False
    return attachment_scan.verdict(channel_type, bool(trusted))


def record_outbound(channel_type, channel_id, external_id, thread_id,
                    sender, recipient, content,
                    content_type="text/plain", metadata=None, agent=None,
                    recipients=None):
    """Record a sent message. Returns the messages.id, or None on conflict.

    `agent` is the DB agent slug that actually composed this reply. It is the
    field whose absence hid a misrouted reply -- a turn addressed to one agent answered by
    another, invisible until two log files were matched by eye. The caller knows
    it (the dispatch layer resolved it); nothing downstream can recover it, so
    it is recorded here or not at all.

    `recipients` is the rest of the audience: send_outbound already
    reads the column and puts everything past the primary into Cc. Nothing was
    ever writing it, so every reply went to the sender alone."""
    msg_id = _record("outbound", channel_type, channel_id, external_id,
                     thread_id, sender, recipient, content, content_type,
                     metadata, recipients=recipients)
    if msg_id:
        _emit(adapter_actor(channel_type), "message.sent", msg_id,
              {"channel": channel_type, "from": sender, "to": recipient,
               "agent": agent, "chars": len(content or "")})
    return msg_id


def emit_turn(agent, event_type, msg_id, payload):
    """Best-effort: record the start or the end of an agent turn run for a message.

    A turn is the agent's, so the actor is agent:<slug>, the same actor the desk
    path and the Runner use, and the entity is the message that caused it. The
    agent's actor row already exists (the agent was created with it), so unlike
    _emit this does not self-register: registering an agent as a component would
    be a wrong fact in the actors table. Never raises, never silent.
    """
    try:
        query("INSERT INTO sn_core.events "
              "  (actor, event_type, entity_type, entity_id, payload) "
              "VALUES (%s, %s, 'message', %s, %s)",
              ("agent:%s" % agent, event_type, str(msg_id), Json(payload)))
    except Exception as e:
        log.warning("%s not recorded for agent %s msg %s: %s", event_type, agent, msg_id, e)


def emit_transmitted(channel_type, msg_id, recipient,
                     actor="component:send-outbound"):
    """Best-effort: record 'an outbound message left the node on the wire'."""
    _emit(actor, "message.transmitted", msg_id,
          {"channel": channel_type, "to": recipient})


def mark_transmitted(channel_type, msg_id, recipient,
                     actor="component:send-outbound"):
    """One outbound row left the node: settle its work-queue state AND record it.

    Both halves belong together. Kept apart, the registry lied on every shipped
    node: the fast-dispatch path transmits inline (adapters.email.deliver sends,
    then records the row), and only the send_outbound timer -- which the
    installer does not wire -- ever advanced a row past 'new'. So six delivered
    emails sat queued forever, and installing that timer later would have
    re-sent every one of them.

    Idempotent, journal included: only the call that actually settles the row
    records the transmission, so the inline path and a later timer pass cannot
    both claim the same send. A journal that double-counts is a journal nobody
    can reason from."""
    settled = query(
        # error and send_next_at are cleared: a send that went through on a
        # retry must not keep the "retrying: ..." text of the try before.
        """UPDATE message_state
              SET status = 'processed', processed_at = now(),
                  error = NULL, send_next_at = NULL
            WHERE message_id = %s AND status <> 'processed'
        RETURNING message_id""",
        (msg_id,), fetch=True,
    )
    if settled:
        emit_transmitted(channel_type, msg_id, recipient, actor=actor)


def emit_failed(channel_type, msg_id, stage, error, actor):
    """Best-effort: record that a message died, and at which stage. `stage` is
    the half of the answer message_state.error cannot give -- 'route', 'think',
    'deliver' and 'transmit' fail for entirely different reasons and are fixed
    in different components."""
    _emit(actor, "message.failed", msg_id,
          {"channel": channel_type, "stage": stage, "error": str(error)[:500]})


def emit_send_retry(channel_type, msg_id, attempt, max_attempts, retry_in, error, actor):
    """Best-effort: a send failed for a passing reason and will be tried again.
    Without it the only trace of an outage would be the final failure, or
    nothing at all when a later try went through."""
    _emit(actor, "message.send_retry", msg_id,
          {"channel": channel_type, "attempt": attempt, "max_attempts": max_attempts,
           "retry_in_seconds": retry_in, "error": str(error)[:500]})


def emit_routing_denied(channel_type, msg_id, reason, actor,
                        sender=None, recipient=None):
    """Best-effort: record that a message was deliberately not routed to an
    agent -- no agent bound to the channel instance, or machine mail that is
    recorded as evidence and never answered. A decision, not a fault: without it
    the two look identical from the journal, and both look like nothing
    happened."""
    _emit(actor, "message.routing_denied", msg_id,
          {"channel": channel_type, "reason": reason,
           "from": sender, "to": recipient})


def emit_dry_run(channel_type, msg_id, reason, actor,
                 sender=None, recipient=None):
    """Record that a message was suppressed by the dry-run mechanism.

    Distinct from emit_blocked (contact-gate refusal) and emit_failed (error):
    a dry-run suppression is deliberate operator policy, not a permission
    decision or a fault. The composed content is in the message row;
    nothing left the node.
    """
    _emit(actor, "message.dry_run", msg_id,
          {"channel": channel_type, "reason": reason,
           "from": sender, "to": recipient})


def emit_content_blocked(channel_type, msg_id, reason, actor,
                         sender=None, recipient=None):
    """Record that the content check refused a message for soliciting a credential.

    Distinct event type from emit_blocked (contact-gate refusal) so the operator
    can filter the journal for specifically what content was refused, separate from
    recipient refusals. Always enforced -- there is no report-only mode for
    content checks.
    """
    _emit(actor, "message.content_blocked", msg_id,
          {"channel": channel_type, "reason": reason,
           "from": sender, "to": recipient, "enforced": True})


def tell_sender(agent, msg_id, channel_type, recipient, what):
    """Put a note into the sending agent's inbox: this message did not go out.

    A refusal or a failed transmission used to be an event and a state on the
    row, and nothing the agent reads; core-api had already answered its send
    with ok, so it went on saying the message was sent (#9). The note is an
    internal inbound from `system`, which is how the agent learns of it on its
    next wake. Best-effort: a note that cannot be written must not stop the
    send pass.
    """
    if not agent:
        return None
    try:
        return _record(
            "inbound", "internal", "system",
            "<delivery-%s-%s@smart-node.local>" % (msg_id, channel_type), None,
            "system", agent,
            "Your %s message %s to %s was NOT delivered: %s. Tell whoever is "
            "waiting that it did not go out, or use another way to reach them."
            % (channel_type, msg_id, recipient, what),
            "text/plain",
            {"source": "delivery-refused", "about_message_id": msg_id,
             "channel": channel_type})
    except Exception:
        return None


def emit_blocked(channel_type, msg_id, reason, actor,
                 sender=None, recipient=None, enforced=True):
    """Record that the communication gate REFUSED a message on policy.

    Its own event type, not a `message.failed`, and that distinction is the
    point. A refusal and a dead SMTP server are different facts with different
    fixes, and until now both arrived in the journal as
    `message.failed{stage: transmit}` -- so "what did we decline to send, and to
    whom" had no answer.

    `enforced=False` is the gate running in report-only mode (CONTACT_GATE off):
    the verdict is recorded, the message still goes. That is what makes it
    possible to see what a node WOULD refuse before switching enforcement on,
    instead of discovering it by locking an operator out of their own outbox.
    """
    _emit(actor, "message.blocked", msg_id,
          {"channel": channel_type, "reason": reason,
           "from": sender, "to": recipient, "enforced": bool(enforced)})
