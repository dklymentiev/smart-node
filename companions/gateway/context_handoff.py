"""Context-budget handoff and clean-session reset.

The gateway owns this because it is the provider-neutral point that sees the
latest response usage and controls session lifecycle.  No Claude-specific hook
is involved: any opaque CLI backend implementing SessionBackend can use it.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

import config
import db
import budget


logger = logging.getLogger(__name__)

# A handoff is a short lifecycle transition between the user-visible response
# and the durable terminal state.  The route holds the session's turn claim for
# the whole window; this set only stops a second threshold handler for the same
# session from writing a second handoff document.
_HANDOFF_IN_PROGRESS: set[str] = set()

# Sessions whose last handoff attempt failed, and what has to change before the
# next attempt.  Without this, a memory outage turns every following turn into
# another full-context generation: the spend doubles exactly when the node is
# already broken, and nothing gets written either way.
_HANDOFF_BACKOFF: Dict[str, Dict[str, float]] = {}

HANDOFF_PROMPT = """You have reached this node's context handoff boundary.
Write a self-contained handoff for your next clean session. Preserve the facts,
decisions, commitments, unfinished work, exact identifiers and file paths that
the next session needs. State uncertainty honestly. Do not call tools and do not
address the user. Return only concise Markdown; this text is stored in memory
and injected into your next session."""


def context_tokens(usage: Optional[Dict[str, Any]]) -> int:
    """Context of one provider response, never the cumulative session rollup."""
    usage = usage or {}
    return sum(
        int(usage.get(name, 0) or 0)
        for name in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )


def turn_context_tokens(usage: Optional[Dict[str, Any]]) -> int:
    """Context the session holds after a turn: its LAST provider request.

    A claude-cli turn reports the CLI `result` usage, which sums every request
    of the turn; the per-request list rides along in ``requests``.
    Comparing the sum with the threshold reset sessions whose real context was
    a fraction of it (63k measured, 481k recorded). A backend that makes
    one request per turn sends no list, and its usage is that request.
    A marked cumulative total without request records has unknown context;
    it must never trigger a reset based on the total billed input.
    """
    usage = usage or {}
    requests = usage.get("requests")
    if isinstance(requests, list) and requests and isinstance(requests[-1], dict):
        return context_tokens(requests[-1])
    if usage.get("cumulative"):
        logger.warning("context size unknown: cumulative turn usage has no final request record")
        return 0
    return context_tokens(usage)


def is_handoff_in_progress(session_id: str) -> bool:
    return session_id in _HANDOFF_IN_PROGRESS


def _backoff_blocks(session_id: str, context_now: int) -> bool:
    """True while a failed session must not pay for another handoff attempt.

    Either enough time has passed for the memory service to have come back, or
    the session has grown enough that a fresh attempt is worth its price again.
    """
    state = _HANDOFF_BACKOFF.get(session_id)
    if not state:
        return False
    if time.time() >= state["retry_after"]:
        return False
    if context_now >= state["retry_at_tokens"]:
        return False
    return True


def _note_failure(session_id: str, context_now: int) -> None:
    _HANDOFF_BACKOFF[session_id] = {
        "retry_after": time.time() + config.CONTEXT_HANDOFF_RETRY_COOLDOWN,
        "retry_at_tokens": int(context_now * config.CONTEXT_HANDOFF_RETRY_GROWTH),
    }


def _clear_failure(session_id: str) -> None:
    _HANDOFF_BACKOFF.pop(session_id, None)


def _lc(value: Any) -> str:
    """Tag values are lowercased on write by Mesh (mesh/utils.py normalize_tags).

    Its tag filter is an exact match, so a value we do not lowercase OURSELVES
    is a document we can never find again -- silently, since an empty result
    looks exactly like "this conversation has no handoff yet".
    """
    return str(value).strip().lower()


def _ensure_actor(cur, actor: str) -> None:
    prefix = actor.split(":", 1)[0] if ":" in actor else actor
    if prefix in ("user", "human"):
        layer = "human"
    elif prefix in ("component", "system"):
        layer = "system"
    else:
        layer = "specialist"
    cur.execute(
        "INSERT INTO sn_core.actors (id, display_name, type, layer) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (actor, actor, prefix, layer),
    )


def emit_event(event_type: str, session_id: str, payload: Dict[str, Any]) -> int:
    """Write a lifecycle event.  Raises: losing reset audit is not silent."""
    with db.with_transaction() as conn:
        cur = conn.cursor()
        _ensure_actor(cur, "component:gateway")
        cur.execute(
            """INSERT INTO sn_core.events
                   (actor, event_type, entity_type, entity_id, payload)
               VALUES ('component:gateway', %s, 'ai_session', %s, %s::jsonb)
               RETURNING id""",
            (event_type, session_id, json.dumps(payload)),
        )
        return int(cur.fetchone()[0])


def _identity_and_limit(sess: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve agent/workspace/conversation and the effective policy."""
    agent_id = sess.get("agent_id")
    metadata = sess.get("metadata") or {}
    conversation = metadata.get("conversation")
    if not agent_id or not conversation or not sess.get("is_opaque"):
        return None

    agent_rows = db.query(
        """SELECT slug, mesh_workspace
             FROM sn_config.agents
            WHERE id = %s AND archived_at IS NULL""",
        (agent_id,), fetch=True, as_dict=True,
    )
    if not agent_rows:
        return None

    threshold = config.CONTEXT_HANDOFF_THRESHOLD
    enabled = True
    scope_type = "fallback"
    try:
        rows = db.query(
            """SELECT scope_type, threshold_tokens, enabled
                 FROM sn_config.session_context_limits
                WHERE (scope_type = 'agent' AND scope_id = %s)
                   OR (scope_type = 'global' AND scope_id IS NULL)
                ORDER BY CASE scope_type WHEN 'agent' THEN 0 ELSE 1 END
                LIMIT 1""",
            (str(agent_id),), fetch=True, as_dict=True,
        )
        if rows:
            threshold = int(rows[0]["threshold_tokens"])
            enabled = bool(rows[0]["enabled"])
            scope_type = rows[0]["scope_type"]
    except Exception as exc:
        # Upgrade overlap: a new gateway may start seconds before B02 applies
        # the new table.  The documented 120k fallback keeps behaviour safe.
        logger.warning("context limit lookup failed; using fallback: %s", exc)

    agent = agent_rows[0]
    return {
        "agent_id": int(agent_id),
        "agent_slug": agent["slug"],
        "workspace": agent["mesh_workspace"],
        "conversation": str(conversation),
        "threshold_tokens": threshold,
        "enabled": enabled,
        "scope_type": scope_type,
    }


def _mesh_headers(workspace: str) -> Dict[str, str]:
    headers = {"X-Workspace": workspace, "Content-Type": "application/json"}
    if config.MESH_API_KEY:
        headers["X-API-Key"] = config.MESH_API_KEY
    return headers


async def _write_handoff(content: str, identity: Dict[str, Any], session_id: str) -> str:
    if not identity.get("workspace"):
        raise RuntimeError("agent has no mesh_workspace")
    body = {
        "content": (
            f"# Session handoff\n\n"
            f"Agent: {identity['agent_slug']}\n\n"
            f"Conversation: {identity['conversation']}\n\n"
            f"Prior session: {session_id}\n\n"
            f"{content.strip()}"
        ),
        "tags": [
            "type:handoff",
            f"agent:{_lc(identity['agent_slug'])}",
            f"session:{_lc(session_id)}",
            f"conversation:{_lc(identity['conversation'])}",
        ],
        "source": "gateway-handoff",
    }
    async with httpx.AsyncClient(timeout=config.MESH_HANDOFF_TIMEOUT) as client:
        response = await client.put(
            f"{config.MESH_API_URL}/",
            headers=_mesh_headers(identity["workspace"]),
            json=body,
        )
        response.raise_for_status()
        data = response.json()
    guid = data.get("guid")
    if not guid:
        raise RuntimeError("Mesh accepted handoff without returning a guid")
    return str(guid)


def _record_handoff_usage(session_service, session_id: str, usage: Dict[str, Any]) -> None:
    if not usage:
        return
    session_service.record_usage(
        session_id,
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cost_usd=float(usage.get("cost_usd", 0) or 0),
        estimated=bool(usage.get("estimated", False)),
        cache_read_tokens=int(usage.get("cache_read_tokens", 0) or 0),
        cache_write_tokens=int(usage.get("cache_write_tokens", 0) or 0),
        requests=usage.get("requests", []),
    )


def _finalize_reset(session_id: str, guid: str, before_tokens: int,
                    identity: Dict[str, Any], handoff_tokens: int) -> int:
    """Atomically make the old session terminal and expose its reset event."""
    payload = {
        "before_context_tokens": before_tokens,
        "after_context_tokens": 0,
        "threshold_tokens": identity["threshold_tokens"],
        "threshold_scope": identity["scope_type"],
        "handoff_context_tokens": handoff_tokens,
        "handoff_doc_id": guid,
        "agent": identity["agent_slug"],
        "conversation": identity["conversation"],
    }
    with db.with_transaction() as conn:
        cur = conn.cursor()
        _ensure_actor(cur, "component:gateway")
        cur.execute(
            """UPDATE sn_core.ai_sessions
                  SET status = 'completed', completed_at = now(), error = NULL,
                      metadata = COALESCE(metadata, '{}'::jsonb)
                          || jsonb_build_object('handoff_doc_id', %s)
                WHERE id = %s""",
            (guid, session_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"session {session_id} vanished before context reset")
        cur.execute(
            """INSERT INTO sn_core.events
                   (actor, event_type, entity_type, entity_id, payload)
               VALUES ('component:gateway', 'session.context_reset',
                       'ai_session', %s, %s::jsonb)
               RETURNING id""",
            (session_id, json.dumps(payload)),
        )
        return int(cur.fetchone()[0])


def finalize_manual_clean(session_id: str, sess: Dict[str, Any],
                          requested_by: str, reason: str) -> Dict[str, Any]:
    """Atomically retire one session and audit an owner-requested clean start.

    Unlike automatic handoff this deliberately writes no handoff document. The
    visible Studio history, Mesh and Documents remain in their own stores, but
    the successor receives neither this CLI transcript nor an injected handoff.
    """
    metadata = sess.get("metadata") or {}
    usage = sess.get("usage") or {}
    requests = usage.get("requests") if isinstance(usage, dict) else None
    latest = requests[-1] if isinstance(requests, list) and requests else None
    before_tokens = context_tokens(latest) if isinstance(latest, dict) else None
    actor = f"user:{requested_by or 'studio'}"
    payload = {
        "reason": reason,
        "requested_by": requested_by or "studio",
        "agent_id": sess.get("agent_id"),
        "conversation": metadata.get("conversation"),
        "before_context_tokens": before_tokens,
        "preserved": ["studio_chat_history", "mesh", "documents"],
        "discarded": ["provider_transcript_context", "unsaved_conversation_facts"],
    }
    with db.with_transaction() as conn:
        cur = conn.cursor()
        _ensure_actor(cur, actor)
        cur.execute(
            """UPDATE sn_core.ai_sessions
                  SET status = 'completed', completed_at = now(), error = NULL,
                      metadata = COALESCE(metadata, '{}'::jsonb)
                          || jsonb_build_object('manual_context_clear', true)
                WHERE id = %s
                  AND status IN ('active', 'streaming', 'idle_released')""",
            (session_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"session {session_id} is no longer active")
        cur.execute(
            """INSERT INTO sn_core.events
                   (actor, event_type, entity_type, entity_id, payload)
               VALUES (%s, 'session.context_cleared', 'ai_session', %s, %s::jsonb)
               RETURNING id, ts""",
            (actor, session_id, json.dumps(payload)),
        )
        event_id, event_at = cur.fetchone()
    return {
        "session_id": session_id,
        "status": "completed",
        "event_id": int(event_id),
        "event_at": event_at.isoformat() if hasattr(event_at, "isoformat") else str(event_at),
        "before_context_tokens": before_tokens,
    }


async def maybe_handoff(session_service, session_id: str, sess: Dict[str, Any],
                        backend, usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Generate/store a handoff and retire a session after its threshold crossing.

    Any failure leaves the DB session non-terminal and emits
    ``session.handoff_failed``.  The user-visible answer has already been stored,
    so failure of this maintenance turn never loses that answer.
    """
    identity = _identity_and_limit(sess)
    before_tokens = turn_context_tokens(usage)
    if not identity or not identity["enabled"] or before_tokens < identity["threshold_tokens"]:
        return None

    if _backoff_blocks(session_id, before_tokens):
        logger.info(
            "context handoff skipped session=%s: still in backoff after a failed attempt",
            session_id,
        )
        return None

    # The event loop cannot interleave between this membership test and add.
    # A duplicate threshold handler therefore becomes a no-op rather than
    # creating two handoff documents for the same session.
    if session_id in _HANDOFF_IN_PROGRESS:
        return None
    _HANDOFF_IN_PROGRESS.add(session_id)

    base_payload = {
        "context_tokens": before_tokens,
        "threshold_tokens": identity["threshold_tokens"],
        "threshold_scope": identity["scope_type"],
        "agent": identity["agent_slug"],
        "conversation": identity["conversation"],
    }
    try:
        emit_event("session.context_threshold_crossed", session_id, base_payload)
        history = session_service.get_history(session_id)
        # Do not wrap this in an outer wait_for. CLI backends own their turn
        # deadline and, critically, kill the child process when it expires. An
        # asyncio cancellation would release the send lock while leaving the
        # process writing to the same pipe; the next person's turn would then
        # consume the abandoned handoff response.
        handoff_response = await backend.send(session_id, history, HANDOFF_PROMPT)
        handoff_text = (handoff_response.content or "").strip()
        if len(handoff_text) < 10:
            raise RuntimeError("provider returned an empty handoff")
        _record_handoff_usage(session_service, session_id, handoff_response.usage or {})
        if handoff_response.usage:
            # The handoff is a real provider request and belongs in the same
            # tenant/agent budget as the turn that triggered it.  Accounting is
            # best-effort, like the normal response path: a transient budget DB
            # failure must not discard a valid handoff or keep a full session
            # alive until provider-side compaction.
            try:
                budget.record_spend(
                    tenant_id=int(sess["tenant_id"]),
                    cost_usd=float(handoff_response.usage.get("cost_usd", 0) or 0),
                    agent_id=sess.get("agent_id"),
                    metadata=sess.get("metadata") or {},
                )
            except Exception:
                logger.exception("handoff budget accounting failed session=%s", session_id)
        guid = await _write_handoff(handoff_text, identity, session_id)
        event_id = _finalize_reset(
            session_id, guid, before_tokens, identity,
            turn_context_tokens(handoff_response.usage),
        )
        try:
            await backend.close(session_id)
        except Exception as exc:
            # The durable terminal state prevents reuse; leaking a child process is
            # still loud in the journal, but must not undo a stored handoff/reset.
            logger.exception("backend close after context reset failed session=%s", session_id)
            try:
                emit_event(
                    "session.context_reset_cleanup_failed", session_id,
                    {**base_payload, "handoff_doc_id": guid, "error": str(exc)[:500]},
                )
            except Exception:
                logger.exception("could not audit reset cleanup failure session=%s", session_id)
        _clear_failure(session_id)
        return {
            "handoff_doc_id": guid,
            "event_id": event_id,
            "before_context_tokens": before_tokens,
            "after_context_tokens": 0,
        }
    except Exception as exc:
        logger.exception("context handoff failed session=%s", session_id)
        _note_failure(session_id, before_tokens)
        try:
            emit_event(
                "session.handoff_failed", session_id,
                {
                    **base_payload,
                    "error": str(exc)[:500],
                    "retry_after_seconds": config.CONTEXT_HANDOFF_RETRY_COOLDOWN,
                    "retry_at_context_tokens": int(
                        before_tokens * config.CONTEXT_HANDOFF_RETRY_GROWTH
                    ),
                },
            )
        except Exception:
            logger.exception("could not record session.handoff_failed session=%s", session_id)
        return None
    finally:
        _HANDOFF_IN_PROGRESS.discard(session_id)


def _predecessor_handoff_guid(agent_id: Any, conversation: str) -> Optional[str]:
    """The document the previous session of this conversation handed off to.

    Read from OUR OWN record of the reset rather than from a tag search: the
    session we closed carries the guid in its metadata, so the injection cannot
    miss because of how a conversation id happens to be spelled.
    """
    rows = db.query(
        """SELECT metadata->>'handoff_doc_id' AS guid,
                  metadata->>'manual_context_clear' = 'true' AS manually_cleared
             FROM sn_core.ai_sessions
            WHERE agent_id = %s
              AND metadata->>'conversation' = %s
              AND status IN ('completed', 'cancelled', 'failed', 'reaped')
              AND (
                   metadata->>'handoff_doc_id' IS NOT NULL
                   OR metadata->>'manual_context_clear' = 'true'
              )
            ORDER BY COALESCE(completed_at, last_activity_at) DESC NULLS LAST,
                     created_at DESC, id DESC
            LIMIT 1""",
        (agent_id, str(conversation)), fetch=True, as_dict=True,
    )
    if not rows or rows[0].get("manually_cleared"):
        return None
    return rows[0].get("guid")


async def inject_latest_handoff(session_service, session_id: str,
                                sess: Dict[str, Any]) -> Optional[str]:
    """Inject only the latest handoff for this agent/conversation into a new session."""
    metadata = sess.get("metadata") or {}
    workspace = metadata.get("mesh_workspace")
    conversation = metadata.get("conversation")
    agent_slug = metadata.get("agent_slug")
    if not (workspace and conversation and agent_slug and sess.get("agent_id")):
        return None

    guid = _predecessor_handoff_guid(sess.get("agent_id"), conversation)
    if not guid:
        # The normal case for a first session on a conversation.  Nothing was
        # reset, so there is nothing to carry over and nothing to report.
        return None
    try:
        async with httpx.AsyncClient(timeout=config.MESH_HANDOFF_READ_TIMEOUT) as client:
            response = await client.get(
                f"{config.MESH_API_URL}/{guid}",
                headers=_mesh_headers(workspace),
            )
            response.raise_for_status()
            doc = response.json()
        content = (doc.get("content") or "").strip()
        if not content:
            raise RuntimeError(f"Mesh handoff {guid} has no content")
        session_service.append_message(
            session_id,
            role="system",
            content=(
                "The prior context was deliberately reset. Continue from this "
                "handoff, treating it as previous-session context:\n\n" + content
            ),
            is_injected=True,
        )
        db.query(
            """UPDATE sn_core.ai_sessions
                  SET metadata = COALESCE(metadata, '{}'::jsonb)
                      || jsonb_build_object('injected_handoff_doc_id', %s)
                WHERE id = %s""",
            (str(guid), session_id),
        )
        emit_event(
            "session.handoff_injected", session_id,
            {"handoff_doc_id": str(guid), "agent": agent_slug,
             "conversation": str(conversation)},
        )
        return str(guid)
    except Exception as exc:
        # Loud: a reset DID happen, the document exists, and this session is
        # starting without it.  That is the difference between a clean session
        # and an amnesiac one.
        logger.exception("handoff injection failed session=%s", session_id)
        try:
            emit_event(
                "session.handoff_injection_failed", session_id,
                {"agent": agent_slug, "conversation": str(conversation),
                 "handoff_doc_id": str(guid), "error": str(exc)[:500]},
            )
        except Exception:
            logger.exception("could not record handoff injection failure session=%s", session_id)
        return None


def content_for_first_turn(history: list, content: str) -> str:
    """Prepend injected context exactly once, on the first real user turn."""
    injected = [m.get("content", "") for m in history if m.get("is_injected")]
    real_user_messages = [
        m for m in history if m.get("role") == "user" and not m.get("is_injected")
    ]
    if not injected or len(real_user_messages) > 1:
        return content
    return "\n\n".join(injected) + "\n\nCurrent message:\n" + content
