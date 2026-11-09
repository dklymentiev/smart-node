"""Context handoff thresholds for long-lived agent sessions.

The global row is the node default.  An agent row overrides it by agent id.
Writes are owner-only and are audited as ``config.changed`` in the same
transaction as the setting change.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import Principal, require_session
from db import cursor
from permissions import requires
from events_emit import emit


router = APIRouter(prefix="/api/session-context-limits", tags=["session-context"])


class ContextLimitUpdate(BaseModel):
    threshold_tokens: int = Field(ge=10000, le=1000000)
    enabled: bool = True
    reason: str = Field(
        default="updated through the session context limits API",
        min_length=1,
        max_length=500,
    )


def _require_owner(principal: Principal) -> None:
    # A role on an asserted header is not proof that the human owner made this
    # node-wide configuration decision.
    require_session(principal)
    if principal.role != "super-admin":
        raise HTTPException(403, "owner role required")


def _row(r) -> dict:
    return {
        "id": r["id"],
        "scope_type": r["scope_type"],
        "scope_id": r["scope_id"],
        "threshold_tokens": r["threshold_tokens"],
        "enabled": r["enabled"],
        "updated_at": r["updated_at"],
        "agent_slug": r.get("agent_slug"),
        "agent_title": r.get("agent_title"),
    }


def _value(r) -> dict | None:
    if r is None:
        return None
    return {
        "threshold_tokens": int(r["threshold_tokens"]),
        "enabled": bool(r["enabled"]),
    }


@router.get("")
def list_context_limits(principal: Principal = Depends(requires("session_context.read"))):
    """Return the node default and every per-agent override."""
    with cursor() as cur:
        cur.execute(
            """SELECT l.id, l.scope_type, l.scope_id, l.threshold_tokens,
                      l.enabled, l.updated_at, a.slug AS agent_slug,
                      a.title AS agent_title
                 FROM sn_config.session_context_limits l
                 LEFT JOIN sn_config.agents a
                   ON l.scope_type = 'agent' AND l.scope_id = a.id::text
                ORDER BY CASE l.scope_type WHEN 'global' THEN 0 ELSE 1 END,
                         COALESCE(a.slug, l.scope_id)"""
        )
        rows = cur.fetchall() or []
    global_row = next((_row(r) for r in rows if r["scope_type"] == "global"), None)
    overrides = [_row(r) for r in rows if r["scope_type"] == "agent"]
    return {"global": global_row, "agent_overrides": overrides}


def _upsert(scope_type: str, scope_id: str | None, payload: ContextLimitUpdate,
            principal: Principal) -> dict:
    _require_owner(principal)
    with cursor(commit=True) as cur:
        cur.execute(
            """SELECT threshold_tokens, enabled
                 FROM sn_config.session_context_limits
                WHERE scope_type = %s AND scope_id IS NOT DISTINCT FROM %s
                FOR UPDATE""",
            (scope_type, scope_id),
        )
        old = _value(cur.fetchone())
        cur.execute(
            """INSERT INTO sn_config.session_context_limits
                   (scope_type, scope_id, threshold_tokens, enabled)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (scope_type, scope_id) DO UPDATE
                   SET threshold_tokens = EXCLUDED.threshold_tokens,
                       enabled = EXCLUDED.enabled,
                       updated_at = now()
               RETURNING id, scope_type, scope_id, threshold_tokens,
                         enabled, updated_at""",
            (scope_type, scope_id, payload.threshold_tokens, payload.enabled),
        )
        row = cur.fetchone()
        entity_id = "global" if scope_type == "global" else f"agent/{scope_id}"
        actor = f"user:{principal.username or 'panel'}"
        new = {
            "threshold_tokens": payload.threshold_tokens,
            "enabled": payload.enabled,
        }
        emit(
            cur,
            actor=actor,
            event_type="config.changed",
            entity_type="session_context_limit",
            entity_id=entity_id,
            payload={
                "scope": {"type": scope_type, "id": scope_id},
                "old": old,
                "new": new,
                "actor": actor,
                "reason": payload.reason,
            },
        )
    return _row(row)


@router.put("/global")
def set_global_context_limit(
    payload: ContextLimitUpdate,
    principal: Principal = Depends(requires("session_context.write")),
):
    return _upsert("global", None, payload, principal)


@router.put("/agents/{agent_slug}")
def set_agent_context_limit(
    agent_slug: str,
    payload: ContextLimitUpdate,
    principal: Principal = Depends(requires("session_context.write")),
):
    _require_owner(principal)
    with cursor() as cur:
        cur.execute(
            "SELECT id FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (agent_slug,),
        )
        agent = cur.fetchone()
    if agent is None:
        raise HTTPException(404, f"agent '{agent_slug}' not found")
    return _upsert("agent", str(agent["id"]), payload, principal)


@router.delete("/agents/{agent_slug}")
def delete_agent_context_limit(
    agent_slug: str,
    reason: str = Query(
        default="agent override removed through the session context limits API",
        min_length=1,
        max_length=500,
    ),
    principal: Principal = Depends(requires("session_context.write")),
):
    _require_owner(principal)
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (agent_slug,),
        )
        agent = cur.fetchone()
        if agent is None:
            raise HTTPException(404, f"agent '{agent_slug}' not found")
        scope_id = str(agent["id"])
        cur.execute(
            "DELETE FROM sn_config.session_context_limits "
            "WHERE scope_type = 'agent' AND scope_id = %s "
            "RETURNING id, threshold_tokens, enabled",
            (scope_id,),
        )
        old_row = cur.fetchone()
        deleted = old_row is not None
        if deleted:
            cur.execute(
                """SELECT threshold_tokens, enabled
                     FROM sn_config.session_context_limits
                    WHERE scope_type = 'global' AND scope_id IS NULL"""
            )
            effective = _value(cur.fetchone())
            actor = f"user:{principal.username or 'panel'}"
            emit(
                cur,
                actor=actor,
                event_type="config.changed",
                entity_type="session_context_limit",
                entity_id=f"agent/{scope_id}",
                payload={
                    "scope": {"type": "agent", "id": scope_id},
                    "old": _value(old_row),
                    "new": None,
                    "effective_new": effective,
                    "actor": actor,
                    "reason": reason,
                },
            )
    return {"agent_slug": agent_slug, "deleted": deleted}
