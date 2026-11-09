#!/usr/bin/env python3
"""Outbound sender -- Smart Node communication component.

The transmit half of the loop, and the mirror image of poll_mailboxes:
the secretary (or any consumer) does not send -- it only writes its reply
as an outbound `messages` row (direction='outbound'); the AFTER INSERT
trigger seeds that row a `message_state` of 'new'. This pass drains those
queued rows and transmits each on its own channel, then advances the row
to 'processed' (or 'failed', with the error recorded). Nothing here
reasons or composes -- transport only, channel-agnostic by dispatch.

email identity: an outbound row's `sender` is the node mailbox the reply
goes out as (the address the original arrived on). Its per-mailbox SMTP
password comes from the mail companion's generate-once store
(AGENT_ACCOUNTS_FILE), exactly as the inbound poller resolves IMAP creds.
The RFC822 Message-ID was minted when the row was written and stored as
the row's external_id -- we send WITH that id so the registry and the
wire agree.

Cron-invoked, one pass per tick. Run from this directory so `common` and
`adapters` resolve as packages:  python send_outbound.py
"""
import email.utils
import re

from common import contacts, db, mailcreds, mailrules
from common.config import LOG, get

_TAG = "send-outbound"

CREDS_FILE = get("AGENT_ACCOUNTS_FILE",
                 "/opt/smart-node/mail/agent-accounts.env")

# Every queued outbound row, oldest first. Column order is consumed
# positionally below -- common.db.query returns plain tuples.
SCAN_SQL = """
    SELECT m.id, m.channel_type, m.sender, m.recipient, m.content,
           m.external_id, m.thread_id, m.metadata, m.recipients, ms.send_attempts
      FROM messages m
      JOIN message_state ms ON ms.message_id = m.id
     WHERE m.direction = 'outbound'
       AND ms.status = 'new'
       AND (ms.send_next_at IS NULL OR ms.send_next_at <= now())
     ORDER BY m.id
"""


def _cred_key(addr):
    """The PW_ key the mail companion stored for an address (@ and . -> _)."""
    return "PW_" + addr.replace("@", "_").replace(".", "_")


# --- Egress guard -----------------------------------------
# Outbound email reaches only recipients this node is permitted to write to, so
# a mistaken or hallucinated address can never receive mail. Refusal happens at
# the transmit point -- the row is marked failed with the reason and nothing
# leaves. now this is the ONLY point at which mail leaves, so this guard
# is now effective rather than decorative.
#
# The rule allows replies to known correspondents as well as configured recipients, and the distinction is
# load-bearing: "for a REPLY, the recipient must be the sender of the inbound
# being answered (they wrote to us first); for an agent-INITIATED message, the
# recipient must be on a configured allowlist. A static whitelist alone would
# wrongly block real customers, so reply-to-the-known-correspondent is the rule."
#
# So there are two ways to be allowed:
#   1. the recipient wrote to this node first, on this thread -- answering a
#      correspondent is always permitted and needs no configuration. This is
#      what keeps a node able to do its job out of the box.
#   2. the recipient is covered by the operator's configured allowlist -- the
#      only way to reach an address that did NOT write first, i.e. mail the node
#      originates on its own initiative.
# Configure neither and the node can still answer everyone who writes to it, and
# can cold-mail nobody. That is the intended posture, and it is what makes an
# unset allowlist safe instead of either useless or paralysing.
#
#   EMAIL_ALLOWED_DOMAINS     e.g. example.com,partner.example.com
#   EMAIL_ALLOWED_ADDRESSES   e.g. owner@example.com
def _csv(name):
    return tuple(p.strip().lower()
                 for p in (get(name, "") or "").split(",") if p.strip())


ALLOWED_EMAIL_DOMAINS = _csv("EMAIL_ALLOWED_DOMAINS")
ALLOWED_EMAIL_ADDRESSES = _csv("EMAIL_ALLOWED_ADDRESSES")

# --- Content check -------------------------------------------------
# Enforcing with no report-only mode: there is no legitimate case for a message
# that solicits a credential value from a human. This node passes secrets by
# reference; agents must not ask people to paste or share a token. The contact
# gate has a report-only phase because it requires a seeded catalog; the content
# check requires nothing and is always on.
#
# Pattern design (accepted by the architect and the owner):
#   Noun alone: "the token has expired" -> allowed (information, not solicitation).
#   Verb alone: "can you send me the invoice" -> allowed (no credential noun).
#   Both:       "please paste the token value" -> blocked.
#
# False positives are acceptable. An agent that writes "what is the password for
# the Studio VPN?" should be blocked; it can rephrase to "does the Studio VPN
# credential need updating?" The bluntness is deliberate.
#
# Internal and system channels are not checked: agent-to-agent traffic is within
# the node perimeter and unrestricted.
_CRED_NOUNS = re.compile(
    r'\b(token|key|password|secret|credential|api[-_\s]?key|pat|passphrase|'
    r'access[-_\s]?token|api[-_\s]?secret|private[-_\s]?key)\b',
    re.IGNORECASE)

_REQUEST_VERBS = re.compile(
    r'\b(paste|send\s+me|share|provide|give\s+me|retrieve|type\s+the|'
    r'copy\s+(and\s+)?send|forward\s+(me\s+)?the|what\s+is\s+the|'
    r'can\s+you\s+send)\b',
    re.IGNORECASE)

# Channels exempt from the content check (node-internal perimeter).
_INTERNAL_CHANNELS = {"internal", "system"}


def _content_allowed(channel_type, content):
    """(allowed, reason). Always enforcing: no report-only mode.

    Returns (True, "") when the message may proceed; (False, reason) when it
    contains a credential solicitation that must be blocked.
    """
    if channel_type in _INTERNAL_CHANNELS:
        return True, ""
    if not content:
        return True, ""
    if _CRED_NOUNS.search(content) and _REQUEST_VERBS.search(content):
        return False, (
            "body contains a credential solicitation -- this node passes secrets "
            "by reference; agents must not ask humans for credential values")
    return True, ""


# --- Dry-run suppression -------------------------------------------
# DRY_RUN_AGENTS   comma-separated agent slugs whose outbound is suppressed.
# DRY_RUN_CHANNELS comma-separated channel types (email, telegram, ...).
#
# When a match is found the message row is advanced to 'dry_run' and the
# composed content is logged; nothing is transmitted. Both checks run in
# send_outbound -- the transport is never dialled -- so this covers email,
# telegram, slack, and any future channel through one path.
#
# The flag is set by the OPERATOR, not the agent. An agent body cannot clear
# it by choosing not to mention DRY_RUN; that is the point (an
# operator letter dated 2026-09-01): "the reason we are rewriting the body in the first
# place is that this agent ignored a body instruction."
DRY_RUN_AGENTS   = _csv("DRY_RUN_AGENTS")
DRY_RUN_CHANNELS = _csv("DRY_RUN_CHANNELS")


def _agent_slug_for_sender(sender):
    """Resolve a sending mailbox address to an agent slug, or None.

    The mailbox stored as sender in outbound rows is the instance_ref in
    sn_config.agent_channels. The join to agents gives us the slug the
    operator used in DRY_RUN_AGENTS. Returns None when the mailbox does not
    resolve -- non-agent senders (system rows) are not suppressed."""
    if not sender:
        return None
    try:
        rows = db.query(
            "SELECT a.slug FROM sn_config.agent_channels ac "
            "JOIN sn_config.agents a ON a.id = ac.agent_id "
            "WHERE ac.instance_ref = %s LIMIT 1",
            (sender,), fetch=True) or []
        return rows[0][0] if rows else None
    except Exception as exc:
        LOG(_TAG, "dry-run slug lookup failed for %s: %s" % (sender, exc))
        return None


def _is_dry_run(channel_type, sender):
    """True when this send should be suppressed by the operator's dry-run flag.

    Checked per-channel first (cheapest, no DB query); per-agent second.
    Both are additive -- if either matches the send is suppressed."""
    if channel_type in DRY_RUN_CHANNELS:
        return True, "channel '%s' in DRY_RUN_CHANNELS" % channel_type
    if DRY_RUN_AGENTS:
        slug = _agent_slug_for_sender(sender)
        if slug and slug in DRY_RUN_AGENTS:
            return True, "agent '%s' in DRY_RUN_AGENTS" % slug
    return False, ""


def _wrote_to_us_first(addr, thread_id):
    """True if this address sent us inbound mail -- on this thread when we have
    one, anywhere when we do not. Answering a correspondent is the reply case of
    the reply-to-correspondent rule and needs no allowlist entry.

    Scoping to the thread when it is known is deliberate: it means an allowlist
    of nothing permits answering, but never permits picking a stranger's address
    out of the registry and originating mail to them."""
    a = (addr or "").strip().lower()
    if not a or "@" not in a:
        return False
    if thread_id:
        rows = db.query(
            "SELECT 1 FROM messages WHERE direction = 'inbound'"
            "   AND channel_type = 'email' AND lower(sender) = %s"
            "   AND thread_id = %s LIMIT 1",
            (a, thread_id), fetch=True)
    else:
        rows = db.query(
            "SELECT 1 FROM messages WHERE direction = 'inbound'"
            "   AND channel_type = 'email' AND lower(sender) = %s LIMIT 1",
            (a,), fetch=True)
    return bool(rows)


def _recipient_set(recipient, recipients):
    """Every address this row goes to, each with its role.

    The scalar `recipient` stays the primary and stays first, so a row written
    by any caller that has not been changed behaves exactly as it always did.
    The set is additive on top of it. Order is preserved and addresses are
    de-duplicated case-insensitively, because the same person appearing twice
    must not be gated twice or emailed twice.

    A bare string in the set is a cc: the only reason to list an address
    alongside the primary one is to copy them.
    """
    out, seen = [], set()

    def add(addr, role):
        a = (addr or "").strip()
        if not a or a.lower() in seen:
            return
        seen.add(a.lower())
        out.append({"addr": a, "role": role})

    add(recipient, "to")
    for r in (recipients or []):
        if isinstance(r, str):
            add(r, "cc")
        elif isinstance(r, dict):
            add(r.get("addr"), (r.get("role") or "cc").strip().lower())
    return out


def _email_recipient_allowed(addr, thread_id=None):
    """allowed if they wrote to us first (a reply), or if the
    operator's allowlist covers them (an agent-initiated message). Everything
    else is refused so a bad address cannot be emailed."""
    a = (addr or "").strip().lower()
    if not a or "@" not in a:
        return False
    if a in ALLOWED_EMAIL_ADDRESSES:
        return True
    if a.rsplit("@", 1)[1] in ALLOWED_EMAIL_DOMAINS:
        return True
    return _wrote_to_us_first(a, thread_id)


# The node's own alerts to its owner. A guard that fires and cannot be
# reported is not protection, so these are named here rather than discovered
# one blocked message at a time: the wake cap's letter was written and blocked
# on a stand because this list held only the budget one.
# Adding a source here is a deliberate act -- everything below still applies,
# and the recipient must be the registered owner.
# voice-late-answer (#9): an answer to a call that had already ended, mailed to
# the owner by the node itself, not by the agent that answered.
_SYSTEM_ALERT_SOURCES = frozenset({"budget-exhaustion", "wake-cap", "voice-late-answer"})


def _is_system_owner_notification(channel_type, recipient, metadata, targets=None):
    """True only for a node system alert to the registered owner.

    Contact grants govern an AGENT choosing an external recipient. A system
    alert is an obligation of the node to its owner and may use a dedicated
    mailbox that is deliberately not bound to an agent. Metadata alone is
    insufficient for the exemption: the recipient must also match the enabled
    super-admin email channel in this node's registry.
    """
    if channel_type != "email" or (metadata or {}).get("source") not in _SYSTEM_ALERT_SOURCES:
        return False
    target = (email.utils.parseaddr(recipient or "")[1] or "").lower()
    if not target:
        return False
    # Owner is the generated user id 1 throughout the node schema; the
    # communication role already has the narrow user_channels grant used by
    # _owner_bcc(). Do not widen it to all of sn_config.users merely to repeat
    # a role check core-api already performed when it created the row.
    rows = db.query(
        "SELECT 1 FROM sn_config.user_channels uc "
        "WHERE uc.user_id = 1 AND uc.channel_slug = 'email' "
        "AND uc.enabled = true AND lower(uc.address) = %s LIMIT 1",
        (target,), fetch=True) or []
    if not rows:
        return False
    # The exemption is for one system letter to the owner, not for arbitrary
    # CC/BCC addresses attached to an otherwise valid-looking alert.
    for item in targets or ():
        address = (email.utils.parseaddr(item.get("addr") or "")[1] or "").lower()
        if address != target:
            return False
    return True


def _load_passwords(path):
    """Parse the generate-once creds file into {PW_key: password}."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                out[key.strip()] = val.strip()
    except FileNotFoundError:
        LOG(_TAG, "creds file not found: %s" % path)
    return out


# A send that failed for a reason that passes is tried again, with the wait
# doubling: 30 s, 1, 2, 4, 8 min -- about a quarter of an hour in all, which
# covers a provider restart or a short outage. Then it fails as before, and the
# agent is told. What is transient is decided by _transient(); everything else
# (a refusal, a bad address, a missing password) fails on the first try,
# because trying it again cannot change the answer.
SEND_MAX_ATTEMPTS = 6
SEND_RETRY_BASE_SECONDS = 30


def _transient(exc):
    """True when trying the same send again later can succeed.

    The cost of a wrong True is a possible duplicate (a timeout after the
    provider accepted the message); the cost of a wrong False is a lost
    answer. Only outcomes that say nothing about the message itself count."""
    import smtplib
    import socket
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status is not None:
        return status == 429 or status >= 500
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return all(400 <= code < 500 for code, _ in exc.recipients.values())
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
        return True
    try:
        import requests
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
    except ImportError:
        pass
    return False


def _schedule_retry(message_id, attempt, error):
    """Keep the row queued and due again after the backoff; returns the wait."""
    wait = SEND_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    db.query(
        """UPDATE message_state
              SET send_attempts = %s,
                  send_next_at = now() + make_interval(secs => %s),
                  error = %s,
                  updated_at = now()
            WHERE message_id = %s""",
        (attempt, wait, ("retrying: " + str(error))[:500], message_id),
    )
    return wait


def _set_state(message_id, status, error=None):
    """Advance one outbound message_state row. processed_at on terminals."""
    db.query(
        """UPDATE message_state
              SET status = %s,
                  error = %s,
                  processed_at = CASE WHEN %s IN ('processed', 'failed')
                                      THEN now() ELSE processed_at END
            WHERE message_id = %s""",
        (status, error, status, message_id),
    )


import html as _htmlmod

# Brand wrapper for customer-facing emails. Owner notifications arrive with their
# own HTML (metadata.html) already rendered by core-api; replies and originated
# messages do not, so they would otherwise go out as bare plain text. Here we wrap
# such bare emails in the shared brand template (the shared email template,
# read from the host-mounted files volume) so every outbound email carries the same
# header/footer with the confidentiality line. Falls back to a minimal inline footer
# if the template is unreadable, so a missing file never blocks a send.
_BRAND_TEMPLATE = "/opt/smart-node/documents/files/templates/email/message.md"
# The business name is per-node DATA, not baked into the platform template/code.
# It comes from config (the node sets SN_BUSINESS_NAME); unset -> no name line.
_BUSINESS_NAME = get("SN_BUSINESS_NAME", "")


def _sender_identity(sender):
    """Resolve a sending mailbox/handle -> (display name, role) for the email
    signature, by the agent's channel binding (agent_channels.instance_ref). Returns
    empty parts if the sender does not resolve to an agent, so the signature simply
    degrades to the business name rather than blocking the send."""
    if not sender:
        return ("", "")
    try:
        rows = db.query(
            "SELECT a.title, a.role FROM sn_config.agent_channels ac "
            "JOIN sn_config.agents a ON a.id = ac.agent_id "
            "WHERE ac.instance_ref = %s LIMIT 1", (sender,), fetch=True) or []
        if rows:
            title, role = rows[0]
            return (title or "", role or "")
    except Exception as exc:  # never block a send on a signature lookup
        LOG.warning("sender identity lookup failed for %s: %s", sender, exc)
    return ("", "")


def _brand_html(content, ref, sender=None):
    esc = _htmlmod.escape(content or "")
    paras = esc.split("\n\n")
    body = "".join("<p style=\"margin:0 0 14px;\">%s</p>" % p.replace("\n", "<br>") for p in paras if p.strip())
    ref = _htmlmod.escape(str(ref or ""))
    biz = _htmlmod.escape(_BUSINESS_NAME or "")
    # Signature = the sending agent's name + role; if the mailbox does not resolve to
    # an agent, sign with the business name so the email is never left unsigned.
    name, role = _sender_identity(sender)
    sig_name = _htmlmod.escape(name or _BUSINESS_NAME or "")
    sig_role = _htmlmod.escape(role or "")
    try:
        tpl = open(_BRAND_TEMPLATE).read()
        return (tpl.replace("{{body}}", body)
                   .replace("{{button_block}}", "")
                   .replace("{{signature_name}}", sig_name)
                   .replace("{{signature_role}}", sig_role)
                   .replace("{{message_ref}}", ref)
                   .replace("{{business_name}}", biz))
    except Exception:
        biz_line = (biz + "<br>") if biz else ""
        sig_line = (sig_name + (("<br>" + sig_role) if sig_role else "") + "<br><br>") if sig_name else ""
        return ("<div style=\"font-family:Arial,sans-serif;font-size:15px;color:#1a1a1a;line-height:1.6;\">%s%s"
                "<div style=\"border-top:1px solid #e5e5e5;margin-top:18px;padding-top:18px;font-size:11px;color:#999999;\">"
                "%sThis message is intended for the named recipient and may be confidential. "
                "If it reached you in error, please let us know and delete it. To refer to this email, quote reference %s.</div></div>"
                % (body, sig_line, biz_line, ref))


def _owner_bcc():
    """The owner's notification email from sn_config.user_channels -- used to
    silently BCC the owner on outbound customer correspondence so they receive the
    full thread, not just notifications. Single source of truth = the DB (no
    hardcode); the address forwards to the human inbox at the mail layer. The
    transport skips the BCC when it equals the recipient, so owner notifications
    (already addressed to the owner) are never doubled. Empty -> no BCC."""
    try:
        rows = db.query(
            "SELECT address FROM sn_config.user_channels "
            "WHERE user_id = 1 AND channel_slug = 'email' AND enabled = true LIMIT 1",
            fetch=True) or []
        if rows and rows[0][0]:
            return rows[0][0]
    except Exception as exc:
        LOG.warning("owner bcc lookup failed: %s", exc)
    return None


def _send_email(mid, sender, recipient, content, external_id, thread_id, metadata,
                 passwords, targets=None):
    """Transmit one outbound email AS the node mailbox `sender`, threaded
    against the original via the stored In-Reply-To/References, using the
    row's pre-minted Message-ID. The From shows the business name (config), and
    the human-facing reference is the short row id, not the RFC822 Message-ID."""
    # Egress guard, SUPERSEDED by the contact gate and kept only
    # until it is switched on: the owner decided deny-by-default with no
    # reply-to-known-correspondent exception, and this function grants
    # exactly that exception. Running both would be two policies answering the
    # same question differently, which caused the conflicting-egress-policy failure -- so this one
    # stands down the moment the gate enforces, and is deleted once every node
    # has CONTACT_GATE on. Until then it is what stops a report-only node from
    # having no egress control at all.
    _targets = targets or _recipient_set(recipient, None)
    _addrs = [t["addr"] for t in _targets]
    if not contacts.enabled() and not _is_system_owner_notification(
            "email", recipient, metadata, targets):
        _bad = [a for a in _addrs if not _email_recipient_allowed(a, thread_id)]
        if _bad:
            raise RuntimeError(
                "egress guard: refusing to email %s -- they did not write to us "
                "on this thread, and they are not covered by "
                "EMAIL_ALLOWED_DOMAINS or EMAIL_ALLOWED_ADDRESSES"
                % ", ".join(repr(a) for a in _bad))
    # Loop guards, independent of the allowlist above (which a wider config
    # would open up): never answer machinery, never exceed the per-thread
    # ceiling. Both raise, so the row is marked failed with the reason.
    _machinery = [a for a in _addrs if mailrules.is_unanswerable_recipient(a)]
    if _machinery:
        raise RuntimeError(
            "refusing to email %s -- system mailbox, replying to it starts a "
            "bounce loop" % ", ".join(repr(a) for a in _machinery))
    # Counted per sender: the refusal names who hit the ceiling and by
    # what rule, because "cap reached on thread" read as the whole room being
    # silenced when one speaker was.
    if mailrules.reply_cap_reached("email", thread_id, sender):
        raise RuntimeError(
            "refusing to email %r -- reply cap reached: %r already sent %d "
            "messages on thread %r in the last %d minutes"
            % (recipient, sender, mailrules.REPLY_CAP, thread_id,
               mailrules.REPLY_WINDOW_MINUTES))
    # EmailTransport reads its identity from the environment at construction and
    # a fresh transport is built per send, so pointing the environment at this
    # mailbox is all that is needed. Raises when the box has no password.
    mailcreds.apply_mailbox_env(sender, passwords)
    from adapters.email.transport import EmailTransport
    meta = metadata or {}
    # Robustness: if a model put a leading "Subject:" line in the body instead of
    # using the subject field, lift it into the real Subject header so it never
    # shows up as body text.
    _subject = (meta.get("subject") or "").strip()
    _body = content or ""
    if not _subject and _body:
        _first, _sep, _rest = _body.partition("\n")
        if _first.strip().lower().startswith("subject:"):
            _subject = _first.split(":", 1)[1].strip()
            _body = _rest.lstrip("\n")
    # From display name: the PERSONA (mailbox local-part) plus the business, e.g.
    # "<Agent name> (<Business>)" -- so the recipient sees who wrote, not just the brand.
    _persona = sender.split("@", 1)[0].replace(".", " ").replace("_", " ").strip().title()
    _from_name = ("%s (%s)" % (_persona, _BUSINESS_NAME)) if _BUSINESS_NAME else (_persona or None)
    # ONE REFERENCE PER PART, ADDED WHERE THE PART IS BUILT.
    # The brand template puts it in the HTML footer; nothing did the same for the
    # plain part, so it depended on whoever wrote the row remembering to bake it
    # into the content. Notifications did and replies did not, which meant a
    # duplicate whenever the HTML fell back to wrapping that content, and no
    # reference at all for a plain-text reader of a reply.
    #
    # Multipart/alternative shows ONE part, so this is not a second copy: it is
    # the same reference in the other rendering.
    _plain = "%s\n\nRef: %s" % (_body, mid)
    EmailTransport().send(
        recipient,
        _subject or "(no subject)",
        _plain,
        in_reply_to=(meta.get("in_reply_to") or None),
        references=(meta.get("references") or None),
        message_id=external_id,
        html=(meta.get("html") or _brand_html(_body, mid, sender)),
        from_name=(meta.get("from_name") or _from_name or None),
        bcc=_owner_bcc(),
        cc=[t["addr"] for t in _targets if t["role"] == "cc"] or None,
    )


def _send_telegram(mid, sender, recipient, content, external_id, thread_id, metadata,
                    passwords, targets=None):
    """Transmit one outbound telegram message to the chat via the bot.

    The chat to send to is the row's thread_id (the adapter records
    thread_id = str(chat_id) on every telegram row); metadata.chat_id is a
    fallback, recipient last."""
    meta = metadata or {}
    chat_id = meta.get("chat_id") or thread_id or recipient
    if not chat_id:
        raise RuntimeError("no chat_id/thread_id for telegram outbound %s" % external_id)
    from adapters.telegram import transport as _tg
    _tg.send(chat_id, content or "")


# One neutral icon for every agent. A per-agent picture belongs in the node DB
# (sn_config.agents.avatar), not in a table of slugs here: the slugs of one demo
# node are meaningless on every other node.
_SLACK_ICON_DEFAULT = ":speech_balloon:"


def _slack_persona(sender):
    """(display_name, slug) for a Slack author. The app posts under one bot
    identity, so the author is shown via username/prefix. `sender` may be an
    agent slug OR the slack channel instance_ref the reply path stamps --
    resolve either to the agent. system/blank/unknown -> no persona."""
    if not sender or sender == "system":
        return "", None
    rows = db.query(
        "SELECT a.slug, a.title FROM sn_config.agents a WHERE a.slug = %s "
        "UNION ALL "
        "SELECT a.slug, a.title FROM sn_config.agent_channels ac "
        "  JOIN sn_config.agents a ON a.id = ac.agent_id "
        "  JOIN sn_config.channels c ON c.id = ac.channel_id "
        " WHERE c.slug = 'slack' AND ac.instance_ref = %s "
        "LIMIT 1",
        (sender, sender), fetch=True)
    if rows:
        return rows[0][1], rows[0][0]
    return sender, None


def _send_slack(mid, sender, recipient, content, external_id, thread_id, metadata,
                 passwords, targets=None):
    """Transmit one outbound message to Slack, one fixed channel.

    With a bot token the sending agent posts under its OWN name + avatar
    (chat.postMessage). Without one the app webhook ignores the username,
    so the author is shown inline as a `*Name:*` prefix instead."""
    name, slug = _slack_persona(sender)
    body = content or ""
    from adapters.slack import transport as _slack
    if get("SLACK_BOT_TOKEN"):
        # bot token honors per-message identity: real name + avatar
        _slack.send(body, username=(name or None),
                    icon_emoji=(_SLACK_ICON_DEFAULT if slug else None))
    else:
        # app webhook ignores username -> attribute the author inline
        if name:
            body = "*%s:* %s" % (name, body)
        _slack.send(body)


def _send_webchat(mid, sender, recipient, content, external_id, thread_id,
                  metadata, passwords, targets=None):
    """Hand one message to the browser chat, which has no transport at all.

    Every other channel here dials something. Web chat is a page the person has
    open, and it learns about new turns by polling the desk history -- so for
    this channel a row that the history query can find IS a delivered row, and
    there is nothing to transmit.

    It still needs an entry. Without one the queue raised "no sender wired for
    channel 'web-chat'" and marked the message failed, which is how four
    answers John had already written were recorded and never shown: the owner
    asked, saw nothing, and asked again.

    The one thing that must be checked is the desk. The history is fetched per
    desk and keyed on thread_id, so a row without one can never be rendered no
    matter how healthy it looks. Marking that delivered would recreate the
    original defect quietly, so it fails loudly instead.
    """
    if not str(thread_id or "").startswith("desk:"):
        raise RuntimeError(
            "web-chat message %s has thread_id %r, not a desk -- the browser "
            "fetches history by desk, so nothing would ever display it"
            % (mid, thread_id))
    LOG(_TAG, "web-chat msg id=%s left for the desk to poll (%s)"
        % (mid, thread_id))


# channel_type -> sender. Adding a channel is one entry plus its transport.
_SENDERS = {
    "email": _send_email,
    "telegram": _send_telegram,
    "slack": _send_slack,
    "web-chat": _send_webchat,
}


def main():
    passwords = _load_passwords(CREDS_FILE)
    rows = db.query(SCAN_SQL, fetch=True) or []
    LOG(_TAG, "send pass start -- %d queued%s"
        % (len(rows), "" if contacts.enabled() else " (contact gate: report-only)"))
    sent = 0
    blocked = 0
    dry_ran = 0
    retrying = 0
    for row in rows:
        (mid, channel_type, sender, recipient, content,
         external_id, thread_id, metadata, recipients, attempts) = row
        targets = _recipient_set(recipient, recipients)
        # The agent that composed the row, when core-api named it (#9); else the
        # agent bound to the sending address. Its grants are checked, and it is
        # the one told when the message does not go out.
        agent = (metadata or {}).get("agent") or contacts.agent_for_sender(channel_type, sender)
        try:
            # The communication gate, at the single point where bytes leave.
            # Deny-by-default: a recipient must be a contact granted to the
            # sending agent. Report-only until CONTACT_GATE is
            # switched on, so a node can be seen refusing before it actually
            # refuses -- the verdict is journalled either way.
            # Once per address, and ONE verdict for the message: if any
            # recipient is forbidden the whole message is refused, naming them.
            # Not a partial send -- that hides a decision. The operator asked to
            # write to a group; a thread continuing without someone they meant
            # to include is a subtler failure than a refusal they can act on
            refused = []
            system_owner_alert = _is_system_owner_notification(
                channel_type, recipient, metadata, targets)
            for t in targets:
                held = contacts.decide_held_notice(channel_type, t["addr"], metadata)
                if system_owner_alert:
                    ok, why_one = True, "budget alert to the registered node owner"
                elif held is not None:
                    ok, why_one = held
                else:
                    ok, why_one = contacts.decide(channel_type, sender, t["addr"],
                                                  agent=(metadata or {}).get("agent"))
                if not ok:
                    refused.append("%s (%s)" % (t["addr"], why_one))
            if refused:
                why = "not sent: forbidden recipient(s) " + "; ".join(refused)
                db.emit_blocked(channel_type, mid, why, "component:send-outbound",
                                sender=sender, recipient=recipient,
                                enforced=contacts.enabled())
                if contacts.enabled():
                    _set_state(mid, "blocked", why[:500])
                    db.tell_sender(agent, mid, channel_type, recipient, why)
                    blocked += 1
                    LOG(_TAG, "BLOCKED msg id=%s: %s" % (mid, why))
                    continue
                LOG(_TAG, "would block msg id=%s (gate off): %s" % (mid, why))

            # Content check: refuse credential solicitations before
            # anything is transmitted. Runs after the contact gate (recipient
            # permission) and before the dry-run gate. Enforcing always -- no
            # operator switch. Internal/system channels are exempt.
            content_ok, content_reason = _content_allowed(channel_type, content)
            if not content_ok:
                db.emit_content_blocked(channel_type, mid, content_reason,
                                        "component:send-outbound",
                                        sender=sender, recipient=recipient)
                _set_state(mid, "blocked", content_reason[:500])
                db.tell_sender(agent, mid, channel_type, recipient, content_reason)
                LOG(_TAG, "CONTENT_BLOCKED msg id=%s: %s" % (mid, content_reason))
                blocked += 1
                continue

            # Dry-run gate: operator-controlled suppression, independent
            # of the agent body. Checked after the contact gate (which is about
            # permission) and before any transport is dialled (which is the
            # guarantee: nothing leaves the node in dry-run mode). The message
            # row is preserved at status 'dry_run' so the composed content is
            # readable and the row does not re-queue on the next pass.
            dry, dry_reason = _is_dry_run(channel_type, sender)
            if dry:
                _set_state(mid, "dry_run", dry_reason[:500])
                db.emit_dry_run(channel_type, mid, dry_reason,
                                "component:send-outbound",
                                sender=sender, recipient=recipient)
                LOG(_TAG, "DRY_RUN msg id=%s channel=%s to=%s reason=%s"
                    % (mid, channel_type, recipient, dry_reason))
                dry_ran += 1
                continue

            send = _SENDERS.get(channel_type)
            if send is None:
                raise RuntimeError("no sender wired for channel %r" % channel_type)
            send(mid, sender, recipient, content, external_id, thread_id, metadata,
                 passwords, targets)
            db.mark_transmitted(channel_type, mid, recipient)
            sent += 1
            _cc_n = sum(1 for t in targets if t["role"] != "to")
            LOG(_TAG, "sent msg id=%s channel=%s to=%s%s"
                % (mid, channel_type, recipient,
                   (" cc=%d" % _cc_n) if _cc_n else ""))
        except Exception as exc:  # one bad row must not stall the queue
            attempt = (attempts or 0) + 1
            if _transient(exc) and attempt < SEND_MAX_ATTEMPTS:
                wait = _schedule_retry(mid, attempt, exc)
                db.emit_send_retry(channel_type, mid, attempt, SEND_MAX_ATTEMPTS, wait, exc,
                                   "component:send-outbound")
                retrying += 1
                LOG(_TAG, "RETRY msg id=%s in %ds (attempt %d of %d): %r"
                    % (mid, wait, attempt, SEND_MAX_ATTEMPTS, exc))
                continue
            LOG(_TAG, "FAIL msg id=%s: %r" % (mid, exc))
            tries = " after %d attempts" % attempt if attempt > 1 else ""
            _set_state(mid, "failed", (str(exc) + tries)[:500])
            db.emit_failed(channel_type, mid, "transmit", exc,
                           "component:send-outbound")
            db.tell_sender(agent, mid, channel_type, recipient,
                           "sending failed%s (%s)" % (tries, str(exc)[:200]))
    LOG(_TAG, "send pass done -- %d sent, %d blocked, %d dry_run, %d retrying"
        % (sent, blocked, dry_ran, retrying))


if __name__ == "__main__":
    main()
