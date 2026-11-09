"""Session-first gateway endpoints.

Adds new endpoints in parallel to existing /api/v1/sessions + /api/v1/chat.
The existing endpoints continue working unchanged -- this module is purely
additive until Stage 4 (brin migration) cuts traffic over.

Endpoints:
    POST   /api/v1/sessions/v2          create session (Mode A or Mode B)
    GET    /api/v1/sessions/v2          list sessions (tenant-filtered)
    GET    /api/v1/sessions/{id}/info   session metadata + usage
    GET    /api/v1/sessions/{id}/messages   history
    POST   /api/v1/sessions/{id}/messages   send message + get response
    DELETE /api/v1/sessions/{id}/v2     terminate

The /v2 path suffix on collection endpoints avoids conflict with existing
/api/v1/sessions (which uses CLI session_manager). The path parameter
endpoints (/sessions/{id}/...) use distinct sub-paths (/info, /messages)
that don't exist in the old API.

Deferred:
    POST /api/v1/sessions/{id}/inject   admin auth gate -- Stage 6
    GET  /api/v1/sessions/{id}/stream   SSE streaming -- Stage 2e
"""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import time
import uuid
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from session_service import service as session_service
import backends
import budget
import db
import model_door
import context_handoff
from backends.cli_claude import SpawnCapReached, TurnLimitExceeded
from turn_limits import record_turn_cutoff, turn_limit_phrase

logger = logging.getLogger(__name__)

# One user turn owns a session until its response maintenance (including a
# possible handoff) is finished. The claim is a QUEUE, not a refusal: a second
# user turn waits behind the first exactly as it used to wait on the CLI
# backend's per-session send lock, so a person who types again while the agent
# is working still gets both answers. What the queue adds over that lock is a
# claim that also spans the post-response handoff, so a turn already queued
# cannot overtake the handoff and land in a session that is being retired
_TURN_LOCKS: Dict[str, asyncio.Lock] = {}
# Turns queued behind the one holding the lock. A handoff is deliberately NOT
# started while someone is waiting: the burst finishes on the session it was
# addressed to, and the threshold is re-evaluated after the next response.
_TURN_WAITERS: Dict[str, int] = {}
# Browser-supplied IDs let the owner cancel one waiting turn, including two
# identical messages. A short-lived tombstone covers a click before Brin has
# finished preparing the gateway request.
_PENDING_TURNS: Dict[tuple, Dict[str, Any]] = {}
_CANCEL_TOMBSTONES: Dict[tuple, Dict[str, Any]] = {}
_CANCEL_TTL = 600


def _desk_from_session(sess: dict) -> Optional[str]:
    metadata = sess.get("metadata") or {}
    conversation = str(metadata.get("conversation") or metadata.get("user") or "")
    return conversation.rsplit("desk:", 1)[-1] if "desk:" in conversation else None


def _prune_cancel_tombstones() -> None:
    now = time.monotonic()
    for key, item in list(_CANCEL_TOMBSTONES.items()):
        if item["expires"] <= now:
            _CANCEL_TOMBSTONES.pop(key, None)


class SessionRetiredError(Exception):
    """A queued turn woke up to find its session handed off and closed."""


def _turn_lock(session_id: str) -> asyncio.Lock:
    """Per-session turn lock, lazily created on the running event loop."""
    lock = _TURN_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _TURN_LOCKS[session_id] = lock
    return lock


def _queued_turns(session_id: str) -> int:
    return _TURN_WAITERS.get(session_id, 0)


def _release_turn_lock(session_id: str) -> None:
    """Drop bookkeeping for a session nobody is using, so the maps stay bounded."""
    if _TURN_WAITERS.get(session_id):
        return
    lock = _TURN_LOCKS.get(session_id)
    if lock is not None and not lock.locked():
        _TURN_LOCKS.pop(session_id, None)
        _TURN_WAITERS.pop(session_id, None)

# Session statuses that are permanently closed. Must match the executor.py constant
# in brin (companions/brin/core/executor.py _TERMINAL_STATUSES). A terminal session
# cannot receive messages and cannot be meaningfully restarted: the process was
# reaped or the session was completed/cancelled, and the DB record is final.
_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "reaped"})

# How long to wait for the CLI to respond before returning 202 ("working") and
# letting the turn continue as a background task. Set below brin's own gateway
# timeout (300s) so this deadline fires gateway-side before brin gives up.
_SYNC_RESPONSE_TIMEOUT = float(os.environ.get("SN_SYNC_RESPONSE_TIMEOUT", "285"))
# The longest a caller may ask to wait instead (SendMessageRequest.wait_seconds):
# a live channel that holds the person's message until the turn ends. The turn
# limit ends a turn past this, so it is the same number.
_MAX_SYNC_WAIT = float(os.environ.get("SN_MAX_SYNC_WAIT", "3600"))


def _sync_wait(requested: Optional[float]) -> float:
    if requested is None:
        return _SYNC_RESPONSE_TIMEOUT
    return max(1.0, min(float(requested), _MAX_SYNC_WAIT))

# budget notifications.  communication.sh publishes the owner actor
# and email channel into an installer-owned contract consumed by this service.
# Exhaustion is NOT fire-and-forget: core-api atomically queues the owner email
# and only then stamps notified_at.  If config or core-api is absent, the POST
# fails and the next 402 retries because the budget row remains unclaimed.
_CORE_API_URL = os.environ.get("CORE_API_URL", "").rstrip("/")
_BUDGET_NOTIFY_ACTOR = os.environ.get("BUDGET_NOTIFY_ACTOR", "")
_BUDGET_NOTIFY_CHANNEL = os.environ.get("BUDGET_NOTIFY_CHANNEL", "")
_GATEWAY_CORE_API_SECRET = os.environ.get("SN_GATEWAY_CORE_API_SECRET", "")


def _notice_headers() -> dict[str, str]:
    """Prove this gateway to core-api for the configured owner notice paths."""
    if not _GATEWAY_CORE_API_SECRET or not _BUDGET_NOTIFY_ACTOR:
        raise RuntimeError("gateway core-api owner notice proof is not configured")
    return {
        # Kept for old core-api during the ordered gateway-then-core-api update.
        # New core-api checks the proof and refuses a mismatched actor name.
        "Remote-User": _BUDGET_NOTIFY_ACTOR,
        "X-SN-Service": "gateway",
        "X-SN-Service-Auth": _GATEWAY_CORE_API_SECRET,
        "X-SN-On-Behalf-Of": _BUDGET_NOTIFY_ACTOR,
    }


async def _send_budget_message(text: str) -> None:
    """Best-effort: send a budget alert to the owner via core-api messages/send.

    Fires as a background task; any failure is logged and swallowed so that the
    notification path never kills the gateway's response path.
    Skipped if BUDGET_NOTIFY_ACTOR is not set; the runner's notification path
    handles the send in that case.
    """
    if not _BUDGET_NOTIFY_ACTOR:
        return
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{_CORE_API_URL}/api/messages/send",
                headers=_notice_headers(),
                json={
                    "to": _BUDGET_NOTIFY_ACTOR,
                    "channel": _BUDGET_NOTIFY_CHANNEL,
                    "body": text,
                },
            )
            response.raise_for_status()
    except Exception as exc:
        logger.warning("budget owner notification failed (non-fatal): %s", exc)


async def _send_budget_exhaustion_notification(budget_id: int) -> bool:
    """Ask core-api to atomically enqueue + mark one exhausted budget row.

    A false result is deliberately non-fatal to the 402 response, but it is not
    treated as delivery: core-api leaves notified_at NULL, so a later hit gets
    another attempt.  A 200 `already_notified` is success and stays deduplicated.
    """
    if not _CORE_API_URL or not _BUDGET_NOTIFY_ACTOR or not _BUDGET_NOTIFY_CHANNEL:
        logger.warning(
            "budget exhaustion notification not configured; budget id=%s remains retryable",
            budget_id,
        )
        return False
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{_CORE_API_URL}/api/budgets/{budget_id}/notify-exhaustion",
                headers=_notice_headers(),
                json={"channel": _BUDGET_NOTIFY_CHANNEL},
            )
            response.raise_for_status()
            result = response.json()
            logger.info(
                "budget exhaustion notification budget=%s status=%s message=%s",
                budget_id,
                result.get("status"),
                result.get("message_id"),
            )
            return True
    except Exception as exc:
        logger.warning(
            "budget exhaustion notification failed; budget id=%s remains retryable: %s",
            budget_id,
            exc,
        )
        return False


async def _notify_turn_cutoff(notice: dict) -> bool:
    if not _CORE_API_URL or not _BUDGET_NOTIFY_ACTOR:
        logger.error("turn cutoff owner notification unavailable: service identity missing")
        return False
    import httpx as _httpx
    for attempt in range(3):
        try:
            async with _httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{_CORE_API_URL}/api/limits/notify-turn-cutoff",
                    headers=_notice_headers(), json=notice)
                response.raise_for_status()
                return True
        except Exception as exc:
            logger.warning("turn cutoff notification attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                await asyncio.sleep(1 + attempt)
    return False



def _door_caller(sess: Dict[str, Any], session_id: str):
    """Who the turn's model calls are for, when the gateway makes them itself
    (an HTTP backend; a CLI process holds a token that says it)."""
    slug = None
    if not sess.get("is_opaque") and sess.get("agent_id"):
        try:
            slug = (session_service.agent_state(sess["agent_id"]) or {}).get("slug")
        except Exception as e:  # noqa: BLE001
            logger.warning("agent of session %s unreadable for the record: %s", session_id, e)
    return model_door.calling(agent=slug, session_id=session_id, service="sessions")


async def _complete_turn_async(
    session_id: str,
    sess: Dict[str, Any],
    backend,
    history: list,
    content: str,
    user_msg_id: int,
) -> Dict[str, Any]:
    """Run the backend turn and write the result to the bus.

    Can be awaited directly (synchronous path) or run as an asyncio.Task (async
    path). In both cases it writes the completed turn to
    sn_core.ai_session_messages so the desk history shows the answer regardless
    of whether the originating HTTP request is still alive.

    On success: returns the HTTP-response dict.
    On transient error: logs a warning and re-raises (no session status change).
    On fatal error: sets session status to 'failed', logs, and re-raises.
    """
    try:
        with _door_caller(sess, session_id):
            response = await backend.send(session_id, history, content)
    except Exception as e:
        if isinstance(e, TurnLimitExceeded):
            line = f"Turn stopped by the {turn_limit_phrase(e.seconds)} limit."
            # The same durable assistant row feeds the desk and session chat,
            # including a background turn whose initial HTTP request got 202.
            assistant_msg_id = session_service.append_message(
                session_id, role="assistant", content=line, is_partial=True)
            notice = None
            try:
                notice = record_turn_cutoff(session_id, e.seconds)
            except Exception:
                logger.exception("turn cutoff journal failed session=%s", session_id)
            if notice and notice["notify"] and not await _notify_turn_cutoff(notice):
                logger.error("turn cutoff owner notification pending session=%s", session_id)
            # The limit ends the turn, not the conversation. The session used to
            # be set failed here, so the next message found it dead and the
            # agent started over in a new one without what it had done; on a
            # node in the field long work restarted from nothing this way. The
            # process is gone, as after Stop, and the next message resumes the
            # same session with --resume.
            return {"message_id": assistant_msg_id, "user_message_id": user_msg_id,
                    "response": {"role": "assistant", "content": line,
                                 "final_answer": "", "usage": {}, "tool_calls": []},
                    "stopped": True, "stopped_by": "turn_seconds", "outcome": "stopped",
                    "budgets": []}
        msg = str(e)
        transient = (
            "another coroutine is already waiting" in msg
            or "readline timeout" in msg
            or "readuntil() called" in msg
        )
        if isinstance(e, SpawnCapReached):
            # The node is full, the session is fine: refuse this turn only.
            logger.warning("spawn cap refused a turn session=%s: %s", session_id, e)
        elif isinstance(e, model_door.Refused):
            # The door's rules refused a model call (agent stopped, budget
            # spent): the turn ends, the session is fine.
            logger.warning("the door refused a turn's call session=%s: %s", session_id, e)
        elif transient:
            logger.warning("transient send error session=%s: %s", session_id, e)
        else:
            logger.exception("backend.send failed session=%s: %s", session_id, e)
            try:
                session_service.set_status(session_id, "failed", error=msg[:500])
            except Exception:
                pass
        raise

    # Persist the assistant response to the bus (sn_core.ai_session_messages).
    # The desk history query reads from this table, so writing here delivers
    # the answer to the person's chat even when the HTTP request has gone away.
    # A stopped turn keeps what it had said, marked partial. It writes its
    # row even when it had said nothing yet: the chat has to keep showing that
    # this message was stopped after a reload, and the row is where the chat
    # reads it from. Only CLI sessions can be stopped, and they never replay this
    # history to the model, so an empty assistant row reaches no provider.
    # What the chat keeps is the answer, as mail and Telegram already send it:
    # the turn's last text. The notes the agent writes between its steps ("I'll
    # check the details", "the draft has language artefacts, rewriting") were
    # glued into the stored reply and read as part of it;
    # the chat's live line shows them while the turn runs. A stopped or failed
    # turn has no final answer and keeps what it had said.
    said = (response.final_answer if (response.final_answer or "").strip() and not (response.stopped or response.failed)
            else response.content)
    assistant_msg_id = session_service.append_message(
        session_id, role="assistant", content=said,
        tool_calls=response.tool_calls, usage=response.usage,
        is_partial=response.stopped,
    )
    if response.usage:
        session_service.record_usage(
            session_id,
            input_tokens=int(response.usage.get("input_tokens", 0)),
            output_tokens=int(response.usage.get("output_tokens", 0)),
            cost_usd=float(response.usage.get("cost_usd", 0)),
            estimated=bool(response.usage.get("estimated", False)),
            cache_read_tokens=int(response.usage.get("cache_read_tokens", 0)),
            cache_write_tokens=int(response.usage.get("cache_write_tokens", 0)),
            requests=response.usage.get("requests", []),
        )
        try:
            # record_spend now returns any newly claimed pre-warnings.
            budget_warnings = budget.record_spend(
                tenant_id=int(sess["tenant_id"]),
                cost_usd=float(response.usage.get("cost_usd", 0)),
                agent_id=sess.get("agent_id"),
                metadata=sess.get("metadata") or {},
            )
            for w in (budget_warnings or []):
                pct = w.get("warn_at_pct", 90)
                period = w.get("period", "period")
                limit = w.get("limit_usd", 0)
                spent = w.get("current_spend_usd", 0)
                reset_at = w.get("reset_at", "the next reset time")
                warn_text = (
                    f"Budget pre-warning: the {period} limit is ${limit:.2f} "
                    f"and spend has reached ${spent:.2f} ({pct}% threshold). "
                    f"The limit resets at {reset_at}. "
                    f"No action is required yet, but the limit may be hit before reset."
                )
                asyncio.create_task(_send_budget_message(warn_text))
        except Exception as e:
            # Spend tracking must never fail the response.
            logger.exception("budget.record_spend failed session=%s: %s", session_id, e)

    if response.failed:
        # Journalled like a turn that raised: the monitor that mails
        # *.failed events sees it. The session itself is fine.
        _record_turn_failure(session_id, RuntimeError(response.failed))

    _, _, budget_after = budget.check_budget(
        tenant_id=int(sess["tenant_id"]),
        agent_id=sess.get("agent_id"),
        metadata=sess.get("metadata") or {},
    )

    return {
        "message_id": assistant_msg_id,
        "user_message_id": user_msg_id,
        "response": {
            "role": "assistant",
            "content": said,
            "final_answer": (
                "" if response.stopped or response.failed else
                response.final_answer if response.final_answer is not None else
                "" if response.tool_calls else response.content
            ),
            "usage": response.usage,
            "tool_calls": response.tool_calls,
        },
        "stopped": response.stopped,
        "stopped_by": response.stopped_by,
        "failed": response.failed,
        "outcome": "stopped" if response.stopped else "failed" if response.failed else "ok",
        "budgets": budget_after,
    }


async def _run_queued_turn_async(
    session_id: str,
    backend,
    content: str,
    user_msg_id: int,
    result_future: "asyncio.Future[Dict[str, Any]]",
    pending: Optional[Dict[str, Any]] = None,
) -> None:
    """Run one turn under the session's turn claim, then do its maintenance.

    The caller's answer is published to ``result_future`` as soon as the turn
    itself is done. The context handoff runs AFTER that, still inside the claim:
    it is a second full-context provider request, and making the person wait for
    it would push a threshold-crossing turn past the sync window and turn their
    answer into a "still working" placeholder.

    The session row is re-read after the claim is taken, not passed in: whoever
    held the claim before us may have retired it.
    """
    counted = False
    try:
        if pending and pending["state"] == "cancelled":
            return
        _TURN_WAITERS[session_id] = _TURN_WAITERS.get(session_id, 0) + 1
        counted = True
        if pending:
            pending["counted"] = True
        async with _turn_lock(session_id):
            if not pending or pending["counted"]:
                _TURN_WAITERS[session_id] = max(0, _TURN_WAITERS.get(session_id, 1) - 1)
                if pending:
                    pending["counted"] = False
            counted = False
            if pending:
                if pending["state"] == "cancelled":
                    return
                # No await between this transition and the lock acquisition:
                # cancellation can never delete a message now being delivered.
                pending["state"] = "running"
            # The turn ahead of us may have handed this session off and closed
            # it. The stored message was never delivered, so say so precisely:
            # brin creates a clean session and sends the same text again.
            fresh = session_service.get_session(session_id)
            if not fresh or fresh["status"] in _TERMINAL_STATUSES:
                # The stored copy belongs to a session that never spoke it. It
                # is about to be re-sent on the clean session, and a person
                # should not see their own message twice.
                try:
                    session_service.discard_message(session_id, user_msg_id)
                except Exception:
                    logger.warning(
                        "could not discard undelivered message %s on retired session %s",
                        user_msg_id, session_id,
                    )
                raise SessionRetiredError(
                    (fresh or {}).get("status", "gone") if fresh else "gone"
                )
            # Read the history only now: a turn that waited must see the answer
            # of the turn it waited for.
            history = session_service.get_history(session_id)
            backend_content = context_handoff.content_for_first_turn(history, content)
            result = await _complete_turn_async(
                session_id, fresh, backend, history, backend_content, user_msg_id,
            )
            if not result_future.done():
                result_future.set_result(result)

            if result.get("stopped_by") == "turn_seconds":
                return

            if _queued_turns(session_id) > 0:
                # Someone is already waiting. Let the burst finish on this
                # session; the next response re-evaluates the threshold.
                logger.info(
                    "context handoff deferred session=%s: %d turn(s) queued",
                    session_id, _queued_turns(session_id),
                )
                return
            await context_handoff.maybe_handoff(
                session_service, session_id, fresh, backend,
                (result.get("response") or {}).get("usage"),
            )
    except Exception as exc:
        if not result_future.done():
            result_future.set_exception(exc)
            if not isinstance(exc, SessionRetiredError):
                _record_turn_failure(session_id, exc)
        else:
            # The answer was already delivered; this is maintenance failing.
            # context_handoff audits its own failures, so a log line is enough.
            logger.exception("post-response maintenance failed session=%s", session_id)
        raise
    finally:
        if counted and (not pending or pending["counted"]):
            _TURN_WAITERS[session_id] = max(0, _TURN_WAITERS.get(session_id, 1) - 1)
        if pending:
            _PENDING_TURNS.pop((pending["desk_id"], pending["client_turn_id"]), None)
        _release_turn_lock(session_id)


def _record_turn_failure(session_id: str, exc: BaseException) -> None:
    """Journal a turn that died before it produced an answer.

    The only trace used to be a line in this service's log. A background
    turn has no caller left to tell, so the agent simply stopped: on 2026-10-11
    an agent's turn died of a silent provider and nothing on the node knew
    for seven minutes. The type ends in ".failed" because that is the suffix the
    owner's event monitor already mails.

    A retired session is not passed here: that is a normal reset, not a death.
    Never raises: the turn's own error is what the caller has to see.
    """
    try:
        sess = session_service.get_session(session_id) or {}
        context_handoff.emit_event("turn.failed", session_id, {
            "conversation": (sess.get("metadata") or {}).get("conversation"),
            "model": sess.get("model"),
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        })
    except Exception:
        logger.exception("could not journal the failed turn of session=%s", session_id)


def _log_turn_task_error(task: "asyncio.Task[Any]") -> None:
    """Done callback: log if a background turn task raised an unhandled error.

    When send_message() returns 202 and the turn task continues in the
    background, nothing awaits it. Without this callback asyncio would print
    'Task exception was never retrieved' on GC. Here we retrieve it explicitly
    and log at ERROR so the session failure is visible in the gateway journal.
    """
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if isinstance(exc, SpawnCapReached):
        logger.warning("background turn refused by the spawn cap: %s", exc)
        return
    if isinstance(exc, model_door.Refused):
        logger.warning("background turn refused by the door: %s", exc)
        return
    if isinstance(exc, SessionRetiredError):
        # Not an error: the session was handed off and the caller was told to
        # re-send on a clean one. Logging it at ERROR would make a normal reset
        # look like a fault in the journal.
        return
    if exc:
        logger.error("background turn task completed with error: %s", exc)


router = APIRouter(prefix="/api/v1/sessions", tags=["sessions-v2"])


# -- Request / response models -------------------------------------------------

class CreateSessionV2Request(BaseModel):
    """Mode A: provide agent_slug. Mode B: provide model + type + (optional) system_prompt.

    Exactly one of agent_slug OR model must be set.
    """
    # Mode A
    agent_slug: Optional[str] = None
    # Mode B
    tenant_id: Optional[int] = None  # required for Mode B; Mode A derives from agent
    type: Optional[str] = None        # required for Mode B
    model: Optional[str] = None       # required for Mode B
    system_prompt: Optional[str] = None  # optional for Mode B
    # Both modes
    metadata: Optional[Dict[str, Any]] = None
    parent_session_id: Optional[str] = None


class CreateSessionV2Response(BaseModel):
    session_id: str
    provider: str
    model: str
    is_opaque: bool
    type: str
    created_at: str


class SessionInfoResponse(BaseModel):
    session_id: str
    tenant_id: int
    type: str
    model: str
    provider: str
    is_opaque: bool
    agent_id: Optional[int]
    status: str
    system_prompt: Optional[str]
    parent_session_id: Optional[str]
    metadata: Dict[str, Any]
    usage: Dict[str, Any]
    budget_key: Optional[str]
    created_at: str
    last_activity_at: str
    completed_at: Optional[str]
    error: Optional[str]


class SessionListItem(BaseModel):
    session_id: str
    type: str
    model: str
    provider: str
    is_opaque: bool
    status: str
    last_activity_at: str
    completed_at: Optional[str]
    usage: Dict[str, Any]


class SendMessageRequest(BaseModel):
    role: str = Field(default="user", description="user | system | tool")
    content: str
    wait_seconds: Optional[float] = Field(
        default=None,
        description=(
            "How long to hold the request for the answer before 202 'working'. "
            "Default SN_SYNC_RESPONSE_TIMEOUT; at most SN_MAX_SYNC_WAIT. A live "
            "channel that has nowhere to put a late answer asks for the whole turn."
        ),
    )
    raw_content: Optional[str] = Field(
        default=None,
        description=(
            "The raw user text before enrichment (caller identity, cross-channel "
            "history, memory context). When provided, this is what is stored in "
            "ai_session_messages for display; content (the enriched form) is still "
            "what the backend receives. Omit for non-user roles."
        ),
    )
    tool_call_id: Optional[str] = None
    client_turn_id: Optional[uuid.UUID] = None


class CancelQueuedRequest(BaseModel):
    desk_id: uuid.UUID
    client_turn_id: uuid.UUID


class SendMessageResponse(BaseModel):
    message_id: int
    response: Dict[str, Any]
        # {role: 'assistant', content: str, usage: dict, tool_calls?: list}


class ManualCleanRequest(BaseModel):
    requested_by: str = Field(default="studio", min_length=1, max_length=200)
    reason: str = Field(default="studio_manual_clean", min_length=1, max_length=500)


class InterruptRequest(BaseModel):
    """Stop a turn in flight. requested_by is the person, recorded so the
    stopped answer and the event can say who stopped it."""
    requested_by: Optional[str] = Field(default=None, max_length=200)


class DeskInterruptRequest(InterruptRequest):
    desk_id: str = Field(..., min_length=1, max_length=64)
    # The agent whose turn is stopped: the chat's own, or a colleague answering
    # in it (its chat wake). Unset: the chat's own turn.
    agent: Optional[str] = Field(None, max_length=64)


class DeskSteerRequest(BaseModel):
    desk_id: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=8000)


class HistoryItem(BaseModel):
    id: int
    seq: int
    role: str
    content_type: str
    content: Optional[str]
    artifact_uri: Optional[str]
    tool_calls: Optional[List[Dict[str, Any]]]
    tool_call_id: Optional[str]
    is_partial: bool
    is_injected: bool
    usage: Optional[Dict[str, Any]]
    created_at: str
    completed_at: Optional[str]


# -- Helpers -------------------------------------------------------------------

def _serialize_session(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert sn_core.ai_sessions row dict to JSON-safe response shape."""
    return {
        "session_id": str(row["id"]),
        "tenant_id": row["tenant_id"],
        "type": row["type"],
        "model": row["model"],
        "provider": row["provider"],
        "is_opaque": row["is_opaque"],
        "agent_id": row.get("agent_id"),
        "status": row["status"],
        "system_prompt": row.get("system_prompt"),
        "parent_session_id": str(row["parent_session_id"]) if row.get("parent_session_id") else None,
        "metadata": row.get("metadata") or {},
        "usage": row.get("usage") or {},
        "budget_key": row.get("budget_key"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "last_activity_at": row["last_activity_at"].isoformat() if row.get("last_activity_at") else None,
        "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None,
        "error": row.get("error"),
    }


def _serialize_message(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "role": row["role"],
        "content_type": row["content_type"],
        "content": row.get("content"),
        "artifact_uri": row.get("artifact_uri"),
        "tool_calls": row.get("tool_calls"),
        "tool_call_id": row.get("tool_call_id"),
        "is_partial": row["is_partial"],
        "is_injected": row["is_injected"],
        "usage": row.get("usage"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None,
    }


# -- Endpoints -----------------------------------------------------------------

@router.post("/v2", response_model=CreateSessionV2Response, status_code=201)
async def create_session_v2(req: CreateSessionV2Request):
    """Create a new session via SessionService.

    Mode A: provide agent_slug -- gateway resolves model + system_prompt + tenant from agent row.
    Mode B: provide tenant_id + type + model + (optional) system_prompt -- ad-hoc session.
    """
    has_agent = bool(req.agent_slug)
    has_raw = bool(req.model)
    if has_agent == has_raw:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of agent_slug (Mode A) or model (Mode B)",
        )

    try:
        if has_agent:
            session_id = session_service.create_from_agent(
                agent_slug=req.agent_slug,
                metadata=req.metadata,
                parent_session_id=req.parent_session_id,
            )
        else:
            if req.tenant_id is None or not req.type:
                raise HTTPException(
                    status_code=400,
                    detail="Mode B requires tenant_id, type, model",
                )
            session_id = session_service.create_adhoc(
                tenant_id=req.tenant_id,
                session_type=req.type,
                model=req.model,
                system_prompt=req.system_prompt,
                metadata=req.metadata,
                parent_session_id=req.parent_session_id,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=500, detail="session created but not retrievable")

    if has_agent:
        await context_handoff.inject_latest_handoff(session_service, session_id, sess)

    return CreateSessionV2Response(
        session_id=session_id,
        provider=sess["provider"],
        model=sess["model"],
        is_opaque=sess["is_opaque"],
        type=sess["type"],
        created_at=sess["created_at"].isoformat(),
    )


@router.get("/v2")
async def list_sessions_v2(
    tenant_id: int = Query(..., description="required; sessions are tenant-scoped"),
    status: Optional[str] = Query(None),
    agent_id: Optional[int] = Query(None),
    conversation: Optional[str] = Query(None, description="filter by metadata.conversation, e.g. desk:<id>"),
    limit: int = Query(50, le=200),
):
    """List sessions for a tenant. Filtered by status, agent_id and/or conversation."""
    rows = session_service.list_sessions(
        tenant_id=tenant_id, status=status, agent_id=agent_id,
        conversation=conversation, limit=limit,
    )
    return {
        "tenant_id": tenant_id,
        "count": len(rows),
        "sessions": [
            {
                "session_id": str(r["id"]),
                "type": r["type"],
                "model": r["model"],
                "provider": r["provider"],
                "is_opaque": r["is_opaque"],
                "status": r["status"],
                "last_activity_at": r["last_activity_at"].isoformat() if r["last_activity_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r.get("completed_at") else None,
                "usage": r.get("usage") or {},
            }
            for r in rows
        ],
    }


@router.get("/desk-activity")
async def get_desk_activity(desk_id: str = Query(..., description="desk UUID"),
                            since: int = Query(0, description="last step seq the caller has")):
    """Live turn status for the session currently serving a desk.

    Returns {in_progress, elapsed, action, action_elapsed} while a CLI turn is
    running for this desk. Returns {in_progress: false} otherwise.
    Responds in-process (no DB round-trip); safe to poll every 2 seconds.
    """
    from backends.cli_claude import get_desk_activity as _get_desk_activity
    return _get_desk_activity(desk_id, since)


@router.get("/desk-live")
async def get_desk_live(desk_id: str = Query(..., description="desk UUID")):
    """The same activity as /desk-activity, pushed as server-sent events on
    every change while the caller stays connected: the chat's live line."""
    from backends.cli_claude import desk_live
    return StreamingResponse(desk_live(desk_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/agent-activity")
async def get_agent_activity(agent: str = Query(..., description="agent slug")):
    """An agent's console: its turns in flight on every channel, with
    their steps, and the turn it finished last. In-process, safe to poll every
    2 seconds. Tool results are never included.
    """
    from backends.cli_claude import agent_activity
    return agent_activity(agent)


@router.get("/desks-in-progress")
async def list_desks_in_progress():
    """Every desk with a turn running now, with how many more are queued.

    One call for all of a person's chat tabs, answered in-process from the same
    table as /desk-activity. `queued` counts messages waiting behind the running
    turn on the same session; they are answered in order after it.
    """
    from backends.cli_claude import desks_in_progress
    items = desks_in_progress()
    seen = {item["session_id"] for item in items}
    # HTTP-model turns use the same gateway claim but have no CLI process in
    # desks_in_progress(). Expose their waiting messages after a page reload.
    for pending in _PENDING_TURNS.values():
        sid = pending["session_id"]
        lock = _TURN_LOCKS.get(sid)
        if (sid not in seen and pending["state"] == "queued"
                and lock is not None and lock.locked()):
            items.append({"desk_id": pending["desk_id"], "agent": pending.get("agent"),
                          "session_id": sid, "in_progress": True, "can_steer": False,
                          "action": None, "elapsed": 0})
            seen.add(sid)
    for item in items:
        item["queued"] = _queued_turns(item["session_id"])
        item["running_turn_id"] = next(
            (p["client_turn_id"] for p in _PENDING_TURNS.values()
             if p["session_id"] == item["session_id"] and p["state"] == "running"),
            None,
        )
        item["queued_turns"] = [
            {"client_turn_id": p["client_turn_id"], "message_id": p["message_id"]}
            for p in _PENDING_TURNS.values()
            if p["session_id"] == item["session_id"] and p["state"] == "queued"
        ]
    return {"desks": items}


class AgentStopRequest(BaseModel):
    agent: str
    requested_by: Optional[str] = None
    reason: Optional[str] = None


def _last_model_calls(session_ids: List[str]) -> Dict[str, dict]:
    """The latest model call of each session, from sn_core.model_calls."""
    rows = db.query(
        "SELECT DISTINCT ON (session_id) session_id, ts, model, outcome, status, duration_ms "
        "FROM sn_core.model_calls WHERE session_id = ANY(%s) ORDER BY session_id, ts DESC",
        (list(session_ids),), fetch=True, as_dict=True)
    return {r["session_id"]: {"at": r["ts"].isoformat(), "model": r["model"],
                              "outcome": r["outcome"], "status": r["status"],
                              "duration_ms": r["duration_ms"]} for r in rows or []}


def _model_calls_live(session_ids: List[str]) -> Dict[str, dict]:
    """The model call in flight of each session, from sn_core.model_calls_live:
    since when, how much has come back, and how long nothing has."""
    rows = db.query(
        "SELECT DISTINCT ON (session_id) session_id, model, started_at, last_event_at, bytes, "
        " EXTRACT(EPOCH FROM now() - COALESCE(last_event_at, started_at))::int AS silent "
        "FROM sn_core.model_calls_live WHERE session_id = ANY(%s) "
        "ORDER BY session_id, started_at DESC",
        (list(session_ids),), fetch=True, as_dict=True)
    return {r["session_id"]: {"model": r["model"], "since": r["started_at"].isoformat(),
                              "last_event_at": r["last_event_at"].isoformat() if r["last_event_at"] else None,
                              "bytes": r["bytes"], "silent_seconds": r["silent"]}
            for r in rows or []}


def activity(p: dict, now: Optional[datetime] = None) -> dict:
    """What a live process is waiting on and when it last showed life.

    waiting_on: "model" (a call in flight), "tool" (a tool is running),
    "cli" (a turn is on, between calls and tools), None (no turn: idle).
    last_activity_at: the latest of an answer event from the model, the end of the last
    model call, the start of the running tool and, for an idle process, the end
    of its last turn. silent_seconds is how long ago that was."""
    now = now or datetime.now(timezone.utc)
    turn = p.get("turn") or {}
    live = p.get("model_call")
    seen = []
    if live:
        seen.append(datetime.fromisoformat(live["last_event_at"] or live["since"]))
    if p.get("last_call"):
        seen.append(datetime.fromisoformat(p["last_call"]["at"]))
    if turn.get("action") and turn.get("action_elapsed") is not None:
        seen.append(now - timedelta(seconds=turn["action_elapsed"]))
    if not turn.get("in_progress") and p.get("idle_seconds") is not None:
        seen.append(now - timedelta(seconds=p["idle_seconds"]))
    last = max(seen) if seen else None
    if live:
        waiting = "model"
    elif turn.get("in_progress"):
        waiting = "tool" if turn.get("action") else "cli"
    else:
        waiting = None
    return {"waiting_on": waiting,
            "last_activity_at": last.isoformat() if last else None,
            "silent_seconds": max(0, int((now - last).total_seconds())) if last else None}


@router.get("/processes")
async def list_processes(agent: Optional[str] = Query(None, description="agent slug")):
    """The process table: every live agent process, whose it is, whether a
    turn is running and what it does, how long it has been idle, and its last
    model call. Answered from the runtime's own state, which is the ground
    truth: a process here is alive now. Safe to poll every few seconds.
    """
    from backends.cli_claude import processes
    items = processes(agent)
    calls: Dict[str, dict] = {}
    if items:
        try:
            calls = await asyncio.to_thread(_last_model_calls, [p["session_id"] for p in items])
        except Exception as e:  # noqa: BLE001 -- the table answers without it
            logger.warning("last model calls unavailable: %s", e)
    live: Dict[str, dict] = {}
    if items:
        try:
            live = await asyncio.to_thread(_model_calls_live, [p["session_id"] for p in items])
        except Exception as e:  # noqa: BLE001 -- the table answers without it
            logger.warning("model calls in flight unavailable: %s", e)
    for p in items:
        p["last_call"] = calls.get(p["session_id"])
        p["model_call"] = live.get(p["session_id"])
        p.update(activity(p))
    return {"processes": items, "count": len(items)}


@router.post("/agent-stop")
async def stop_agent_processes(req: AgentStopRequest):
    """End every live process of an agent now; sessions are kept. The caller
    (core-api) disables the agent first, so nothing respawns behind this."""
    from backends.cli_claude import stop_agent
    return await stop_agent(req.agent, req.requested_by, req.reason or "stopped by owner")


@router.post("/desk-interrupt")
async def interrupt_desk_turn(req: DeskInterruptRequest):
    """Stop the turn running for a desk, keeping its session.

    The session is found by the turn in flight, not by a stored key, because the
    stored key is what the old Studio Stop got wrong. Queued messages behind the
    stopped turn are not dropped: they were sent, and they run next.
    """
    from backends.cli_claude import desk_session_in_flight
    sid = desk_session_in_flight(req.desk_id, agent=req.agent, colleagues=True)
    if not sid:
        return {"interrupted": False, "reason": "no turn in flight for this desk"}
    return await _interrupt(sid, req.requested_by)


@router.post("/desk-steer")
async def steer_desk_turn(req: DeskSteerRequest):
    """Send text into the running Flint turn; never create a second turn."""
    from backends.cli_claude import desk_session_in_flight
    sid = desk_session_in_flight(req.desk_id)
    if not sid:
        return {"status": "too_late"}
    sess = session_service.get_session(sid)
    if not sess:
        return {"status": "too_late"}
    backend = backends.resolve_backend(sess["model"])
    if not hasattr(backend, "steer"):
        return {"status": "unsupported"}
    return await backend.steer(sid, req.text)


@router.post("/desk-cancel-queued")
async def cancel_queued_desk_turn(req: CancelQueuedRequest):
    """Cancel exactly one desk message before its gateway turn starts.

    A pending tombstone covers the short interval before Brin posts the message.
    The caller repeats this request until it sees cancelled or too_late.
    """
    _prune_cancel_tombstones()
    key = (str(req.desk_id), str(req.client_turn_id))
    item = _CANCEL_TOMBSTONES.get(key)
    if item and item["status"] != "pending":
        return {"status": item["status"]}
    pending = _PENDING_TURNS.get(key)
    if pending:
        if pending["state"] != "queued":
            _CANCEL_TOMBSTONES[key] = {"status": "too_late", "expires": time.monotonic() + _CANCEL_TTL}
            return {"status": "too_late"}
        # No await before the state changes; a lock waiter cannot cross into
        # running between the check and removal of the undelivered DB row.
        session_service.discard_message(pending["session_id"], pending["message_id"])
        pending["state"] = "cancelled"
        if pending["counted"]:
            sid = pending["session_id"]
            _TURN_WAITERS[sid] = max(0, _TURN_WAITERS.get(sid, 1) - 1)
            pending["counted"] = False
        if not pending["future"].done():
            pending["future"].set_result({"cancelled": True, "message_id": pending["message_id"]})
        _CANCEL_TOMBSTONES[key] = {"status": "cancelled", "expires": time.monotonic() + _CANCEL_TTL}
        _PENDING_TURNS.pop(key, None)
        pending["task"].cancel()
        return {"status": "cancelled"}
    _CANCEL_TOMBSTONES[key] = {"status": "pending", "expires": time.monotonic() + _CANCEL_TTL}
    return {"status": "pending"}


@router.post("/{session_id}/interrupt")
async def interrupt_session_turn(session_id: str, req: InterruptRequest):
    """Stop the turn in flight on this session without closing it."""
    return await _interrupt(session_id, req.requested_by)


async def _interrupt(session_id: str, requested_by: Optional[str]) -> Dict[str, Any]:
    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        backend = backends.resolve_backend(sess["model"])
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=f"backend resolve: {e}")
    out = await backend.interrupt(session_id, requested_by)
    return {"session_id": session_id, **out}


@router.get("/{session_id}/activity")
async def get_session_activity(session_id: str):
    """Live turn status for a specific session.

    Returns {in_progress, elapsed, action, action_elapsed} while a CLI turn is
    running. Returns {in_progress: false} otherwise.
    """
    from backends.cli_claude import get_turn_activity
    return get_turn_activity(session_id)


@router.get("/{session_id}/info")
async def get_session_info(session_id: str):
    """Get session metadata + usage rollup."""
    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    return _serialize_session(sess)


@router.get("/{session_id}/messages")
async def get_session_messages(session_id: str):
    """Get full session history (paginated read in future v0.x)."""
    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    history = session_service.get_history(session_id)
    return {
        "session_id": session_id,
        "count": len(history),
        "messages": [_serialize_message(m) for m in history],
    }


def _agent_stopped(sess: Dict[str, Any]) -> Optional[str]:
    """The slug of the session's agent when that agent is disabled, else None.

    A disabled agent runs no turn anywhere: not from the runner, which never
    wakes it, and not from a chat, which is what this check closes. Before,
    "disabled" kept the agent from being woken while a message in an open
    chat still started its process. A store that cannot be read is not the
    agent's fault: the turn runs, and the warning says why.
    """
    agent_id = sess.get("agent_id")
    if not agent_id:
        return None
    try:
        state = session_service.agent_state(agent_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent state unreadable for agent_id=%s: %s", agent_id, e)
        return None
    if state and not state["enabled"]:
        return state["slug"]
    return None


@router.post("/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest):
    """Append a message and get backend response.

    Backend dispatch resolved by session.model prefix (resolve_backend).
    Stage 2d: only openrouter and claude-cli stub registered. Other backends
    raise ValueError -> 400.
    """
    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if sess["status"] not in ("active", "streaming", "idle_released"):
        if sess["status"] in _TERMINAL_STATUSES:
            # Structured, because the caller can recover from this without
            # telling anyone off: the session is final, so the same text belongs
            # in a new one. A generic 409 here used to end as a dropped message
            # on brin's side.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "session_reset",
                    "reason": f"session is {sess['status']}; cannot send",
                    "session_id": session_id,
                },
            )
        raise HTTPException(
            status_code=409,
            detail=f"session is {sess['status']}; cannot send",
        )
    stopped = _agent_stopped(sess)
    if stopped:
        # Structured like session_reset: the caller tells the person why,
        # instead of logging a bare 409 and dropping the message.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "agent_stopped",
                "reason": f"agent {stopped} is stopped; enable it to continue",
                "agent": stopped,
                "session_id": session_id,
            },
        )

    desk_id = _desk_from_session(sess) if req.client_turn_id else None
    client_turn_id = str(req.client_turn_id) if req.client_turn_id else None
    if client_turn_id and req.role != "user":
        raise HTTPException(status_code=400, detail="client_turn_id requires a user message")
    if client_turn_id and not desk_id:
        raise HTTPException(status_code=400, detail="client_turn_id requires a desk session")
    key = (desk_id, client_turn_id) if client_turn_id else None
    if key:
        _prune_cancel_tombstones()
        if key in _PENDING_TURNS:
            raise HTTPException(status_code=409, detail="duplicate client_turn_id")
        cancelled = _CANCEL_TOMBSTONES.get(key)
        if cancelled and cancelled["status"] in ("pending", "cancelled"):
            cancelled["status"] = "cancelled"
            return {"cancelled": True}

    # Stage 6: pre-send budget check. Blocks immediately if any HARD-limit
    # budget for tenant/agent/metadata-key scope is exhausted. SOFT budgets
    # surface in response but do not block.
    allowed, reason, budget_status = budget.check_budget(
        tenant_id=int(sess["tenant_id"]),
        agent_id=sess.get("agent_id"),
        metadata=sess.get("metadata") or {},
    )
    if not allowed:
        # core-api locks each budget, queues one owner email and
        # only then stamps notified_at.  Await the acceptance: launching this as
        # a background task would return the 402 before knowing whether the
        # period may honestly be marked notified.
        exhausted = [b for b in budget_status if b.get("over") and b.get("hard")]
        for b in exhausted:
            await _send_budget_exhaustion_notification(b["id"])

        # include reset_at, limit, spent in the 402 body so
        # brin can tell the person the real reason rather than the generic fallback.
        first = exhausted[0] if exhausted else {}
        raise HTTPException(
            status_code=402,
            detail={
                "error": "budget_exhausted",
                "reason": reason,
                "budgets": budget_status,
                "reset_at": first.get("reset_at"),
                "limit": first.get("limit_usd"),
                "spent": first.get("current_spend_usd"),
            },
        )

    # Resolve before making the inbound message durable or claiming the turn.
    try:
        backend = backends.resolve_backend(sess["model"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"backend resolve: {e}")
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=f"backend not yet implemented: {e}")

    # Append the inbound message. For user turns, brin may supply raw_content
    # (the text the person actually typed) separately from content (the enriched
    # form with caller identity, cross-channel history, memory context prepended).
    # We store raw_content so the desk history shows what was typed; we pass
    # content (enriched) to the backend so the model has full context.
    stored_content = req.raw_content if req.raw_content is not None else req.content
    user_msg_id = session_service.append_message(
        session_id, role=req.role, content=stored_content, tool_call_id=req.tool_call_id,
    )

    # Run the backend turn as an independent asyncio Task so the CLI process
    # continues to completion even when the originating HTTP request times out
    # or the client disconnects. asyncio.shield() prevents the task
    # from being cancelled by the outer await being cancelled. The task takes the
    # session's turn claim itself, so a second caller queues behind this one
    # instead of being refused.
    result_future: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
    pending = None
    if key:
        conversation = str((sess.get("metadata") or {}).get("conversation") or "")
        agent = conversation.split(":desk:", 1)[0] if ":desk:" in conversation else None
        pending = {"desk_id": desk_id, "client_turn_id": client_turn_id,
                   "session_id": session_id, "message_id": user_msg_id,
                   "agent": agent, "future": result_future,
                   "state": "queued", "counted": False}
        _PENDING_TURNS[key] = pending
    turn_task: asyncio.Task = asyncio.create_task(
        _run_queued_turn_async(
            session_id, backend, req.content, user_msg_id, result_future, pending,
        )
    )
    if pending:
        pending["task"] = turn_task
    # The task outlives this request whenever the sync window expires, and it
    # keeps running after the answer is published to do the handoff. Nothing
    # else retrieves its exception, so log it here in every case.
    turn_task.add_done_callback(_log_turn_task_error)

    try:
        # Wait for the answer, but only up to the sync timeout. asyncio.shield()
        # here means: if wait_for's deadline fires, it cancels the shield wrapper
        # future but NOT turn_task itself. The turn_task keeps running and will
        # write its result to sn_core.ai_session_messages when the CLI finishes.
        # The desk history query reads from there.
        result = await asyncio.wait_for(
            asyncio.shield(result_future), timeout=_sync_wait(req.wait_seconds)
        )
        return result

    except SessionRetiredError as e:
        # This turn queued behind one that handed the session off. Structured so
        # brin can do the only correct thing: send the same text to a clean
        # session instead of reporting a failure to the person.
        logger.info(
            "send_message session=%s: session retired (%s) while the turn queued",
            session_id, e,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "session_reset",
                "reason": f"session is {e}; its context was handed off",
                "session_id": session_id,
            },
        )

    except asyncio.TimeoutError:
        # The CLI is still thinking. turn_task continues in the background.
        logger.info(
            "send_message session=%s: sync window (%.0fs) elapsed; "
            "turn continues as background task -- response will arrive in session history",
            session_id, _SYNC_RESPONSE_TIMEOUT,
        )
        return JSONResponse(
            status_code=202,
            content={
                "status": "working",
                "session_id": session_id,
                "user_message_id": user_msg_id,
                "message": (
                    "The agent is still working. "
                    "The response will appear in the session history."
                ),
            },
        )

    except Exception as e:
        # turn_task raised before the timeout. The exception was retrieved by
        # the await (via shield), so asyncio will not warn about it. Convert
        # to an appropriate HTTP error for the synchronous caller.
        if isinstance(e, SpawnCapReached):
            # The session was NOT failed (_complete_turn_async). Structured so
            # the caller can wait and send the same message again.
            raise HTTPException(
                status_code=503,
                headers={"Retry-After": "10"},
                detail={
                    "error": "spawn_cap",
                    "reason": "the node is at its agent process cap; the session is "
                              "intact, send the message again shortly",
                    "session_id": session_id,
                },
            )
        if isinstance(e, model_door.Refused):
            # The session was NOT failed. Structured like agent_stopped and
            # budget_exhausted at the start of a turn.
            status, detail = e.http_error()
            raise HTTPException(status_code=status, detail={**detail, "session_id": session_id})
        msg = str(e)
        transient = (
            "another coroutine is already waiting" in msg
            or "readline timeout" in msg
            or "readuntil() called" in msg
        )
        if transient:
            # session_service.set_status was NOT called (transient path in
            # _complete_turn_async). The session stays active and resumable.
            raise HTTPException(status_code=409, detail=f"session busy: {msg}")
        # session_service.set_status("failed") was already called by _complete_turn_async.
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")


@router.post("/{session_id}/restart")
async def restart_session(session_id: str):
    """Restart an opaque (CLI) session's process in place, reloading its .claude
    config (CLAUDE.md, .mcp.json, settings) and system prompt while preserving
    the conversation via --resume. No-op for stateless backends.

    Returns 409 if the session is in a terminal state (completed, cancelled,
    reaped). Terminal sessions cannot be restarted; create a new session instead.
    Returning 200 here and then 409 on the first message is worse than refusing
    clearly upfront.
    """
    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if sess["status"] in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"session is {sess['status']} and cannot be restarted; create a new session",
        )
    backend = backends.resolve_backend(sess["model"])
    if not getattr(backend, "is_opaque", False) or not hasattr(backend, "restart"):
        return {"session_id": session_id, "status": "noop",
                "reason": "stateless backend, nothing to restart"}
    try:
        result = await backend.restart(session_id)
    except Exception as e:
        logger.exception("restart failed session=%s: %s", session_id, e)
        raise HTTPException(status_code=502, detail=f"restart error: {e}")

    # A successfully resumed process can serve messages again. If the session
    # was left in a non-deliberate dead-end (`failed` from a crashed turn, or
    # `idle_released` to save resources), reactivate it so the next send is
    # accepted instead of 409'd. This is what makes restart a real recovery:
    # the on-disk transcript is reloaded via --resume AND the session becomes
    # sendable, so brin/Studio can keep the same conversation alive.
    if sess["status"] in ("failed", "idle_released"):
        session_service.set_status(session_id, "active")
        result["reactivated_from"] = sess["status"]
    return result


@router.post("/{session_id}/clean")
async def clean_session_context(session_id: str, req: ManualCleanRequest):
    """Close an idle current session so the next turn starts without its transcript.

    This is distinct from ``restart`` (which resumes the same provider context)
    and from ``DELETE .../v2`` (the stop button).  It records a dedicated
    ``session.context_cleared`` event and intentionally creates no handoff doc.
    """
    lock = _turn_lock(session_id)
    if lock.locked() or _queued_turns(session_id):
        raise HTTPException(status_code=409, detail={
            "error": "session_busy",
            "reason": "wait for the active turn to finish before starting clean",
            "session_id": session_id,
        })
    try:
        async with lock:
            sess = session_service.get_session(session_id)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            if sess["status"] not in ("active", "streaming", "idle_released"):
                raise HTTPException(status_code=409, detail={
                    "error": "session_not_active",
                    "reason": f"session is {sess['status']}",
                    "session_id": session_id,
                })
            result = context_handoff.finalize_manual_clean(
                session_id, sess, req.requested_by, req.reason,
            )
            try:
                backend = backends.resolve_backend(sess["model"])
                await backend.close(session_id)
            except Exception as exc:
                logger.exception("backend close after manual clean failed session=%s", session_id)
                try:
                    context_handoff.emit_event(
                        "session.context_clear_cleanup_failed", session_id,
                        {"error": str(exc)[:500], "requested_by": req.requested_by},
                    )
                except Exception:
                    logger.exception("could not audit manual-clean cleanup failure session=%s", session_id)
            return result
    finally:
        _release_turn_lock(session_id)


@router.delete("/{session_id}/v2")
async def terminate_session(session_id: str):
    """Mark session completed. Backend close called for runtime cleanup."""
    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if sess["status"] in _TERMINAL_STATUSES or sess["status"] == "failed":
        return {"session_id": session_id, "status": sess["status"], "note": "already terminal"}

    # Best-effort backend cleanup
    try:
        backend = backends.resolve_backend(sess["model"])
        await backend.close(session_id)
    except Exception as e:
        logger.warning("backend close failed (continuing): %s", e)

    session_service.close_session(session_id)
    return {"session_id": session_id, "status": "completed"}
