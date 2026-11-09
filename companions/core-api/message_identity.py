"""One identity verdict per inbound message.

The read side of "identity across channels" shipped and works: GET
/api/messages/by-contact answers "everything this person said, on every
channel" by looking their handles up in sn_config.contact_channels. The write
side never did. Nothing wrote sn_core.message_identity, and nothing ever added
a handle to the map, so the map grew only when a human typed into the panel.
Measured on this node 2026-09-10: 0 identity rows against 1596 inbound
messages.

Two things happen here, both best-effort, neither ever fatal to an ingest:

  1. THE VERDICT -- one row in sn_core.message_identity: which contact this
     message is from, the handle it arrived under, and how strongly that is
     known. Written once, at arrival, never revised (the table is keyed by
     message_id and the insert is ON CONFLICT DO NOTHING).

  2. THE MAP -- a handle that arrived through an authenticated door is added to
     contact_channels for that contact, with verified_by='session'. That is the
     part that makes the next channel recognise the person without anyone
     maintaining a list by hand, and it is deliberately the ONLY way a handle
     is learned automatically: a claim in a message body proves nothing, so
     'declared' is recorded and the map is left alone.

Trust vocabulary is the table's own CHECK: session | proved | linked |
declared | unknown.
"""


def stamp(cur, message_id: int, channel_type: str, handle: str,
          proof: str | None = None, evidence: dict | None = None):
    """Record the verdict for one inbound message. Returns the trust recorded.

    The rule itself lives in sn_core.stamp_message_identity(), in the database,
    because the other writer is not this codebase: the communication companion's
    email, telegram and slack adapters write their rows straight to this
    database and share no Python with core-api. A rule that must not drift
    belongs where both can call it.

    The caller is expected to wrap this: a verdict that cannot be written must
    never cost the message it describes.
    """
    from psycopg2.extras import Json          # same import the routes use

    cur.execute(
        "SELECT sn_core.stamp_message_identity(%s, %s, %s, %s, %s) AS trust",
        (message_id, channel_type, handle, proof, Json(evidence or {})),
    )
    row = cur.fetchone()
    return row["trust"] if row else None
