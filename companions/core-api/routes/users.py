"""User notification channels -- per-user delivery addresses.

A human user (sn_config.users) may register one or more notification channels
(email mailbox, telegram chat id, ...). The /api/notify endpoint resolves a
planner assignee to that user's enabled channels and writes outbound message
rows. This module is the CRUD surface for those channel rows.

Tenant-scoped via get_principal; a cross-tenant principal (super-admin) may
manage any user's channels, everyone else only within their own tenant.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import Principal, get_principal
from db import cursor
from permissions import requires

log = logging.getLogger("core-api.users")

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/resolve-identity", response_model=dict,
            dependencies=[Depends(requires("users.read"))])
def resolve_identity(identity: str):
    """Resolve a forwarded auth identity (Authum Remote-User, matched against
    sn_config.users.email) to the person behind it. Node-local helper so brin can
    label an authenticated caller. No auth dependency (service-to-service, read
    only). Unknown identity -> found=false."""
    ident = (identity or "").strip()
    if not ident:
        return {"found": False}
    with cursor() as cur:
        cur.execute(
            "SELECT id, name, role, handle, contact_email FROM sn_config.users "
            "WHERE email = %s AND active = true AND archived_at IS NULL LIMIT 1",
            (ident,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT u.id, u.name, u.role, u.handle, u.contact_email FROM sn_config.users u "
                "JOIN sn_config.user_channels uc ON uc.user_id = u.id "
                "WHERE uc.address = %s AND uc.enabled AND u.active "
                "AND u.archived_at IS NULL LIMIT 1",
                (ident,),
            )
            row = cur.fetchone()
    if row is None:
        return {"found": False, "identity": ident}
    return {
        "found": True,
        "identity": ident,
        "user_id": row["id"],
        "name": row["name"],
        "role": row["role"] or "",
        "handle": row["handle"],
        # The person's reachable personal email (contact_email), so a caller-facing
        # agent knows where to email them without asking. May be empty.
        "email": row["contact_email"] or "",
        "is_owner": (row["role"] == "super-admin"),
    }


@router.get("/cross-channel-history", response_model=dict,
            dependencies=[Depends(requires("users.read"))])
def cross_channel_history(identity: str, agent: str = None, limit: int = 10,
                          thread: str = None, thread_channel: str = None):
    """Recent messages between a specific agent and a person, oldest->newest.

    `thread` + `thread_channel` scope the result to the conversation this turn
    belongs to. ON THAT CHANNEL only the named thread is returned; every other
    channel is untouched, because carrying the person across channels is the
    whole point of this block. Without them the answer is the old global one.

    Why it matters: a person with two Studio desks open on the same
    agent had all ten slots filled from the desk he was NOT writing in, and the
    lines carry a channel and an age but no thread, so the model read another
    conversation as this one and answered a question nobody asked here.

    Both `identity` and `agent` are required. `agent` is the agent slug (e.g.
    'foreman', 'developer'). Requests without `agent` return 400. Scoping to
    the agent+person pair is the security boundary: each agent sees only its own
    history with the person; cross-agent disclosure is refused by the query, not
    post-filtered.

    Node-local, read-only, no auth dependency.
    """
    if not (agent or "").strip():
        raise HTTPException(400, "agent parameter is required")
    ident = (identity or "").strip()
    agent = agent.strip()
    if not ident:
        return {"found": False, "messages": []}
    limit = max(1, min(int(limit or 10), 40))
    with cursor() as cur:
        if ident.lower() == "owner":
            cur.execute("SELECT id FROM sn_config.users WHERE role = 'super-admin' "
                        "AND active AND archived_at IS NULL ORDER BY id LIMIT 1")
        else:
            cur.execute("SELECT id FROM sn_config.users "
                        "WHERE (email = %s OR handle = %s) AND active AND archived_at IS NULL "
                        "LIMIT 1", (ident, ident.lower()))
        u = cur.fetchone()
        if not u:
            cur.execute(
                "SELECT u.id FROM sn_config.users u "
                "JOIN sn_config.user_channels uc ON uc.user_id = u.id "
                "WHERE uc.address = %s AND uc.enabled AND u.active "
                "AND u.archived_at IS NULL LIMIT 1",
                (ident,),
            )
            u = cur.fetchone()
        if not u:
            return {"found": False, "messages": []}
        cur.execute("SELECT address FROM sn_config.user_channels "
                    "WHERE user_id = %s AND enabled", (u["id"],))
        addrs = [r["address"] for r in (cur.fetchall() or [])]
        # record-webchat uses a synthetic address when the user_channels row does
        # not exist yet ('web:owner' for identity='owner', 'web:<ident>' for
        # others). Include it here so those messages are always reachable.
        synthetic = "web:owner" if ident.lower() == "owner" else f"web:{ident}"
        if synthetic not in addrs:
            addrs.append(synthetic)
        if not addrs:
            return {"found": True, "messages": []}
        # Resolve the agent's channel addresses. The messages table stores the
        # address (mailbox, chat id, etc.), not the slug. We look up all instance_refs
        # bound to this agent, plus the slug itself (used as sender/recipient on
        # internal and system channel messages).
        cur.execute(
            "SELECT ac.instance_ref FROM sn_config.agent_channels ac "
            "JOIN sn_config.agents a ON a.id = ac.agent_id "
            "WHERE a.slug = %s",
            (agent,),
        )
        agent_addrs = [r["instance_ref"] for r in (cur.fetchall() or [])]
        # Always include the slug itself: internal/system channel rows store the
        # slug as sender or recipient directly (not via agent_channels).
        if agent not in agent_addrs:
            agent_addrs.append(agent)
        # Scope query to the agent+person pair only. No cross-agent leakage.
        # And no letter the contact gate refused, unless the agent wrote it: this
        # block goes straight into the agent's prompt, so a refused message here
        # reaches the model without it asking (the same rule as messages.py
        # _refused_hidden).
        # Same channel, different thread = another conversation, and it must not
        # arrive here. A row on ANOTHER channel is kept: that is continuity, not
        # bleed. Applied only when the caller says which thread it is in, so an
        # older caller keeps the previous behaviour.
        thread_sql, thread_params = "", []
        if thread and thread_channel:
            thread_sql = ("  AND NOT (m.channel_type = %s "
                          "           AND m.thread_id IS DISTINCT FROM %s) ")
            thread_params = [thread_channel, thread]
        cur.execute(
            "SELECT m.channel_type, m.direction, m.sender, m.recipient, m.content, "
            "m.metadata->>'subject' AS subject, "
            "EXTRACT(EPOCH FROM (now() - m.received_at))::bigint AS age_sec "
            "FROM sn_core.messages m "
            "JOIN sn_core.message_state ms ON ms.message_id = m.id "
            "WHERE ((m.sender = ANY(%s) AND m.recipient = ANY(%s)) "
            "    OR (m.sender = ANY(%s) AND m.recipient = ANY(%s))) "
            "  AND (ms.status <> 'blocked' OR m.sender = ANY(%s)) "
            + thread_sql +
            "ORDER BY m.received_at DESC LIMIT %s",
            (addrs, agent_addrs, agent_addrs, addrs, agent_addrs,
             *thread_params, limit),
        )
        rows = list(reversed(cur.fetchall() or []))

    def _ago(sec):
        s = int(sec or 0)
        if s < 45:
            return "just now"
        if s < 5400:
            return "%d min ago" % max(1, round(s / 60))
        if s < 172800:
            return "%d h ago" % round(s / 3600)
        return "%d d ago" % round(s / 86400)

    msgs = [{
        "channel": r["channel_type"],
        "direction": r["direction"],
        "from_person": (r["sender"] in addrs),
        "ago": _ago(r["age_sec"]),
        "subject": r["subject"],
        "from": r["sender"],
        "to": r["recipient"],
        "text": (r["content"] or "")[:300],
    } for r in rows]
    return {"found": True, "messages": msgs}


def _load_user(cur, user_id: int, principal: Principal):
    """Fetch a user row, enforcing tenant scope. Returns the row or raises."""
    cur.execute(
        "SELECT id, tenant_id, email FROM sn_config.users WHERE id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "user not found")
    if not principal.cross_tenant and row["tenant_id"] != principal.tenant_id:
        raise HTTPException(403, "user belongs to another tenant")
    return row


@router.get("/{user_id}/notify-channels", response_model=dict)
def list_notify_channels(
    user_id: int,
    principal: Principal | None = Depends(requires("users.read")),
):
    """List a user's ENABLED notification channels."""
    # users.read is also listed for a node service, which has no account and
    # so no tenant to scope this read by. Nothing a service does needs it.
    if principal is None:
        raise HTTPException(403, "a user's channels are read by an account, "
                                 "not by a node service")
    with cursor() as cur:
        _load_user(cur, user_id, principal)
        cur.execute(
            "SELECT channel_slug AS channel, address, enabled "
            "FROM sn_config.user_channels "
            "WHERE user_id = %s AND enabled = true "
            "ORDER BY channel_slug",
            (user_id,),
        )
        rows = cur.fetchall()
    return {"channels": rows}


class NotifyChannelUpsert(BaseModel):
    address: str
    enabled: bool = True


@router.put("/{user_id}/notify-channels/{channel}", response_model=dict)
def upsert_notify_channel(
    user_id: int,
    channel: str,
    body: NotifyChannelUpsert,
    principal: Principal = Depends(requires("users.channels.write")),
):
    """Create or update a user's notification channel (upsert on user+channel)."""
    if not body.address.strip():
        raise HTTPException(400, "address is required")

    with cursor(commit=True) as cur:
        user = _load_user(cur, user_id, principal)
        cur.execute(
            "INSERT INTO sn_config.user_channels "
            "  (tenant_id, user_id, channel_slug, address, enabled) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, channel_slug) DO UPDATE "
            "  SET address = EXCLUDED.address, enabled = EXCLUDED.enabled "
            "RETURNING channel_slug AS channel, address, enabled",
            (user["tenant_id"], user_id, channel, body.address.strip(), body.enabled),
        )
        row = cur.fetchone()
    return {"ok": True, **row}
