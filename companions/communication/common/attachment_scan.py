"""The attachment scan stage, as the channel ingest runs it.

core-api/attachment_scan.py is the stage the Studio chat upload runs, and
GET /api/attachments/{id}/content serves only what the stage approved. The
channel ingest (email, and every adapter that stores attachments through
common.db) stored every file as `received` and never ran the stage, so since
the API stopped serving `received` files (2026-09-21) no agent could open
an attachment that arrived by mail.

The companions share no Python package, so the rule is restated here, word for
word. tests/test_attachment_scan.py loads both modules and fails the moment
their answers differ: one rule, two copies that cannot drift apart.

Pure functions only: no I/O, no model, no network. The caller stores the
verdict and its reason on the row and emits the event.
"""

APPROVED = "approved"
RECEIVED = "received"

# The sources this stage knows. A desk upload is the owner's own file in the
# Studio chat; anything else is a channel name (email, telegram, ...).
DESK_OWNER = "desk-owner"


def verdict(source: str, trusted: bool) -> tuple:
    """(status, reason) for one attachment.

    source  DESK_OWNER for the owner's upload in the Studio chat, else the
            channel the file arrived on.
    trusted the contact gate's answer for the sender (deny by default);
            ignored for DESK_OWNER, which is the owner by construction.

    Phase 1 decides nothing from the content. What it does decide is recorded,
    so a later rule can tell a file it approved from one nobody looked at.
    """
    if source == DESK_OWNER:
        return APPROVED, "owner-upload"
    if trusted:
        return APPROVED, "trusted-sender"
    return RECEIVED, "unscanned"
