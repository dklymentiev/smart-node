"""One way for the node to tell its owner that a guard fired.

A guard nobody hears about is not protection: on 2026-09-19 a hard budget
stopped every agent turn on a live node, stamped itself notified, and sent
nothing, because the notifier was configured off. So this module exists to make
the telling explicit and the same everywhere.

Two rules it enforces, and both are here rather than at each call site because
both were got wrong once already:

  * no invented sender. `SN_NOTIFY_SENDER` is published by communication.sh only
    after it has matched the address to installed credentials. Unset means
    refuse, not guess: an address the relay cannot authenticate as produces a
    message that is written, blocked at the gate, and reported as sent.
  * the recipient is the owner as the registry has him, `sn_config.user_channels`,
    never an address a caller passed in. The owner changes his mail in one place.

The caller owns the transaction. Queue the message and claim whatever marks the
alert as delivered-once inside the SAME transaction, in that order, so a failure
to queue leaves nothing claimed and the next occurrence can still speak.
"""

from __future__ import annotations

import email.utils
import os

from psycopg2.extras import Json

EMAIL_SENDER = os.environ.get("SN_NOTIFY_SENDER") or None
NODE_DOMAIN = os.environ.get("SMART_NODE_DOMAIN") or "smart-node.local"


class OwnerUnreachable(RuntimeError):
    """No configured way to reach the owner. The caller decides what that means
    for its own state -- usually: do not claim, so the next occurrence retries."""


def owner_email_row(cur):
    """The owner's enabled email channel, or None."""
    cur.execute(
        """SELECT u.id, u.tenant_id, u.name, uc.address
             FROM sn_config.users u
             JOIN sn_config.user_channels uc ON uc.user_id = u.id
            WHERE u.role = 'super-admin'
              AND u.active = true
              AND u.archived_at IS NULL
              AND uc.channel_slug = 'email'
              AND uc.enabled = true
            ORDER BY u.id
            LIMIT 1"""
    )
    row = cur.fetchone()
    return row if row and row["address"] else None


def queue_owner_email(cur, *, subject: str, text: str, source: str,
                      extra: dict | None = None) -> dict:
    """Write ONE outbound email to the owner on the caller's open transaction.

    Returns {"message_id": int, "external_id": str, "address": str}.
    Raises OwnerUnreachable when there is no sender or no owner channel, before
    anything is written.
    """
    if not EMAIL_SENDER:
        raise OwnerUnreachable(
            "SN_NOTIFY_SENDER is unset: refusing to report an alert as sent "
            "without a mailbox the relay can authenticate as"
        )
    owner = owner_email_row(cur)
    if owner is None:
        raise OwnerUnreachable("the node owner has no enabled email channel")

    external_id = email.utils.make_msgid(domain=NODE_DOMAIN)
    metadata = {"subject": subject, "source": source, "from_name": "Smart Node"}
    metadata.update(extra or {})
    cur.execute(
        """INSERT INTO sn_core.messages
                  (channel_type, channel_id, external_id, direction, sender,
                   recipient, content, content_type, metadata, tenant_id)
           VALUES ('email', %s, %s, 'outbound', %s, %s, %s,
                   'text/plain', %s, %s)
        RETURNING id""",
        (EMAIL_SENDER, external_id, EMAIL_SENDER, owner["address"], text,
         Json(metadata), owner["tenant_id"]),
    )
    return {"message_id": cur.fetchone()["id"],
            "external_id": external_id,
            "address": owner["address"]}
