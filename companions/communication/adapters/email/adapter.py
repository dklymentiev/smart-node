"""Email channel adapter -- Smart Node communication component.

Cron-invoked, one pass per tick (email is not latency-sensitive -- a
poll is the correct ingestion mechanism, per the runtime model). Each
UNSEEN message becomes one immutable `messages` row (direction inbound)
and N `message_attachments` rows if the message carries attachments. All
rows are committed before mark_seen(), so a crash between the commit and
the flag-flip re-ingests rather than loses.

Transport only -- no reasoning. Outbound delivery is `deliver()`, called
by the dispatch layer (the secretary), which also records the row.

Run one pass:  python -m adapters.email.adapter
"""
import logging

from common import attachments as att_store
from common import binding, db, mailrules
from common.config import LOG, get
from adapters.email.transport import EmailTransport

log = logging.getLogger(__name__)

CHANNEL_TYPE = "email"


def _channel_id():
    """This mailbox -- the `channel_id` for every row this adapter writes."""
    return get("EMAIL_CHANNEL_ID") or get("IMAP_USER") or "email"


def _thread_id(msg):
    """Thread handle: the ROOT of the chain, else its own id.

    `References` lists the ids of the whole chain, oldest first, so its first id is
    the root. This used to read `In-Reply-To`, which names the letter being
    answered, the immediate parent: every reply opened a thread of its own (four
    letters of one subject, four handles, two rows each), the agent's conversation
    session is keyed by this handle so each reply of the owner started a
    conversation with no memory of the last, and `inbox_read(thread_id)` returned a
    fragment of the chain. A letter with no `References` falls back to the letter it
    answers, then to itself."""
    refs = (msg.get("references") or "").split()
    root = refs[0] if refs else ""
    return (root or msg.get("in_reply_to") or msg.get("message_id") or "").strip("<> ")


def _process_attachments(raw_parts, arrived_at):
    """Apply caps and produce prepared attachment dicts.

    Returns (prepared, dropped_count, dropped_reason_str).

    prepared        list of dicts from att_store.prepare_attachment; '_data'
                    is present and must be flushed before the DB transaction.
    dropped_count   number of parts that exceeded a cap.
    dropped_reason  human-readable string for the NDR / body note, or "".
    """
    max_size = att_store.MAX_SIZE_BYTES
    max_count = att_store.MAX_PER_MESSAGE
    prepared = []
    dropped = []

    for raw in raw_parts:
        data = raw["data"]
        fname = raw["filename"] or "unnamed"
        ctype = raw["content_type"]
        if len(prepared) >= max_count:
            dropped.append(
                "%s dropped: count limit (%d attachments max)" % (fname, max_count)
            )
            continue
        if len(data) > max_size:
            dropped.append(
                "%s dropped: exceeds size limit (%s, max %s)"
                % (fname,
                   att_store.format_size(len(data)),
                   att_store.format_size(max_size))
            )
            continue
        prepared.append(att_store.prepare_attachment(fname, ctype, data, arrived_at))

    reason = "; ".join(dropped) if dropped else ""
    return prepared, len(dropped), reason


def run_once():
    """One ingestion pass: poll UNSEEN, record each, then flag it Seen."""
    transport = EmailTransport()
    messages = transport.poll_unseen()
    if not messages:
        return 0
    mailbox = _channel_id()
    # One lookup per pass, not per message: the watermark is a property of the
    # binding, and it cannot change while a pass is running in any way that
    # should affect this pass.
    watermark = binding.history_before(CHANNEL_TYPE, mailbox)
    recorded = 0
    history = 0
    for msg in messages:
        # THE ROW'S IDENTITY IS NOT THE PROTOCOL'S IDENTITY, and conflating
        # them cost us letters. One message sent to three of our mailboxes is
        # delivered three times, and every copy carries the SAME Message-ID.
        # With external_id = Message-ID the unique index
        # (channel_type, external_id) made the second and third copies dedup
        # hits: one row survived, two agents never saw a letter that was
        # sitting in their own mailbox, and the poller logged it as nothing new.
        #
        # So the row is identified by the pair that actually distinguishes it,
        # the receiving box and the message. The Message-ID itself is kept in
        # metadata, because the reply path needs the REAL one for In-Reply-To
        # (dispatch.py) -- that is the half of this that must not be scoped.
        wire_id = msg["message_id"] or ("uid:" + msg["uid"])
        external_id = "%s|%s" % (mailbox, wire_id)
        arrived_at = msg.get("arrived_at")
        is_history = binding.is_history(arrived_at, watermark,
                                        where="%s in %s" % (external_id, mailbox))

        # Process attachments: apply caps, prepare metadata dicts.
        raw_parts = msg.get("attachments") or []
        prepared, dropped_count, drop_reason = _process_attachments(raw_parts, arrived_at)

        # Build the attachment summary lines and metadata handles for the body
        # and metadata, exactly as the attachment contract specifies.
        summary_lines = [att_store.attachment_summary_line(a) for a in prepared]
        att_handles = [att_store.attachment_handle(a) for a in prepared]
        if drop_reason:
            summary_lines.append("[%s]" % drop_reason)

        body = msg["body"] or ""
        if summary_lines:
            body = (body.rstrip() + "\n\n" + "\n".join(summary_lines)) if body else "\n".join(summary_lines)

        meta = {
            "uid": msg["uid"],
            "subject": msg["subject"],
            # The RFC822 Message-ID exactly as it arrived. external_id is
            # scoped to the receiving mailbox and is NOT this value.
            "message_id": msg["message_id"] or "",
            # The audience the sender chose, kept so a reply can answer the
            # room rather than the speaker alone.
            "to_addrs": msg.get("to_addrs") or [],
            "cc_addrs": msg.get("cc_addrs") or [],
            "from_name": msg["from_name"],
            "in_reply_to": msg["in_reply_to"],
            "references": msg["references"],
            # Machine-mail signals + the verdict itself. The row is still
            # recorded (it is evidence); what changes is that nothing
            # answers it -- see common/mailrules.
            "auto_submitted": msg.get("auto_submitted", ""),
            "precedence": msg.get("precedence", ""),
            "return_path": msg.get("return_path", ""),
            "x_failed_recipients": msg.get("x_failed_recipients", ""),
            "list_id": msg.get("list_id", ""),
            "auto_generated": mailrules.is_auto_generated(msg, msg["from_addr"]),
            # Kept apart from received_at: one is when the message arrived,
            # the other is the rule that was applied to it.
            "history": is_history,
            "history_before": watermark.isoformat() if watermark else "",
        }
        if att_handles:
            meta["attachments"] = att_handles

        # Write attachment bytes to disk BEFORE the DB transaction.
        # Crash-safe: tmp-then-rename; orphaned files are cleaned by the
        # expiration sweep (not a data-loss scenario -- the DB row is never
        # written, so no access is possible to the orphan).
        flush_errors = []
        flushed = []
        for a in prepared:
            try:
                att_store.flush_attachment_bytes(a)
                flushed.append(a)
            except Exception as exc:
                log.error("attachment flush failed for %s in %s: %s",
                          a["filename"], external_id, exc)
                flush_errors.append(a["filename"])

        # Strip _data before passing to the DB function.
        db_attachments = [{k: v for k, v in a.items() if k != "_data"}
                          for a in flushed]

        msg_id, was_new = db.record_inbound_with_attachments(
            channel_type=CHANNEL_TYPE,
            channel_id=mailbox,
            external_id=external_id,
            thread_id=_thread_id(msg),
            sender=msg["from_addr"],
            # The box that actually received this, not the To header. A copy
            # to three of our mailboxes carries one To header, so stamping the
            # header made the three rows indistinguishable. It is also what
            # /api/agents/with-pending-work already joins on
            # (agent_channels.instance_ref = m.recipient), so the row now says
            # what the rest of the node reads it as saying.
            recipient=mailbox,
            content=body,
            # The registry is immutable, so what is stamped here is the
            # permanent answer to "why was this never answered" -- readable
            # long after the binding row has moved on.
            received_at=arrived_at,
            history=is_history,
            metadata=meta,
            attachments=db_attachments,
        )

        # Flag Seen whether the row was new or a dedup hit -- either way
        # this message is accounted for and must not be polled again.
        # Critically, mark_seen runs AFTER both the message row and all
        # attachment rows are committed, so a crash before this point
        # causes re-ingestion, not loss.
        transport.mark_seen(msg["uid"])

        if was_new:
            recorded += 1
            if is_history:
                history += 1
            LOG(CHANNEL_TYPE, "inbound -> messages id=%s ext=%s from=%s%s: %r"
                " (%d attachment(s)%s)"
                % (msg_id, external_id, msg["from_addr"],
                   " [HISTORY -- predates the binding, will not be answered]"
                   if is_history else "",
                   msg["subject"][:60],
                   len(flushed),
                   ", %d flush error(s)" % len(flush_errors) if flush_errors else ""))
        elif msg_id:
            # Dedup: message already existed (re-ingest after crash).
            # Attachment rows were already attempted above; log quietly.
            LOG(CHANNEL_TYPE, "dedup id=%s ext=%s -- attachment rows retried"
                % (msg_id, external_id))

    if history:
        LOG(CHANNEL_TYPE, "%d of %d message(s) in %s predate the binding "
                          "(watermark %s) -- recorded as history, not answered"
            % (history, recorded, mailbox, watermark))
    return recorded


# The second exit is gone.
#
# deliver() used to live here: it transmitted an email and recorded it
# afterwards. It had had no callers now made send_outbound the sole
# transmitter, and it carried its own DO-NOT-CALL warning naming the incident:
# a node whose allowlist forbade everything mailed seven external addresses
# through exactly this route, because a second exit is not a duplicate of the
# first, it is a path with no egress guard on it. It also sent BEFORE it
# recorded, so a crash between the two statements put mail on the wire that the
# registry never heard of.
#
# It survived as "the reference implementation of message assembly". That is not
# a reason to keep a loaded gun in the tree while the contact gate is being
# switched on: the assembly it demonstrated lives in send_outbound, which is the
# only place allowed to do it.
#
# To send an email: write an outbound row (db.record_outbound) and let the timer
# transmit it.


def main():
    LOG(CHANNEL_TYPE, "poll pass start")
    recorded = run_once()
    LOG(CHANNEL_TYPE, "poll pass done -- %d new message(s) recorded" % recorded)


if __name__ == "__main__":
    main()
