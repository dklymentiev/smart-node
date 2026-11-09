"""Communication gate -- may this agent write to this recipient, and may this
message wake it?

The decision, and only the decision. It is a separate module on purpose: the
design calls for an independent screening pass
over `sn_core.messages`, and this is the logic that pass will run. Today it is
called inline from `send_outbound` at the single transmit point, and from
`dispatch` at the single wake point; lifting it out later must not mean
rewriting the rule.

THE RULE

Outbound to the outside world is DENY BY DEFAULT. An agent may write to a
recipient only if that recipient is a contact granted to that agent, on that
channel. There is deliberately NO "they wrote to us first" exception: the outbound contact rule
rule 3 allowed one, and it is what let a marketing blast be answered with the
node's internal status. Having written to us is not consent to be answered.

THE SECOND RULE -- INBOUND

Whether a message may ARRIVE and whether it may WAKE AN AGENT are two
questions, and 1.5.0 answered only the first, by not asking it: inbound was not
gated at all, so that a business inbox could never miss a new customer. That
reasoning survives, and this does not overturn it -- the postman still reads
every mailbox and every message is still recorded. What the inbound gate stops
is narrower: an agent being woken, and a model being billed, for mail from
someone it was never granted.

The cost is real and is the operator's to accept: with the inbound gate ON, a
message from a stranger lands in the registry and nothing answers it until
somebody grants that sender. On a node whose mailbox is a public front door
that is the wrong posture, which is why it is a SEPARATE switch from the
outbound gate rather than the same one -- an operator may want deny-by-default
on the way out and still hear everyone on the way in. That is the 1.5.0 posture,
and it stays the default.

Internal agent-to-agent traffic is unrestricted for now -- exempt by KIND, not
by seeded grant rows, so the working loop does not depend on a catalog nobody
has filled in yet. It still travels the same registry, so it is recorded and can
be gated later without moving anything.

MATCHING is per channel, because handles are not the same shape everywhere. For
email: pull the address out of whatever the recipient field holds (`"Name"
<a@b>` is the ordinary case, not the exotic one) and compare case-insensitively.
Both sides are normalised inside the query, so a contact typed as `A@B.com` in
the catalog still matches mail arriving for `a@b.com` -- no write-time cleanup
step to forget. `contact_channels_match_idx` is functional over
`lower(handle)` for exactly this reason.

Plus-addressing is NOT stripped: `ivanov+shop@x` does not match a granted
`ivanov@x`. Stripping would WIDEN what an agent may write to, and for a
deny-by-default gate the safe direction is narrow.

A WHOLE DOMAIN can be granted, and only by saying so: a handle stored as
`@customer.com` -- an at-sign and nothing before it -- matches every address at
that domain. A normal address never becomes a pattern, so there is one matcher
here rather than two, and nothing an operator types by accident silently widens
into a wildcard. It is the one deliberate widening in this module and it exists
because the alternative is worse: a real correspondent who mails from four
addresses at one company either gets four catalog rows that must be kept in step,
or gets refused for the address nobody thought of.

Subdomains do NOT match: `@customer.com` does not cover `@mail.customer.com`,
which is a different host and often a different party. Grant it separately.
Under a domain grant plus-addressing DOES match, because the domain is granted
and every address at it is covered -- that is not the stripping rule reversed,
it is a wider grant deliberately chosen.
"""
import email.utils

from common import db
from common.config import get

# Master switch. Off by default: turning enforcement on before contacts can be
# managed (the CRUD endpoint) would leave a node that can only write to
# whatever was seeded by hand, with no way to fix it from the panel.
#   CONTACT_GATE=on   enforce
#   CONTACT_GATE=off  decide and report, change nothing  (default)
def enabled():
    return _switch("CONTACT_GATE")


# The inbound half, on its own switch. Off by default, and off is not silent
# either: the verdict is journalled whichever way it goes, so an operator can
# watch which senders WOULD stop waking their agents before any of them stop.
#   CONTACT_GATE_INBOUND=on   a message from a sender the receiving agent was
#                             not granted is recorded and never routed
#   CONTACT_GATE_INBOUND=off  decide and report, route anyway  (default)
def inbound_enabled():
    return _switch("CONTACT_GATE_INBOUND")


def _switch(name):
    return (get(name, "off") or "off").strip().lower() in (
        "1", "on", "true", "yes")


# Channels whose traffic never leaves the node. Exempt by kind.
INTERNAL_CHANNELS = ("internal", "system")


def normalise(channel_type, value):
    """The comparable form of an address on this channel."""
    v = (value or "").strip()
    if not v:
        return ""
    if channel_type == "email":
        # parseaddr turns '"Owner" <A@B.com>' into 'A@B.com' and leaves a bare
        # address alone. It returns '' for anything it cannot resolve, which is
        # then refused rather than compared as raw text.
        return email.utils.parseaddr(v)[1].strip().lower()
    return v.lower()


def handles_matching(channel_type, normalised):
    """Every catalog handle that would cover this address, most specific first.

    The gate asks the catalog "is any of these granted", so widening what may be
    granted happens HERE and nowhere else -- one list, used by both directions,
    which is what keeps the outbound and inbound answers identical.

    For email that is the address itself and, second, the `@domain` form, so a
    grant on a whole company covers every address at it. `parseaddr` has already
    reduced `"Name" <A@B>` to `a@b`, so the domain is simply what follows the
    last `@`. Nothing else expands: no subdomain walk (`@mail.x` is a different
    host, often a different party) and no bare-domain form without the at-sign
    (`customer.com` as a handle stays a literal, so an operator who pastes a
    website into the address book does not thereby grant its whole mail domain).
    """
    if not normalised:
        return []
    out = [normalised]
    if channel_type == "email" and "@" in normalised:
        domain = normalised.rsplit("@", 1)[1]
        if domain:
            out.append("@" + domain)
    return out


# One query answers the whole question: which agent owns the sending address on
# this channel, and does that agent hold a grant covering this recipient there.
# The sending agent is resolved the way the rest of the code already does it --
# `agent_channels.instance_ref` -- so the gate introduces no second notion of
# who sent a message.
_DECIDE_SQL = """
    SELECT a.slug,
           EXISTS (
               SELECT 1
                 FROM sn_config.agent_contacts g
                 JOIN sn_config.contact_channels cc ON cc.contact_id = g.contact_id
                 JOIN sn_config.contacts ct        ON ct.id = g.contact_id
                WHERE g.agent_id = a.id
                  AND cc.channel_id = ch.id
                  AND cc.channel_id = ANY (g.allowed_channels)
                  AND cc.enabled
                  AND ct.enabled
                  AND ct.archived_at IS NULL
                  AND lower(cc.handle) = ANY (%s)
           )
      FROM sn_config.channels ch
      JOIN sn_config.agent_channels bind
        ON bind.channel_id = ch.id AND bind.instance_ref = %s AND bind.enabled
      JOIN sn_config.agents a
        ON a.id = bind.agent_id AND a.enabled AND a.archived_at IS NULL
     WHERE ch.slug = %s
"""


# Every address this node answers on, for one channel. An agent writing to a
# colleague's node mailbox is internal traffic: both ends are this node, the
# owner already decided who runs here, and there is no third party to protect.
# Requiring a contact grant for it produced a node whose agents could receive a
# question by mail and not answer it -- a reply to a group was refused with
# "agent 'architect' has no contact marketing.node@<domain>", where both ends
# were the same node.
_OWN_MAILBOXES_SQL = """
    SELECT lower(ac.instance_ref)
      FROM sn_config.agent_channels ac
      JOIN sn_config.channels ch ON ch.id = ac.channel_id
     WHERE ch.slug = %s AND ac.enabled
       AND ac.instance_ref IS NOT NULL AND ac.instance_ref <> ''
"""


def agent_for_sender(channel_type, sender):
    """The agent bound to this sending address on this channel, or None."""
    if not (sender or "").strip():
        return None
    try:
        rows = db.query(
            "SELECT a.slug FROM sn_config.agent_channels b "
            "JOIN sn_config.channels ch ON ch.id = b.channel_id "
            "JOIN sn_config.agents a ON a.id = b.agent_id "
            "WHERE ch.slug = %s AND b.instance_ref = %s AND b.enabled "
            "AND a.enabled AND a.archived_at IS NULL LIMIT 1",
            (channel_type, sender), fetch=True) or []
    except Exception:
        return None
    return rows[0][0] if rows else None


def own_mailboxes(channel_type):
    """Addresses on this channel that belong to agents of this node."""
    try:
        rows = db.query(_OWN_MAILBOXES_SQL, (channel_type,), fetch=True) or []
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        # Fail CLOSED. An unknown answer must never widen the gate: if this
        # query cannot run, the node falls back to the contact rules, which is
        # the behaviour it had before this existed.
        return set()


# The same question for a row that names its agent (#9). core-api records the
# composing agent on every outbound row it queues; checking THAT agent's grants
# does not depend on the sending address identifying it, which it cannot do when
# two agents share an address or when the address is empty.
_DECIDE_BY_AGENT_SQL = """
    SELECT a.slug,
           EXISTS (
               SELECT 1
                 FROM sn_config.agent_contacts g
                 JOIN sn_config.contact_channels cc ON cc.contact_id = g.contact_id
                 JOIN sn_config.contacts ct        ON ct.id = g.contact_id
                WHERE g.agent_id = a.id
                  AND cc.channel_id = ch.id
                  AND cc.channel_id = ANY (g.allowed_channels)
                  AND cc.enabled
                  AND ct.enabled
                  AND ct.archived_at IS NULL
                  AND lower(cc.handle) = ANY (%s)
           )
      FROM sn_config.channels ch
      JOIN sn_config.agents a
        ON a.slug = %s AND a.enabled AND a.archived_at IS NULL
     WHERE ch.slug = %s
"""


# The notice "your message waits, the assistant is paused" (dispatch). The
# agent is stopped, so the ordinary rule -- an ENABLED agent's grant -- cannot
# admit it, and nothing else should: it is let out only as an answer to the one
# person whose held message it is about, and only if the stopped agent holds a
# grant for them, i.e. they could have woken it.
HELD_TOLD_TAG = "held-person-told"
HELD_NOTICE_SOURCE = "held-notice"
_HELD_NOTICE_SQL = _DECIDE_BY_AGENT_SQL.replace(
    "a.enabled AND a.archived_at IS NULL", "NOT a.enabled AND a.archived_at IS NULL")
assert _HELD_NOTICE_SQL != _DECIDE_BY_AGENT_SQL


def decide_held_notice(channel_type, recipient, metadata):
    """(allowed, reason) for a held-notice row, or None for any other row."""
    meta = metadata or {}
    if meta.get("source") != HELD_NOTICE_SOURCE:
        return None
    target = normalise(channel_type, recipient)
    rows = db.query(
        "SELECT m.sender FROM sn_core.messages m "
        "  JOIN sn_core.message_state ms ON ms.message_id = m.id "
        " WHERE m.id = %s AND m.direction = 'inbound' AND m.channel_type = %s "
        "   AND %s = ANY (ms.tags)",
        (meta.get("about_message_id"), channel_type, HELD_TOLD_TAG), fetch=True) or []
    if not rows or normalise(channel_type, rows[0][0]) != target:
        return False, ("held notice: %r is not the writer of held message %r"
                       % (recipient, meta.get("about_message_id")))
    rows = db.query(_HELD_NOTICE_SQL,
                    (handles_matching(channel_type, target), meta.get("agent"), channel_type),
                    fetch=True) or []
    if rows and rows[0][1]:
        return True, ("held notice to %s, a granted contact of stopped agent %r"
                      % (target, rows[0][0]))
    return False, ("held notice: stopped agent %r has no contact grant for %s"
                   % (meta.get("agent"), target))


def decide(channel_type, sender, recipient, agent=None):
    """(allowed, reason). Reason is recorded whichever way it goes, so the
    journal can answer "why did this leave" as well as "why did it not".

    `agent` is the composing agent when the row names it; its grants are the
    ones checked. Without it the agent is found by the sending address, and an
    EMPTY address is a refusal: it used to match whichever agent also had an
    empty binding on the channel, and name that agent in the refusal (#9)."""
    if channel_type in INTERNAL_CHANNELS:
        return True, "internal channel -- agents are unrestricted among themselves"

    target = normalise(channel_type, recipient)
    if not target:
        return False, "recipient %r is not a usable address on channel %r" % (
            recipient, channel_type)

    if target in own_mailboxes(channel_type):
        return True, ("%s is this node's own mailbox on %s -- internal traffic, "
                      "not correspondence with the outside world"
                      % (target, channel_type))

    if agent:
        rows = db.query(_DECIDE_BY_AGENT_SQL,
                        (handles_matching(channel_type, target), agent, channel_type),
                        fetch=True) or []
        if not rows:
            return False, ("agent %r is not an enabled agent of this node, so there "
                           "is no grant to check" % agent)
    elif not (sender or "").strip():
        return False, ("the message has no sending address on %r and names no "
                       "agent, so there is no grant to check" % channel_type)
    else:
        rows = db.query(_DECIDE_SQL,
                        (handles_matching(channel_type, target), sender, channel_type),
                        fetch=True) or []
    if not rows:
        # No agent owns the sending address. Not a policy denial -- a
        # misconfiguration -- but it must not be treated as permission.
        return False, ("no enabled agent is bound to %r on channel %r, so there "
                       "is no grant to check" % (sender, channel_type))

    if len(rows) > 1:
        return False, ("multiple enabled receivers on %s: %s -- no agent selected"
                       % (channel_type, ", ".join(sorted({r[0] for r in rows}))))
    slug, granted = rows[0]
    if granted:
        return True, "%s is a granted contact of agent %r on %s" % (
            target, slug, channel_type)
    return False, ("agent %r has no contact grant for %s on %s -- deny by "
                   "default" % (slug, target, channel_type))


def may_wake(channel_type, mailbox, sender):
    """(allowed, reason) -- may this inbound message wake the agent that owns
    `mailbox`?

    Same catalog and same single query as `decide`, with the arguments the
    other way round, and that flip is the whole subtlety: on the way OUT the
    node's own address is the sender and the stranger is the recipient; on the
    way IN the node's own address is the recipient and the stranger is the
    sender. `_DECIDE_SQL` resolves the agent from the node's address and matches
    the grant against the other party, so it is given `mailbox` for the first
    and `sender` for the second, whichever direction the message was travelling.
    Calling `decide()` directly with inbound arguments would silently ask
    whether the STRANGER's address owns a mailbox here -- which is never true,
    so every message would be refused for the wrong reason.

    The reasons are worded for the inbound case on purpose. They are what the
    journal shows when somebody asks why an agent stayed asleep, and "no grant
    to send to" would be an answer to a question nobody asked.
    """
    if channel_type in INTERNAL_CHANNELS:
        return True, "internal channel -- agents are unrestricted among themselves"

    origin = normalise(channel_type, sender)
    if not origin:
        return False, "sender %r is not a usable address on channel %r" % (
            sender, channel_type)

    if origin in own_mailboxes(channel_type):
        return True, ("%s is this node's own mailbox on %s -- a colleague on "
                      "this node, not a stranger" % (origin, channel_type))

    rows = db.query(_DECIDE_SQL,
                    (handles_matching(channel_type, origin), mailbox, channel_type),
                    fetch=True) or []
    if not rows:
        # Nothing here owns the mailbox the message arrived at. A
        # misconfiguration rather than a policy refusal -- but an agent that
        # cannot be identified cannot be woken on its own authority either.
        return False, ("no enabled agent is bound to %r on channel %r, so there "
                       "is no grant to check" % (mailbox, channel_type))

    if len(rows) > 1:
        return False, ("multiple enabled receivers on %s: %s -- no agent selected"
                       % (channel_type, ", ".join(sorted({r[0] for r in rows}))))
    slug, granted = rows[0]
    if granted:
        return True, "%s is a granted contact of agent %r on %s" % (
            origin, slug, channel_type)
    return False, ("agent %r was not granted %s as a contact on %s -- recorded, "
                   "not answered" % (slug, origin, channel_type))
