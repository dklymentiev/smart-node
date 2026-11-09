"""Sessions read-side -- conversation history for the studio agent page.

Two read-only endpoints backing the agent page's "Sessions" tab. Everything is
read straight from the database (sn_core.ai_sessions + ai_session_messages),
tenant-scoped like the rest of the read API:

  GET /api/agents/{slug}/sessions   list sessions attributed to that agent
  GET /api/sessions/{id}            one session + its messages (ordered by seq)

The gateway persists every /api/v1/chat call: it derives a uuid5 session id from
the caller's session_id string, resolves the agent slug to sn_config.agents.id,
and stores it on ai_sessions.agent_id. These endpoints surface that data; they
never fabricate -- an agent with no chats returns an empty list.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from typing import Annotated

from auth import Principal
from db import cursor
from permissions import requires

log = logging.getLogger("core-api.sessions")

# Truncation for the snippet shown in the session list (first user message).
SNIPPET_TRUNC = 160
# Truncation per message body in a transcript (keeps the payload bounded).
MSG_CONTENT_TRUNC = 8000

agent_sessions_router = APIRouter(prefix="/api/agents", tags=["sessions"])
sessions_router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@agent_sessions_router.get("/{slug}/sessions")
def list_agent_sessions(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    principal: Principal = Depends(requires("agents.sessions.read")),
):
    """List ai_sessions attributed to this agent, most recent first.

    Each item: id, model, provider, status, created_at, last_activity_at,
    message_count, and a short snippet (the first user message of the session).
    """
    with cursor() as cur:
        cur.execute(
            "SELECT id FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (slug,),
        )
        agent = cur.fetchone()
        if agent is None:
            raise HTTPException(404, f"agent not found: {slug}")
        agent_id = agent["id"]

        params = [agent_id]
        tenant_clause = ""
        if not principal.cross_tenant:
            tenant_clause = " AND s.tenant_id = %s"
            params.append(principal.tenant_id)

        cur.execute(
            "SELECT s.id, s.model, s.provider, s.status, s.metadata, "
            "       s.created_at, s.last_activity_at, "
            "       (SELECT count(*) FROM sn_core.ai_session_messages m "
            "        WHERE m.session_id = s.id) AS message_count, "
            "       (SELECT m.content FROM sn_core.ai_session_messages m "
            "        WHERE m.session_id = s.id AND m.role = 'user' "
            "        ORDER BY m.seq ASC LIMIT 1) AS first_user "
            "FROM sn_core.ai_sessions s "
            "WHERE s.agent_id = %s" + tenant_clause + " "
            "ORDER BY s.last_activity_at DESC",
            tuple(params),
        )
        rows = cur.fetchall()

    sessions = []
    for r in rows:
        snippet = (r["first_user"] or "").strip().replace("\n", " ")
        if len(snippet) > SNIPPET_TRUNC:
            snippet = snippet[:SNIPPET_TRUNC] + "…"
        meta = r["metadata"] or {}
        sessions.append({
            "id": str(r["id"]),
            "model": r["model"],
            "provider": r["provider"],
            "status": r["status"],
            "created_at": r["created_at"],
            "last_activity_at": r["last_activity_at"],
            "message_count": r["message_count"],
            "snippet": snippet,
            "client_session_id": meta.get("client_session_id"),
        })
    return {"agent": slug, "sessions": sessions}


@sessions_router.get("/{session_id}")
def get_session(
    session_id: str,
    principal: Principal = Depends(requires("sessions.read")),
):
    """One session + its messages (ordered by seq). Read-only transcript."""
    with cursor() as cur:
        params = [session_id]
        tenant_clause = ""
        if not principal.cross_tenant:
            tenant_clause = " AND s.tenant_id = %s"
            params.append(principal.tenant_id)

        cur.execute(
            "SELECT s.id, s.tenant_id, s.type, s.model, s.provider, s.status, "
            "       s.agent_id, s.metadata, s.usage, s.created_at, s.last_activity_at, "
            "       ag.slug AS agent_slug "
            "FROM sn_core.ai_sessions s "
            "LEFT JOIN sn_config.agents ag ON ag.id = s.agent_id "
            "WHERE s.id = %s" + tenant_clause,
            tuple(params),
        )
        s = cur.fetchone()
        if s is None:
            raise HTTPException(404, f"session not found: {session_id}")

        cur.execute(
            "SELECT seq, role, content_type, content, usage, created_at "
            "FROM sn_core.ai_session_messages "
            "WHERE session_id = %s ORDER BY seq ASC",
            (session_id,),
        )
        mrows = cur.fetchall()

    messages = []
    for m in mrows:
        content = m["content"] or ""
        if len(content) > MSG_CONTENT_TRUNC:
            content = content[:MSG_CONTENT_TRUNC] + "…"
        messages.append({
            "seq": m["seq"],
            "role": m["role"],
            "content_type": m["content_type"],
            "content": content,
            "usage": m["usage"] or {},
            "created_at": m["created_at"],
        })

    meta = s["metadata"] or {}
    return {
        "id": str(s["id"]),
        "type": s["type"],
        "model": s["model"],
        "provider": s["provider"],
        "status": s["status"],
        "agent_slug": s["agent_slug"],
        "metadata": meta,
        "usage": s["usage"] or {},
        "created_at": s["created_at"],
        "last_activity_at": s["last_activity_at"],
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Cross-agent inbox (Conversations screen). Added 2026-06-12.
# ---------------------------------------------------------------------------

def _derive_channel(meta: dict) -> str:
    """Best-effort channel label for a chat session.

    Explicit metadata.channel wins (set by the email/telegram adapters); studio
    and web-chat collapse to 'chat'. Never invents -- defaults to 'chat' only for
    the in-app chat door, otherwise surfaces whatever service wrote the session.
    """
    ch = (meta or {}).get("channel")
    if ch:
        return ch
    svc = (meta or {}).get("service")
    if svc in (None, "", "studio", "web-chat", "chat"):
        return "chat"
    return svc


@sessions_router.get("")
def list_sessions(
    limit: int = 100,
    agent: str | None = None,
    principal: Principal = Depends(requires("sessions.read")),
):
    """All chat sessions across agents, newest first -- backs the Conversations inbox.

    Read-only, tenant-scoped. Each item carries the attributed agent slug and a
    derived channel ('chat' today; email/telegram arrive with their adapters,
    which set metadata.channel). Never fabricates: no sessions -> empty list.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be 1..500")

    where = ["s.type = 'chat'"]
    params: list = []
    if not principal.cross_tenant:
        where.append("s.tenant_id = %s")
        params.append(principal.tenant_id)
    if agent:
        where.append("ag.slug = %s")
        params.append(agent)

    sql = (
        "SELECT s.id, ag.slug AS agent_slug, s.model, s.provider, s.status, "
        "       s.metadata, s.created_at, s.last_activity_at, "
        "       (SELECT count(*) FROM sn_core.ai_session_messages m "
        "        WHERE m.session_id = s.id) AS message_count, "
        "       (SELECT m.content FROM sn_core.ai_session_messages m "
        "        WHERE m.session_id = s.id AND m.role = 'user' "
        "        ORDER BY m.seq ASC LIMIT 1) AS first_user "
        "FROM sn_core.ai_sessions s "
        "LEFT JOIN sn_config.agents ag ON ag.id = s.agent_id "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY s.last_activity_at DESC LIMIT %s"
    )
    params.append(limit)

    with cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    out = []
    for r in rows:
        meta = r["metadata"] or {}
        snippet = (r["first_user"] or "").strip().replace("\n", " ")
        if len(snippet) > SNIPPET_TRUNC:
            snippet = snippet[:SNIPPET_TRUNC] + "…"
        out.append({
            "id": str(r["id"]),
            "agent_slug": r["agent_slug"],
            "channel": _derive_channel(meta),
            "model": r["model"],
            "provider": r["provider"],
            "status": r["status"],
            "message_count": r["message_count"],
            "snippet": snippet,
            "created_at": r["created_at"],
            "last_activity_at": r["last_activity_at"],
        })
    return {"sessions": out}
