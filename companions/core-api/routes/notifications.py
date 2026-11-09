"""Notification chain -- POST /api/notify, POST /api/owner-alert.

Resolves a planner assignee (= sn_config.users.email) to that user's enabled
notification channels and writes ONE outbound sn_core.messages row per channel.
The communication outbound-pass drains those rows and transmits them; this
endpoint never sends.

An AGENT assignee is not skipped: it gets its wake flag raised on
sn_core.agent_runtime, which is how a planner task reaches the Runner at all --
the planner is a separate database and nothing on this node can query it. That
step is independent of channels, so it also holds for an agent that has them.
Agents typically send no notification message (no user_channels rows), so the
reply still reads {"sent": [], "woke": "<slug>"}.

Mirrors the outbound-row pattern in routes/messages.py: SET LOCAL search_path
so the seed-state trigger fires, and mint external_id via email.utils.make_msgid.

POST /api/owner-alert is the same letter the budget and turn guards send
(lib/owner_alert.py): from the node's own sender to the owner's email channel
as Settings has it. A node tool that has to tell the owner something (the daily
event digest) says what, never to whom or from whom, so nothing about a
particular owner is written into the product.
"""

import email.utils
import html as _htmlmod
import json
import logging
import os
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg2.extras import Json

from auth import Principal
from permissions import requires
from db import cursor
from events_emit import emit
from lib import owner_alert

log = logging.getLogger("core-api.notifications")

router = APIRouter(prefix="/api", tags=["notifications"])

# Node identities for the assistant that originates task notifications. Derived
# from the node's OWN domain (Environment=SMART_NODE_DOMAIN, set by core-api.sh)
# so an OSS install does not send FROM / link to the demo fleet.
_NODE_DOMAIN = os.environ.get("SMART_NODE_DOMAIN") or "smart-node.local"

# NO INVENTED FALLBACK FOR EITHER OF THESE. Both used to be derived from the
# node's bare domain, which produces something syntactically fine and
# functionally impossible on any node whose mail and console do not live on that
# exact name:
#
#   sender  assistant@<domain>  is not a real mailbox and is not a contact the
#           egress gate knows, so every notification was written and then set to
#           'blocked' at the gate. The API had already answered {"sent":["email"]}.
#   link    https://<domain>/tasks/<id>  does not resolve when the console is on
#           its own host, so every approval mail carried a dead link.
#
# Both were reported from a live node on 2026-08-21 and both had been silently
# broken there since install. A default that cannot work is worse than no
# default: it removes the moment where somebody would have had to decide.
#
# None is the honest value. What depends on them refuses and says which variable
# to set, rather than composing an address or a URL nobody can reach.
_EMAIL_SENDER = os.environ.get("SN_NOTIFY_SENDER") or None
_TELEGRAM_SENDER = "smart_node_bot"

# Public console base for task deep-links in notifications.
_TASK_URL_BASE = os.environ.get("SN_CONSOLE_URL") or None

# Said once, at import, so an operator learns it from the log on a quiet node
# instead of from a user who never got told about an approval.
if not _EMAIL_SENDER:
    log.warning("SN_NOTIFY_SENDER is unset: email notifications will be REFUSED "
                "rather than sent from an address that cannot deliver. Set it to "
                "a real mailbox on this node.")
if not _TASK_URL_BASE:
    log.warning("SN_CONSOLE_URL is unset: notifications will be REFUSED rather "
                "than carrying a link that does not resolve. Set it to the "
                "console's own base URL, e.g. https://studio.<domain>/tasks.")
_DOCI_URL = os.environ.get("DOCI_URL", "")
# Owner notifications render into the SHARED brand shell (one template for every
# outbound email); the old dedicated notification.md is retired. The business name
# matches the value the outbound pass uses (config SN_BUSINESS_NAME).
_SHELL_TEMPLATE_PATH = "templates/email/message.md"
_BUSINESS_NAME = os.environ.get("SN_BUSINESS_NAME") or "Smart Node"

# CTA row injected into the shell's {{button_block}} slot for owner notifications.
_BUTTON_ROW = (
    '<tr><td align="center" style="padding-bottom: 24px;">'
    '<table border="0" cellpadding="0" cellspacing="0" style="border-collapse: collapse;">'
    '<tr><td align="center" bgcolor="#ffffff" style="border: 1px solid #000000; border-radius: 4px;">'
    '<a href="{task_url}" target="_blank" style="display: inline-block; padding: 11px 23px; '
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; "
    'font-size: 15px; font-weight: bold; color: #000000; text-decoration: none; border-radius: 4px; line-height: 1;">'
    'Open and review</a></td></tr></table></td></tr>'
)


def _front_desk() -> tuple[str, str]:
    """Who signs an owner notification: the agent that answers the shared line,
    i.e. the oldest enabled one, with its specialist role. No name is written
    into this file -- a node names its own agents. ("", "") when the node has
    none, and the signature slots are then left empty rather than signed by a
    stranger."""
    try:
        with cursor() as cur:
            cur.execute("SELECT a.slug, a.title, s.name AS role FROM sn_config.agents a "
                        "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
                        "WHERE a.enabled AND a.archived_at IS NULL ORDER BY a.id LIMIT 1")
            r = cur.fetchone()
        if not r:
            return ("", "")
        return (r["title"] or r["slug"] or "", r["role"] or "")
    except Exception as e:
        log.warning("front-desk lookup failed: %s", e)
        return ("", "")


def _render_notification_html(title: str, task_id: int, template: str = "notification", recipient_name=None,
                              lead=None):
    """Render an owner task-notification into the SHARED brand shell
    (the shared email template): greeting + body, a Review CTA in the
    {{button_block}} slot, and the front-desk signature (owner notifications come
    from the coordinating assistant, not the specialist). Returns None on any
    failure so the email still goes out as plain text (graceful enrichment, not a
    fallback). The caller fills {{message_ref}}."""
    try:
        url = _DOCI_URL + "/api/documents.php?path=" + _SHELL_TEMPLATE_PATH + "&content=1"
        req = urllib.request.Request(url, headers={"Remote-User": "admin"})
        tpl = json.loads(urllib.request.urlopen(req, timeout=6).read())["document"]["content"]
    except Exception as e:
        log.warning("notification template fetch failed: %s", e)
        return None
    name = _htmlmod.escape(recipient_name or "there")
    body = (f"Hi {name},<br><br>{_htmlmod.escape(lead or 'A task is waiting for your review:')}<br><br>"
            f"{_htmlmod.escape(title)}")
    button = _BUTTON_ROW.format(task_url=f"{_TASK_URL_BASE}/{task_id}")
    _sig_name, _sig_role = _front_desk()
    return (tpl
            .replace("{{body}}", body)
            .replace("{{button_block}}", button)
            .replace("{{signature_name}}", _htmlmod.escape(_sig_name))
            .replace("{{signature_role}}", _htmlmod.escape(_sig_role))
            .replace("{{business_name}}", _htmlmod.escape(_BUSINESS_NAME)))


def _notification_text(task_id: int, title: str, summary: str, event: str = "") -> str:
    # Compose the link line only when we have a real URL. Python renders None as
    # the string "None", so an unguarded f-string ships "None/4190" -- exactly
    # what the owner received eight times on 2026-09-05. The guard here
    # covers every channel that uses this function (telegram, slack, email plain
    # part), so a guard added once applies everywhere.
    if _TASK_URL_BASE:
        action_line = f"Open it here: {_TASK_URL_BASE}/{task_id}"
    else:
        action_line = f"Task ID: {task_id}"
    if event == "mentioned":
        # A mention is not a request to approve anything, and saying so would
        # send the person looking for a decision nobody asked of them.
        return f"""You were mentioned in a comment on task #{task_id}: {title}.

{summary}

{action_line}"""
    if event == "recurring_due":
        # A recurring task's next cycle: work to do again, not a decision.
        return f"""Recurring task #{task_id} is due again: {title}.

{summary}

{action_line}"""
    return f"""{title}.

It's waiting for you to review and approve.

{action_line}"""


class NotifyRequest(BaseModel):
    assignee: str
    task_id: int
    event: str
    title: str
    summary: str
    template: str = "notification"
    recipient_name: str | None = None
    subject: str | None = None
    # Raise the agent's wake flag and stop: no person is messaged. For a clock
    # that repeats (the planner's stale-task pass), where the person path would
    # send the same notice on every scan.
    wake_only: bool = False


@router.post("/notify", response_model=dict)
def notify(
    body: NotifyRequest,
    principal: Principal | None = Depends(requires("notifications.notify")),
):
    """Notify a user about a task assignment over each of their enabled channels.

    No-op (sent: []) when the assignee resolves to no user, or to a user with no
    enabled notify-channels (the agent-assignee case).
    """
    assignee = (body.assignee or "").strip()
    sent: list[str] = []
    # A channel that could not even be attempted is not the same fact as a
    # channel the user does not have, and {"sent": []} said both. Refusals
    # are carried back with a reason so the caller can tell "nobody to tell"
    # from "this node is not configured to tell them".
    refused: list[dict] = []

    with cursor(commit=True) as cur:
        # The seed-state trigger on sn_core.messages INSERTs into message_state
        # unqualified -> this connection needs sn_core on its search_path.
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")

        # An assignee that is an AGENT gets its wake flag raised, and that is
        # decided here, on its own, before anything about people: it does not
        # depend on whether the agent also has notification channels, and it does
        # not consume the person path (an agent with channels gets both).
        #
        # Previously there was no such step. A task assigned to an agent walked
        # the person path, found no enabled channels, returned {"sent": []} and
        # woke nobody -- so the ONLY thing that ever noticed a task was the
        # per-agent cron, which is why the cron could not be retired. The gap was
        # invisible because nothing failed: the hook fired, the call succeeded,
        # and the answer said "sent nothing", which was true and useless.
        cur.execute("SELECT slug, tenant_id FROM sn_config.agents "
                    "WHERE slug=%s AND enabled AND archived_at IS NULL", (assignee,))
        agent_row = cur.fetchone()
        if agent_row is not None:
            # The agent is woken by a message that names the task, not by a bare
            # flag: see _task_wake_message below for why.
            wake_mid, wake_new = _task_wake_message(cur, assignee, agent_row["tenant_id"], body)
            emit(cur, actor="component:planner", event_type="agent.wake_requested",
                 entity_type="agent", entity_id=assignee,
                 payload={"source": "planner", "reason": body.event, "task_id": body.task_id,
                          "message_id": wake_mid, "already_waiting": not wake_new})
            log.info("notify: task %s %s -> message %s for agent %s%s",
                     body.task_id, body.event, wake_mid, assignee,
                     "" if wake_new else " (already waiting, not repeated)")
            woke = assignee
        else:
            woke = None

        if body.wake_only:
            return {"sent": [], "refused": refused, "user_id": None, "woke": woke}

        # Resolve assignee string to a user row by email (planner assignee == users.email).
        cur.execute(
            "SELECT id, tenant_id, name FROM sn_config.users "
            "WHERE email = %s AND active = true",
            (assignee,),
        )
        user = cur.fetchone()
        if user is None:
            return {"sent": [], "refused": refused, "user_id": None, "woke": woke}

        # Tenant scope: a non-cross-tenant caller may only notify within its
        # tenant. No account at all means the call was admitted as the node's
        # own component, which belongs to no tenant and serves every one.
        if (principal is not None and not principal.cross_tenant
                and user["tenant_id"] != principal.tenant_id):
            return {"sent": [], "refused": refused, "user_id": None, "woke": woke}

        cur.execute(
            "SELECT channel_slug, address FROM sn_config.user_channels "
            "WHERE user_id = %s AND enabled = true "
            "ORDER BY channel_slug",
            (user["id"],),
        )
        channels = cur.fetchall()
        if not channels:
            return {"sent": [], "refused": refused, "user_id": user["id"], "woke": woke}

        _nm = (user["name"] or "").split()
        rname = body.recipient_name or (_nm[0] if _nm else "there")
        text = _notification_text(body.task_id, body.title, body.summary, body.event)
        # If the task link could not be composed (SN_CONSOLE_URL unset), make
        # that visible to the caller in the refused list rather than only in the
        # log. Channels that can still send a useful notification (telegram,
        # slack) do so without the link. The email path has its own guard that
        # refuses the whole channel when SN_CONSOLE_URL is absent; this entry
        # covers the remaining channels.
        if not _TASK_URL_BASE:
            refused.append({
                "channel": "link",
                "reason": ("SN_CONSOLE_URL is unset: notifications sent without a task link; "
                           "set it to the console base URL, e.g. https://studio.<domain>/tasks"),
            })

        for ch in channels:
            slug = ch["channel_slug"]
            address = ch["address"]

            if slug == "email":
                # Refuse rather than queue. An undeliverable sender is set to
                # 'blocked' at the egress gate minutes later, where nobody is
                # looking; a dead link is discovered by the person who trusted
                # it. Both are better said here, to the caller, now.
                _missing = [n for n, v in (("SN_NOTIFY_SENDER", _EMAIL_SENDER),
                                           ("SN_CONSOLE_URL", _TASK_URL_BASE)) if not v]
                if _missing:
                    reason = ("not configured: " + " and ".join(_missing)
                              + " must be set for this node to notify by email")
                    log.warning("notify: refusing email to user %s -- %s",
                                user["id"], reason)
                    refused.append({"channel": slug, "reason": reason})
                    continue
                ext = email.utils.make_msgid(domain=_NODE_DOMAIN)
                # reserve the row id up front so the email can print its own ref
                cur.execute(
                    "SELECT nextval(pg_get_serial_sequence('sn_core.messages', 'id')) AS nid")
                mid = cur.fetchone()["nid"]
                meta = {"subject": body.subject or (
                            ("You were mentioned: " if body.event == "mentioned"
                             else "Recurring task due: " if body.event == "recurring_due"
                             else "New task assigned: ") + body.title),
                        "source": "notify"}
                _html = _render_notification_html(
                    body.title, body.task_id, body.template, rname,
                    lead=("You were mentioned in a comment on this task:"
                          if body.event == "mentioned" else None))
                if _html:
                    meta["html"] = _html.replace("{{message_ref}}", str(mid))
                cur.execute(
                    "INSERT INTO sn_core.messages "
                    "  (id, channel_type, channel_id, external_id, direction, sender, "
                    "   recipient, content, content_type, metadata, tenant_id) "
                    "OVERRIDING SYSTEM VALUE "
                    "VALUES (%s, 'email', %s, %s, 'outbound', %s, %s, %s, 'text/plain', %s, %s)",
                    (mid, _EMAIL_SENDER, ext, _EMAIL_SENDER, address,
                     # The reference is NOT stored in the body. It is presentation,
                     # and it is added per PART at send time -- the brand template
                     # puts it in the HTML footer, the outbound pass appends it to
                     # the plain part. Storing it here put it in the body that the
                     # fallback renderer then wraps, so a node whose Doci did not
                     # answer got two references in one email.
                     text, Json(meta), user["tenant_id"]),
                )
                sent.append(slug)

            elif slug == "telegram":
                ext = email.utils.make_msgid(domain=_NODE_DOMAIN)
                meta = {"source": "notify"}
                cur.execute(
                    "INSERT INTO sn_core.messages "
                    "  (channel_type, channel_id, external_id, thread_id, direction, "
                    "   sender, recipient, content, content_type, metadata, tenant_id) "
                    "VALUES ('telegram', %s, %s, %s, 'outbound', %s, %s, %s, 'text/plain', %s, %s)",
                    (_TELEGRAM_SENDER, ext, address, _TELEGRAM_SENDER, address, text,
                     Json(meta), user["tenant_id"]),
                )
                sent.append(slug)

            elif slug == "slack":
                # Slack incoming webhook targets one fixed channel baked
                # into the URL; no per-recipient routing. address is info.
                ext = email.utils.make_msgid(domain=_NODE_DOMAIN)
                meta = {"source": "notify"}
                cur.execute(
                    "INSERT INTO sn_core.messages "
                    "  (channel_type, channel_id, external_id, direction, "
                    "   sender, recipient, content, content_type, metadata, tenant_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    ("slack", address, ext, "outbound", "system",
                     address, text, "text/plain", Json(meta), user["tenant_id"]),
                )
                sent.append(slug)

            else:
                log.warning("notify: unsupported channel '%s' for user %s, skipped",
                            slug, user["id"])

    return {"sent": sent, "refused": refused, "user_id": user["id"], "woke": woke}


_TASK_WAKE_REASONS = {
    "assigned": "it was assigned to you",
    "reviewer_assigned": "you were made its reviewer",
    "status_changed": "its status changed",
    "stale_pending_review": "it has been waiting for review for a long time",
    "mentioned": "a comment on it is addressed to you",
    "recurring_due": "it is a recurring task and its next cycle came due",
}


def _task_wake_message(cur, agent, tenant_id, body):
    """Tell an agent, in its inbox, which task it is being woken for.

    Returns (message id, True when a new message was written).

    WHY. A task used to reach an agent as one timestamp, agent_runtime
    .wake_requested_at. The planner sent the task id, and it was dropped here:
    the agent woke to a fixed sentence, had to guess which task was meant, and
    several requests raised while it was busy collapsed into one mark. On
    2026-10-10 an agent woken for a task looked for its open tasks, found
    none because the task was already in progress, and went back to sleep with
    the work untouched.

    The owner's decision: the reason for a wake is a record with
    content, and the record is an internal message from the node. Nothing else
    changes. The Runner already wakes a free agent for a new message and leaves
    a busy one alone, and its wake sentence already says: a waiting message that
    names a task is the task to work on. So the inbox becomes the agent's queue
    of work and the answer to "why was it woken".

    One open message per (agent, task): the planner sends two notifies for one
    write that changes both status and assignee, and a second message for the
    same task would only be a second thing to close.
    """
    # Two notifies for one write arrive together, each in its own transaction,
    # and each used to look before the other had committed: both found nothing
    # and both wrote, two messages for one task. The lock makes the
    # second wait for the first to commit, so it sees the message and stops.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("task-wake:%s:%s" % (agent, body.task_id),))
    cur.execute(
        "SELECT m.id FROM sn_core.messages m "
        "JOIN sn_core.message_state ms ON ms.message_id = m.id "
        "WHERE m.channel_type = 'internal' AND m.sender = 'system' AND m.recipient = %s "
        "  AND m.metadata->>'source' = 'planner' AND m.metadata->>'task_id' = %s "
        "  AND ms.status IN ('new', 'routed') "
        "ORDER BY m.id DESC LIMIT 1",
        (agent, str(body.task_id)))
    waiting = cur.fetchone()
    if waiting is not None:
        return waiting["id"], False
    why = _TASK_WAKE_REASONS.get(body.event, body.event)
    if body.event == "mentioned" and (body.summary or "").strip():
        # Which comment, and whose: the planner sends it as the summary.
        why += " (%s)" % body.summary.strip()
    title = (body.title or "").strip()
    text = (
        "The node woke you for task #%d%s, because %s. Work on THAT task: open task #%d, "
        "read its description and its comments (if a workflow runs the task, your "
        "instructions are the latest work order in the comments), and do what it asks. "
        "Close this message when you have taken the task; it needs no reply."
        % (body.task_id, (" (%s)" % title) if title else "", why, body.task_id))
    ext = email.utils.make_msgid(domain=os.environ.get("SMART_NODE_DOMAIN") or "smart-node.local")
    cur.execute(
        "INSERT INTO sn_core.messages "
        "  (channel_type, channel_id, external_id, thread_id, direction, sender, "
        "   recipient, content, content_type, metadata, tenant_id) "
        "VALUES ('internal', 'system', %s, %s, 'inbound', 'system', %s, %s, "
        "        'text/plain', %s, %s) RETURNING id",
        (ext, ext, agent, text,
         Json({"source": "planner", "kind": "task", "task_id": body.task_id, "reason": body.event}),
         tenant_id))
    return cur.fetchone()["id"], True


class OwnerAlertRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=200_000)
    source: str = Field(default="node", min_length=1, max_length=64)


@router.post("/owner-alert", response_model=dict)
def owner_alert_send(
    body: OwnerAlertRequest,
    principal: Principal | None = Depends(requires("notifications.notify")),
):
    """Queue one email to the node owner. 409 when the node cannot reach him
    (no sender installed, or no enabled email channel in Settings): the caller
    keeps its own fallback and nothing is reported as sent."""
    with cursor(commit=True) as cur:
        try:
            queued = owner_alert.queue_owner_email(
                cur, subject=body.subject, text=body.text, source=body.source)
        except owner_alert.OwnerUnreachable as exc:
            raise HTTPException(409, str(exc))
    return {"status": "queued", "message_id": queued["message_id"]}
