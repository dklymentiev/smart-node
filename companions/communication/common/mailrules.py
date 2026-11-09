"""When NOT to answer -- Smart Node communication component.

An agent that answers every inbound message will eventually answer a machine.
On 2026-08-02 mail-node replied three times to bounce notices from the mail server:
the bounce text went into an LLM session and the reply went back to
mailer-daemon, which bounces, which arrives, which is answered. Nothing
stopped it except the mail volume being low that afternoon.

Two independent guards, because either one alone has a hole:

  ingest side   an auto-generated message is recorded but never answered
  transmit side a reply is never addressed to a system mailbox

Plus a ceiling per conversation, which catches loops these two do not see (two
agents talking to each other, a customer autoresponder that looks human).
"""
from common import db

# Local-parts that never want a reply. A message from one of these is machinery,
# and a reply addressed to one of them is the first turn of a loop.
SYSTEM_LOCALPARTS = (
    "mailer-daemon", "mail-daemon", "postmaster", "no-reply", "noreply",
    "do-not-reply", "donotreply", "bounce", "bounces", "nobody",
)

# Replies allowed on one thread inside the window. A human exchange does not
# reach this; a loop reaches it in seconds.
REPLY_CAP = 5
REPLY_WINDOW_MINUTES = 60


def _localpart(addr):
    a = (addr or "").strip().lower().strip("<>")
    return a.split("@", 1)[0] if "@" in a else ""


def _is_system_localpart(addr):
    """Match by PREFIX, not equality: real senders decorate the local-part
    (noreply-dmarc-support@google.com, mailer-daemon+bounce@...), and an exact
    match lets exactly those through -- the ones that send the most machine mail.
    """
    local = _localpart(addr)
    if not local:
        return False
    return any(local == name or local.startswith(name + "-")
               or local.startswith(name + "+") or local.startswith(name + ".")
               for name in SYSTEM_LOCALPARTS)


def is_auto_generated(metadata, sender=""):
    """True when this inbound message came from machinery rather than a person.

    Reads the headers the ingest stores. Auto-Submitted and Precedence are the
    standard signals (RFC 3834); X-Failed-Recipients and a null Return-Path mark
    a bounce; List-Id marks a mailing list. The sender local-part is the last
    resort, for servers that send bounces with none of the above.
    """
    meta = metadata or {}

    auto = str(meta.get("auto_submitted") or "").strip().lower()
    if auto and auto != "no":
        return True

    precedence = str(meta.get("precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk", "auto_reply"):
        return True

    if str(meta.get("x_failed_recipients") or "").strip():
        return True

    # Only a LITERAL null sender counts. A missing Return-Path header is normal
    # and must not be read as a bounce.
    if str(meta.get("return_path") or "").strip() == "<>":
        return True

    if str(meta.get("list_id") or "").strip():
        return True

    return _is_system_localpart(sender)


def is_unanswerable_recipient(addr):
    """True when a reply must never be transmitted to this address."""
    a = (addr or "").strip().strip("<>")
    if not a or "@" not in a:
        return True
    return _is_system_localpart(a)


def reply_cap_reached(channel_type, thread_id, sender=None, cap=REPLY_CAP,
                      window_minutes=REPLY_WINDOW_MINUTES):
    """True when THIS SENDER already had `cap` outbound messages on this thread
    inside the window. Counts the registry, so it holds across restarts and
    across the two delivery paths (fast dispatch and the outbound sender).

    Per sender, not per thread. The loop this guards
    against is one mailbox answering the same conversation over and over. A
    group thread is many mailboxes answering once each, and counting the room
    punished the second for looking like the first: on the field node a roll
    call to seven colleagues on one thread had its fifth answer onward refused
    as a loop, silently for the agents refused, because the reason lands on the
    row and in the journal and neither is something an agent reads.

    `sender` is optional so a caller that passes none keeps the old, stricter
    count rather than silently losing the ceiling.

    A lookup failure returns False: a broken counter must not stop real replies.
    """
    if not thread_id:
        return False
    sql = ("SELECT count(*) FROM sn_core.messages "
           "WHERE direction = 'outbound' AND channel_type = %s "
           "AND thread_id = %s "
           "AND received_at > now() - (%s || ' minutes')::interval")
    args = [channel_type, thread_id, str(window_minutes)]
    if sender:
        # lower(): the sender is a mailbox, and the two delivery paths do not
        # agree on its case.
        sql += " AND lower(sender) = lower(%s)"
        args.append(sender)
    try:
        rows = db.query(sql, tuple(args), fetch=True) or []
        return bool(rows) and rows[0][0] >= cap
    except Exception:
        return False
