"""What the node knows about a channel binding -- the history watermark.

A channel instance (a mailbox, a bot, a workspace) is bound to an agent at a
moment in time. Everything that was already sitting in it at that moment is
HISTORY: it is context and evidence, and the agent may read it, but nothing
routes it for a reply. Only what arrives afterwards is an inbox.

This exists because the two are otherwise indistinguishable. Connecting a real
business inbox replayed months of accumulated mail as live inbound and the
agent answered it, reaching an outside party. No existing guard could have
stopped that, and each for its own reason: the egress guard allowed it
correctly (they had written to us), and the machine-mail filter classified it
correctly (a marketing blast is human-sent). Age is not permission and
permission is not age -- a correspondent who wrote in May is legitimately
allowed, and their May question still gets answered as if it arrived today.

The decision belongs at INGEST and nowhere else. Both consumers -- the
fast-dispatch loop and the Runner's wake path -- select on `message_state`
status 'new', so a message that is never 'new' is never routed by either,
without a second guard in either of them.
"""
from common import db
from common.config import LOG

__all__ = ["history_before", "is_history"]

_SQL = """
    SELECT min(ac.history_before)
      FROM sn_config.agent_channels ac
      JOIN sn_config.channels c ON c.id = ac.channel_id
     WHERE c.slug = %s AND ac.instance_ref = %s
"""


def history_before(channel_slug, instance_ref):
    """The watermark for one channel instance, or None if it has no binding.

    None means no agent is bound to this instance, which is a different fact
    from "bound with no watermark" -- the column is NOT NULL, so the latter
    cannot occur. Nothing can route a message addressed to an unbound instance
    anyway (both consumers resolve the agent through this same table), so None
    is safe to treat as "not history".

    The aggregate retains the earliest history boundary without selecting an
    owner. Routing independently refuses multiple enabled receivers.
    """
    if not instance_ref:
        return None
    rows = db.query(_SQL, (channel_slug, instance_ref), fetch=True) or []
    return rows[0][0] if rows else None


def is_history(arrived_at, watermark, where=""):
    """Should this message be recorded as history rather than routed?

    Three cases, and the third is the one worth stating:

      * no watermark -> not history (nothing is bound; see history_before)
      * arrived at or before the watermark -> history
      * arrival time UNKNOWN, with a watermark present -> history

    The last is deliberate. A message whose arrival we cannot establish is not
    thereby new; claiming it is new is precisely the assumption that mailed a
    stranger. Failing this way is loud -- the agent stays silent and the row
    says so -- while failing the other way is silent and leaves the node.
    """
    if watermark is None:
        return False
    if arrived_at is None:
        LOG("binding", "unknown arrival time%s -- recording as history, not "
                       "as new" % (" for " + where if where else ""))
        return True
    return arrived_at <= watermark
