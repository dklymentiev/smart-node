"""Resolve enabled channel receivers without choosing between conflicting bindings."""
from events_emit import emit


RECEIVERS_SQL = """
    SELECT a.slug
      FROM sn_config.agent_channels ac
      JOIN sn_config.agents a ON a.id = ac.agent_id
      JOIN sn_config.channels c ON c.id = ac.channel_id
     WHERE c.slug = %s AND ac.instance_ref = %s AND ac.enabled
       AND a.enabled AND a.archived_at IS NULL
"""


def channel_receivers(cur, channel, instance_ref):
    cur.execute(RECEIVERS_SQL, (channel, instance_ref))
    return sorted(r["slug"] for r in cur.fetchall())


def block_ambiguous_inbound(cur):
    """Keep duplicate-bound inbound mail out of both consumers and agent inboxes.

    The state change and event share the caller's transaction. A conditional
    UPDATE lets concurrent polls report the conflict only once per message.
    Existing bindings are evidence and are never changed here.
    """
    cur.execute("""
        SELECT m.id, m.channel_type, m.recipient,
               array_agg(DISTINCT a.slug ORDER BY a.slug) AS receivers
          FROM sn_core.messages m
          JOIN sn_core.message_state ms ON ms.message_id = m.id AND ms.status = 'new'
          JOIN sn_config.channels c ON c.slug = m.channel_type
          JOIN sn_config.agent_channels ac
            ON ac.channel_id = c.id AND ac.instance_ref = m.recipient AND ac.enabled
          JOIN sn_config.agents a ON a.id = ac.agent_id AND a.enabled
                                   AND a.archived_at IS NULL
         WHERE m.direction = 'inbound' AND m.channel_type NOT IN ('internal', 'system')
         GROUP BY m.id, m.channel_type, m.recipient
        HAVING count(DISTINCT a.id) > 1
    """)
    for row in cur.fetchall():
        reason = ("Mailbox %r on %s has multiple enabled receivers: %s. "
                  "No recipient selected." % (row["recipient"], row["channel_type"],
                                              ", ".join(row["receivers"])))
        cur.execute("UPDATE sn_core.message_state SET status = 'blocked', error = %s "
                    "WHERE message_id = %s AND status = 'new' RETURNING message_id",
                    (reason, row["id"]))
        if cur.fetchone():
            emit(cur, actor="component:core-api", event_type="message.routing_denied",
                 entity_type="message", entity_id=str(row["id"]),
                 payload={"reason": reason, "channel": row["channel_type"],
                          "recipient": row["recipient"], "receivers": row["receivers"],
                          "conflict": "multiple-receivers"})
