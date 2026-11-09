"""Canonical context-status read model for agent conversations.

This is the one product measurement consumed by Studio and node-local monitors.
It reads the bounded per-provider-request ledger written by the gateway and the
effective agent/global handoff policy; neither consumer repeats the arithmetic.

Architecture links are deliberate:

* reset provenance comes from the event ledger, not a synchronous
  side-channel bolted onto a model call.
* the product measurement has one declared home (this read model);
  instance-specific monitors remain consumers rather than alternate stores.
* new and changed checks require observed wire RED and GREEN evidence.

``context_tokens`` is exactly the latest completed provider request's
``input_tokens + cache_read_tokens + cache_write_tokens``.  It is never the
session roll-up, never message usage, and never includes output tokens.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import Principal, get_principal
from db import cursor
from permissions import requires


log = logging.getLogger("core-api.context-status")
router = APIRouter(prefix="/api", tags=["context-status"])

_ACTIVE_STATUSES = ("active", "streaming", "idle_released")


_ACTIVE_SQL = """
WITH current AS (
    SELECT DISTINCT ON (s.agent_id, s.metadata->>'conversation')
           s.id AS session_id, s.tenant_id, s.agent_id, a.slug AS agent,
           s.metadata->>'conversation' AS conversation,
           CASE
             WHEN s.metadata->>'conversation' LIKE 'desk:%%'
               THEN substring(s.metadata->>'conversation' FROM 6)
             WHEN s.metadata->>'conversation' LIKE '%%:desk:%%'
               THEN split_part(s.metadata->>'conversation', ':desk:', 2)
             ELSE NULL
           END AS desk_id,
           s.is_opaque, s.usage, s.last_activity_at
      FROM sn_core.ai_sessions s
      JOIN sn_config.agents a ON a.id = s.agent_id
     WHERE s.status = ANY(%(statuses)s)
       AND s.metadata->>'conversation' IS NOT NULL
       AND (%(cross_tenant)s OR s.tenant_id = %(tenant_id)s)
     ORDER BY s.agent_id, s.metadata->>'conversation',
              s.last_activity_at DESC, s.created_at DESC
)
SELECT c.*,
       jsonb_typeof(c.usage->'requests') AS ledger_type,
       c.usage->'requests'->-1 AS latest_request,
       p.scope_type AS threshold_scope,
       p.threshold_tokens,
       p.enabled AS policy_enabled,
       r.event_id AS reset_event_id, r.reset_at, r.reset_payload
  FROM current c
  LEFT JOIN LATERAL (
      SELECT l.scope_type, l.threshold_tokens, l.enabled
        FROM sn_config.session_context_limits l
       WHERE (l.scope_type = 'agent' AND l.scope_id = c.agent_id::text)
          OR (l.scope_type = 'global' AND l.scope_id IS NULL)
       ORDER BY CASE l.scope_type WHEN 'agent' THEN 0 ELSE 1 END
       LIMIT 1
  ) p ON true
  LEFT JOIN LATERAL (
      SELECT e.id AS event_id, e.ts AS reset_at, e.payload AS reset_payload
        FROM sn_core.events e
        JOIN sn_core.ai_sessions old ON old.id::text = e.entity_id
       WHERE e.event_type = 'session.context_reset'
         AND e.entity_type = 'ai_session'
         AND old.agent_id IS NOT DISTINCT FROM c.agent_id
         AND (
              (c.desk_id IS NOT NULL AND (
                   old.metadata->>'conversation' = 'desk:' || c.desk_id
                OR old.metadata->>'conversation' LIKE '%%:desk:' || c.desk_id
              ))
              OR (c.desk_id IS NULL
                  AND old.metadata->>'conversation' = c.conversation)
         )
       ORDER BY e.ts DESC, e.id DESC
       LIMIT 1
  ) r ON true
 ORDER BY c.last_activity_at DESC
"""


_DESKS_SQL = """
WITH current AS (
    SELECT d.id::text AS desk_id, d.tenant_id,
           s.id AS session_id,
           COALESCE(s.agent_id, configured_agent.id) AS agent_id,
           COALESCE(session_agent.slug, configured_agent.slug) AS agent,
           COALESCE(
               s.metadata->>'conversation',
               CASE WHEN configured_agent.slug IS NOT NULL
                    THEN configured_agent.slug || ':desk:' || d.id::text
                    ELSE 'desk:' || d.id::text END
           ) AS conversation,
           s.is_opaque, COALESCE(s.usage, '{}'::jsonb) AS usage,
           s.last_activity_at
      FROM sn_core.desks d
      LEFT JOIN sn_config.agents configured_agent
        ON configured_agent.slug = d.metadata->>'agent'
       AND configured_agent.archived_at IS NULL
      LEFT JOIN LATERAL (
          SELECT ss.*
            FROM sn_core.ai_sessions ss
            LEFT JOIN sn_config.agents sa ON sa.id = ss.agent_id
           WHERE ss.tenant_id = d.tenant_id
             AND ss.status = ANY(%(statuses)s)
             AND (
                  ss.metadata->>'conversation' = 'desk:' || d.id::text
               OR ss.metadata->>'conversation' LIKE '%%:desk:' || d.id::text
             )
             AND (configured_agent.id IS NULL OR ss.agent_id = configured_agent.id)
           ORDER BY ss.last_activity_at DESC, ss.created_at DESC
           LIMIT 1
      ) s ON true
      LEFT JOIN sn_config.agents session_agent ON session_agent.id = s.agent_id
     WHERE d.status = 'active'
       AND d.tenant_id = %(tenant_id)s
       AND d.owner_user_id = %(user_id)s
       AND (%(desk_id)s IS NULL OR d.id::text = %(desk_id)s)
)
SELECT c.*,
       jsonb_typeof(c.usage->'requests') AS ledger_type,
       c.usage->'requests'->-1 AS latest_request,
       p.scope_type AS threshold_scope,
       p.threshold_tokens,
       p.enabled AS policy_enabled,
       r.event_id AS reset_event_id, r.reset_at, r.reset_payload
  FROM current c
  LEFT JOIN LATERAL (
      SELECT l.scope_type, l.threshold_tokens, l.enabled
        FROM sn_config.session_context_limits l
       WHERE (l.scope_type = 'agent' AND l.scope_id = c.agent_id::text)
          OR (l.scope_type = 'global' AND l.scope_id IS NULL)
       ORDER BY CASE l.scope_type WHEN 'agent' THEN 0 ELSE 1 END
       LIMIT 1
  ) p ON true
  LEFT JOIN LATERAL (
      SELECT e.id AS event_id, e.ts AS reset_at, e.payload AS reset_payload
        FROM sn_core.events e
        JOIN sn_core.ai_sessions old ON old.id::text = e.entity_id
       WHERE e.event_type = 'session.context_reset'
         AND e.entity_type = 'ai_session'
         AND old.agent_id IS NOT DISTINCT FROM c.agent_id
         AND (
              old.metadata->>'conversation' = 'desk:' || c.desk_id
           OR old.metadata->>'conversation' LIKE '%%:desk:' || c.desk_id
         )
       ORDER BY e.ts DESC, e.id DESC
       LIMIT 1
  ) r ON true
 ORDER BY c.last_activity_at DESC NULLS LAST, c.desk_id
"""


class ContextStatusReadError(ValueError):
    """The ledger exists but does not satisfy its product contract."""


def _token(value: object, field: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise ContextStatusReadError(f"latest request {field} is not an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContextStatusReadError(
            f"latest request {field} is not an integer"
        ) from exc
    if number < 0:
        raise ContextStatusReadError(f"latest request {field} is negative")
    return number


def _context_tokens(ledger_type: str | None, request: object) -> int | None:
    """Return the canonical latest-request context, loudly rejecting drift."""
    if ledger_type not in (None, "array"):
        raise ContextStatusReadError("session usage.requests is not an array")
    if request is None:
        return None
    if not isinstance(request, dict):
        raise ContextStatusReadError("latest provider request is not an object")
    return sum(
        _token(request.get(field), field)
        for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )


def _serialize(row: dict) -> dict:
    context_tokens = _context_tokens(row.get("ledger_type"), row.get("latest_request"))
    threshold = row.get("threshold_tokens")
    supports_handoff = not row.get("session_id") or bool(row.get("is_opaque"))
    enabled = (
        bool(row.get("policy_enabled"))
        and threshold is not None
        and int(threshold) > 0
        and supports_handoff
    )
    threshold = int(threshold) if threshold is not None else None
    # How full the context is does not depend on whether handoff can act on it:
    # an HTTP agent (http-agent on Gemini) has no handoff, and computing the percent
    # only when `enabled` hid her meter entirely. `enabled` keeps its
    # meaning, automatic handoff runs for this conversation.
    percent = None
    if threshold and threshold > 0 and context_tokens is not None:
        percent = context_tokens * 100 // threshold

    reset = None
    if row.get("reset_event_id") is not None:
        payload = row.get("reset_payload") or {}
        reset = {
            "event_id": int(row["reset_event_id"]),
            "at": row.get("reset_at"),
            "before_context_tokens": payload.get("before_context_tokens"),
            "after_context_tokens": payload.get("after_context_tokens"),
            "handoff_doc_id": payload.get("handoff_doc_id"),
        }

    return {
        "session_id": str(row["session_id"]) if row.get("session_id") else None,
        "agent": row.get("agent"),
        "conversation": row.get("conversation"),
        "desk_id": row.get("desk_id"),
        "context_tokens": context_tokens,
        "threshold_tokens": threshold,
        "threshold_scope": row.get("threshold_scope"),
        "percent": percent,
        "enabled": enabled,
        "last_activity": row.get("last_activity_at"),
        "last_reset": reset,
    }


@router.get("/context-status",
            dependencies=[Depends(requires("context_status.read"))])
@router.get("/desks/context-status", include_in_schema=False,
            dependencies=[Depends(requires("context_status.read"))])
def context_status(
    scope: Literal["active", "desks"] = Query(default="active"),
    desk_id: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
):
    """Return current context status in one bulk read.

    ``scope=active`` is the node-admin monitor view: one newest non-terminal
    session per agent/conversation across the node. Ordinary tenant members
    cannot enumerate other owners' session/conversation identifiers.
    ``scope=desks`` is the Studio view: one row per active
    desk owned by the caller, including an explicit null session while a clean
    successor has not been created.  ``desk_id`` makes the latter a cheap poll
    of only the open chat without changing the response contract.
    """
    if desk_id is not None and scope != "desks":
        raise HTTPException(400, detail="desk_id requires scope=desks")
    if scope == "active" and not principal.cross_tenant:
        raise HTTPException(
            403,
            detail={
                "error": "context_status_forbidden",
                "reason": "scope=active is node-level monitor data",
            },
        )

    params = {
        "statuses": list(_ACTIVE_STATUSES),
        "cross_tenant": principal.cross_tenant,
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "desk_id": desk_id,
    }
    sql = _DESKS_SQL if scope == "desks" else _ACTIVE_SQL
    try:
        with cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
        contexts = [_serialize(dict(row)) for row in rows]
    except ContextStatusReadError as exc:
        log.error("context ledger contract failure: %s", exc)
        raise HTTPException(
            503,
            detail={"error": "context_status_unavailable", "reason": str(exc)},
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("context status read failed")
        raise HTTPException(
            503,
            detail={
                "error": "context_status_unavailable",
                "reason": "could not read the gateway ledger or context policy",
            },
        ) from exc
    return {"scope": scope, "contexts": contexts}
