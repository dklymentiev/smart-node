"""Brin: unified agent routing API."""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

from core.logging_config import setup_logging, CorrelationMiddleware
from core.auth import require_api_key
from core.health import get_health
from core.specialist_loader import SpecialistLoader
from core.router import Router
from core.executor import Executor, TurnArtifacts, turn_withholds, GatewayWorkingError, BudgetExhaustedError, TurnStoppedError, QueuedTurnCancelledError, AgentStoppedError, SessionBusyDeferredError
from core.turn_budget import refresh_turn_ceiling, turn_ceiling
from core.service_proof import service_headers as _service_headers
from models.actions import DirectAgent, ReinWorkflow
from models.inbox import IncomingMessage, InboxSaveResult
from models.media import (
    VisionRequest, VisionResult,
    TranscribeRequest, TranscribeResult,
    GenerateRequest, GenerateResult,
    DownloadRequest, DownloadResult,
)
from services.inbox_service import InboxService
from services.media_service import MediaService
from services.intent_service import IntentService
from services.notification_service import NotificationService
from models.intent import ClassifyRequest, ClassifyResult, SearchParseRequest, SearchParseResult, ModelParseRequest, ModelParseResult, SessionParseResult
from models.notification import NotificationRequest, NotificationResult
from models.diagnostics import (
    DiagnosticRequest, DiagnosticResult,
    AsyncDiagnosticRequest, AsyncDiagnosticResult,
    DiagnosticTaskStatus,
)
from services.diagnostics_service import DiagnosticsService
from services.model_service import ModelService
from services.process_service import ProcessService
from services.memory_service import MemoryService
from services.session_service import SessionService
from core.tools import ToolsConfig
from models.model_catalog import (
    ModelListResult, AddModelRequest, SetUserModelRequest, SetModelResult,
    UserSettingsResult, UsageLogEntry, UsageStatsResult,
)
from models.process import (
    RegisterProcessRequest, RegisterProcessResult,
    ProcessActionResult, ProcessListResult, UpdateProgressRequest,
    ProcessLogEntry as ProcessLogEntryModel,
)
from models.memory import SmartSearchRequest, SmartSearchResult, MemoryContextResult
from models.session import CreateSessionRequest, CreateSessionResponse, SharedSession

# v2 imports
from services.stm_service import ShortTermMemory
from core.webchat_store import WebchatStore
from core.agent_registry import AgentRegistry
from core.session_history import SessionHistory
from core.session_summary import SessionSummary
from services.event_store import EventStore
from services.event_bus import EventBus
from services.message_watcher import MessageWatcher
from services.task_watcher import TaskWatcher
from core.fast_tier import try_fast_path
from core.async_runner import launch as launch_async
from models.event import EmitEventRequest, EventListResult

# --- Logging (before anything else) ---
setup_logging()

# --- Globals ---
loader = SpecialistLoader()
intent_service = IntentService()
router = Router(loader, intent_service=intent_service)
tools_config = ToolsConfig()
memory_service = MemoryService()
executor = Executor(loader, tools_config=tools_config, memory_service=memory_service)
inbox_service = InboxService()
media_service = MediaService()
notification_service = NotificationService()
diagnostics_service = DiagnosticsService()
model_service = ModelService()
session_service = SessionService()

# --- EventBus setup ---
event_store = EventStore()
event_bus = EventBus(event_store)
message_watcher = MessageWatcher()
task_watcher = TaskWatcher()
process_service = ProcessService(event_bus=event_bus)

# --- v2: Universal Agent setup ---
# (hardcoded-tools Agent(ToolRegistry) + core/v2tools retired in Phase 2;
#  tools are now DB-catalog driven via core/catalog_tools on the live path.)
webchat_store = WebchatStore()
agent_registry = AgentRegistry(loader)
session_history = SessionHistory()
session_summary = SessionSummary()
stm_service = ShortTermMemory()

logger = logging.getLogger("brin.main")

# What a live person sees when the turn failed. Deliberately says nothing
# about why: the executor now raises on an upstream failure instead of returning
# the error as the answer, and the reason belongs in the log and on the message
# row, never in something a customer can read. Kept short and free of machinery.
#
# KNOWN GAP, recorded rather than hidden: this is one fixed English sentence,
# while an agent otherwise answers in the language it was written to. A failure
# reply in the wrong language is a worse experience than a good one and a far
# better one than leaking a stack trace, so it ships as is; giving it the turn's
# language is a follow-up.
_COULD_NOT_ANSWER = "I could not answer just now. Nothing was sent on your behalf."
# Returned (HTTP 202) when the gateway's sync window expired and the turn is
# still running as a background task. Not a failure: the answer will arrive
# in session history.
_STILL_WORKING = (
    "The agent is still working on your request. "
    "The response will appear in the session history shortly."
)


async def _notify_on_failure(event: dict):
    """Send Telegram notification when a process or Rein task fails."""
    payload = event.get("payload", {})
    event_type = event.get("event_type", "")
    source = event.get("source", "")

    if event_type == "process.failed":
        name = payload.get("name", payload.get("process_id", "unknown"))
        error = payload.get("error", "no details")
        text = f"[PROCESS FAILED] {name}: {error}"
        task_id = payload.get("task_id", "unknown")
        flow = payload.get("flow", "")
        text = f"[REIN TASK FAILED] {task_id} (flow={flow})"
    else:
        text = f"[FAILURE] {event_type}: {json.dumps(payload)[:200]}"

    try:
        from models.notification import NotificationChannel, NotificationRequest
        req = NotificationRequest(text=text, channel=NotificationChannel.telegram)
        await notification_service.send(req)
    except Exception as e:
        logger.error("Failed to send failure notification: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loader.reload()
    # Start EventBus and ReinWatcher
    await event_bus.startup()
    await message_watcher.start()
    # It reads the planner: with no planner module it failed every 15 seconds.
    if os.path.exists("/etc/smart-node/companions.d/tracking.json"):
        await task_watcher.start()
    event_bus.subscribe("process.failed", _notify_on_failure)
    yield
    # Graceful shutdown
    await task_watcher.stop()
    await message_watcher.stop()
    await event_bus.shutdown()
    await memory_service.close()
    await stm_service.close()


app = FastAPI(
    title="Brin",
    description="Unified agent routing: specialists + Rein workflows",
    version="0.2.0",
    lifespan=lifespan,
)

# Correlation ID middleware
app.add_middleware(CorrelationMiddleware)


# --- Request models ---

class MessageRequest(BaseModel):
    message: str
    context: Optional[dict] = None
    # Default = the node's OWN agent CLI. It was "openrouter/anthropic/claude-3-haiku",
    # a cloud model: the panel chat sends no model, so every message on a node without an
    # OpenRouter key came back 502 and the chat page sat in "reconnecting" forever. A node
    # is born with an OAuth token for its CLI and NO cloud key -- so the cloud must never
    # be the default. Operators can still pass any "<upstream>/<model-id>" the gateway
    # knows (old cli aliases sonnet/haiku/opus still work via its compat shim);
    # BRIN_DEFAULT_MODEL overrides this default.
    model: Optional[str] = os.environ.get("BRIN_DEFAULT_MODEL", "claude-cli/sonnet")
    stream: Optional[bool] = False
    channel: Optional[str] = None  # "telegram", "email", "web"
    agent: Optional[str] = None    # per-request DB agent slug (overrides BRIN_DEFAULT_AGENT)
    client_turn_id: Optional[uuid.UUID] = None
    # How long the gateway holds this turn before 202 "still working"; None is
    # its default window. fast-dispatch asks for the whole turn: a chat has
    # nowhere to put an answer that arrives after the request is gone.
    wait_seconds: Optional[float] = None


class WorkflowStatusRequest(BaseModel):
    task_id: str


# --- Public endpoints (no auth) ---

@app.get("/health")
async def public_health():
    """Public health check with dependency status."""
    return await get_health(len(loader.list()), len(router.available_flows()))


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get shared session by ID (public, no auth -- for webchat JS)."""
    session = session_service.get(session_id)
    if not session:
        return JSONResponse({"error": "Session not found or expired"}, status_code=404)
    return session.model_dump()


# --- Authenticated API router ---

api_router = APIRouter(
    prefix="/api",
    dependencies=[Depends(require_api_key)],
)


def _parse_agent_command(message: str) -> Optional[dict]:
    """Parse /agent commands. Returns dict with action or None."""
    text = message.strip()
    if not text.startswith("/agent"):
        return None
    parts = text.split(None, 2)
    if len(parts) == 1:
        return {"action": "status"}
    subcmd = parts[1].lower()
    if subcmd in ("list", "ls"):
        return {"action": "list"}
    if subcmd in ("off", "stop", "exit", "quit", "deactivate"):
        return {"action": "deactivate"}
    if subcmd in ("status", "info"):
        return {"action": "status"}
    # /agent <name>
    return {"action": "activate", "name": parts[1]}


async def _handle_agent_command(cmd: dict, user_id: str, channel: str) -> dict:
    """Execute agent command and return response dict."""
    action = cmd["action"]

    if action == "list":
        agents = agent_registry.list_agents()
        active = agent_registry.get_active(user_id)
        lines = ["Available agents:"]
        for a in agents:
            marker = " [active]" if active and active.name == a.name else ""
            lines.append(f"  - {a.name} (workspace: {a.workspace}){marker}")
        if not agents:
            lines = ["No agents configured."]
        return {"content": "\n".join(lines), "specialist": "_system"}

    if action == "activate":
        name = cmd["name"]
        config = agent_registry.activate(user_id, name)
        if not config:
            return {"content": f"Agent '{name}' not found.", "specialist": "_system"}
        session_history.get_or_start_session(
            config.name, config.history_dir,
            user_id=user_id, channel=channel,
        )
        return {
            "content": f"Agent '{config.name}' activated. Workspace: {config.workspace}",
            "specialist": "_system",
        }

    if action == "deactivate":
        config = agent_registry.get_active(user_id)
        if not config:
            return {"content": "No agent active.", "specialist": "_system"}

        summary_msg = ""
        if config.session_summary:
            session_data = session_history.end_session(config.name, user_id)
            if session_data:
                import asyncio
                asyncio.create_task(
                    session_summary.summarize_and_store(
                        session_data, config.workspace, config.name,
                    )
                )
                summary_msg = " Session summary queued."

        agent_registry.deactivate(user_id)
        return {
            "content": f"Agent '{config.name}' deactivated.{summary_msg}",
            "specialist": "_system",
        }

    if action == "status":
        config = agent_registry.get_active(user_id)
        if not config:
            return {"content": "No agent active. Use /agent list to see available.", "specialist": "_system"}
        return {
            "content": f"Active agent: {config.name}\nWorkspace: {config.workspace}",
            "specialist": "_system",
        }

    return {"content": "Unknown command.", "specialist": "_system"}


_CORE_API_URL = (os.environ.get("SN_CORE_API_URL") or os.environ.get("CORE_API_URL") or "")
if not _CORE_API_URL:
    raise RuntimeError(
        "SN_CORE_API_URL is not set. "
        "The node installer should write this to the service environment."
    )


async def _persist_webchat_turn(ctx, agent_name, user_msg, assistant_msg):
    """Mirror a browser web-chat exchange into the unified message registry
    (sn_core.messages) via core-api, so the one table holds every channel and a
    person is recalled across channels by identity, the same contract voice gets
    through record-voice.

    Best-effort and silent on failure: a live person is waiting on this turn,
    so persistence must never break it. Only DESK (browser web-chat) turns
    qualify -- the Studio sends `user_id='desk:<id>'` and no channel; telegram
    and email arrive with their own channel and are recorded by the
    communication component, so gating on the desk marker keeps this from
    double-recording them or from catching the agent's own inference calls."""
    try:
        import httpx  # local: brin imports httpx per-callsite, not at module top
        uid = str((ctx or {}).get("user_id") or "")
        if not uid.startswith("desk:"):
            return
        if not (user_msg or "").strip() and not (assistant_msg or "").strip():
            return
        turns = []
        if (user_msg or "").strip():
            turns.append({"role": "user", "text": user_msg})
        if (assistant_msg or "").strip():
            turns.append({"role": "assistant", "text": assistant_msg})
        payload = {
            "identity": (ctx or {}).get("studio_user") or "owner",
            "turns": turns,
            "session_id": uid.split(":", 1)[1],
            "agent": agent_name,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{_CORE_API_URL}/api/messages/record-webchat",
                headers=_service_headers(), json=payload)
    except Exception as e:  # noqa: BLE001 -- persistence must never break chat
        logger.warning(f"web-chat registry persistence failed (ignored): {e}")


async def _desk_runtime(agent_slug: str, action: str, ref: str | None = None,
                        lease_seconds: int | None = None) -> dict:
    """Best-effort: update agent_runtime for a desk turn. Never raises.

    Uses the same claim/release mechanism as the runner so the runner sees the
    agent as busy during a live desk conversation and does not double-launch.
    action must be claim|release (broken is not used for desk turns)."""
    try:
        import httpx  # local: per-callsite, same convention as _persist_webchat_turn
        body: dict = {"action": action}
        if ref is not None:
            body["ref"] = ref
        if lease_seconds is not None:
            body["lease_seconds"] = lease_seconds
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{_CORE_API_URL}/api/agents/{agent_slug}/runtime",
                headers=_service_headers(),
                json=body,
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning("desk runtime %s for %s failed (ignored): %s", action, agent_slug, e)
    return {}


# A desk turn holds the agent for as long as the gateway lets a turn run, plus a
# margin. It used to take core-api's 300 s default, so any turn longer
# than five minutes showed its agent as free, or as "stale", while it worked.
def _desk_lease_seconds() -> int:
    return int(turn_ceiling()) + 60

# Desk turns of each agent running in this process, and the agents whose lease
# one of them holds. A second message to a busy agent runs anyway, queued
# behind the first in the gateway, but its claim fails because the first holds
# the agent. core-api's release frees whoever holds it, so the first turn to end
# used to free the agent while the queued one was still working. The lease now
# goes back when the LAST desk turn of that agent here ends. Turns are kept by id
# with their start, and an entry older than the lease is dropped: a turn that
# died on an exception nobody caught must not hold the agent for ever, and past
# the lease the gateway's own turn cap has ended it anyway.
_DESK_TURNS_IN_FLIGHT: dict[str, dict[str, float]] = {}
_DESK_LEASE_HELD: set[str] = set()

# Stops requested per desk, {desk_id: (who, when)}. A turn that outlived
# the gateway's sync window ends in the background; its watcher reads this to
# say whether that end was a stop.
_DESK_STOPS: dict[str, tuple[str | None, float]] = {}


def _desk_turns_live(agent_slug: str) -> dict[str, float]:
    turns = _DESK_TURNS_IN_FLIGHT.setdefault(agent_slug, {})
    horizon = time.monotonic() - _desk_lease_seconds()
    for tid in [t for t, started in turns.items() if started < horizon]:
        turns.pop(tid, None)
    return turns


async def _desk_turn_begin(agent_slug: str, ref: str, turn_id: str) -> bool:
    """Count a desk turn in and hold the agent for it. Returns whether THIS turn
    took the lease (the value turn_started has always reported as `claimed`)."""
    _desk_turns_live(agent_slug)[turn_id] = time.monotonic()
    if agent_slug in _DESK_LEASE_HELD:
        # Already ours, from a turn still running. Queued turns run one after
        # another, so the lease has to cover the new one too: renew it. A claim
        # on a busy row is refused, hence release and claim; the gap is one
        # round trip, and a runner that took the agent in it simply holds it.
        await _desk_runtime(agent_slug, "release")
        _DESK_LEASE_HELD.discard(agent_slug)
    res = await _desk_runtime(agent_slug, "claim", ref=ref, lease_seconds=_desk_lease_seconds())
    claimed = res.get("claimed") is True
    if claimed:
        _DESK_LEASE_HELD.add(agent_slug)
    return claimed


async def _desk_turn_end(agent_slug: str, turn_id: str) -> None:
    """Count a desk turn out; release the agent when it was the last one."""
    turns = _desk_turns_live(agent_slug)
    turns.pop(turn_id, None)
    if turns:
        return
    _DESK_TURNS_IN_FLIGHT.pop(agent_slug, None)
    if agent_slug in _DESK_LEASE_HELD:
        _DESK_LEASE_HELD.discard(agent_slug)
        await _desk_runtime(agent_slug, "release")


async def _gateway_desks_in_progress() -> list | None:
    """The gateway's list of desks with a turn running, or None if unreadable."""
    import httpx
    from core.executor import GATEWAY_URL
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{GATEWAY_URL}/api/v1/sessions/desks-in-progress")
            if r.status_code == 200:
                return (r.json() or {}).get("desks") or []
    except Exception as e:
        logger.warning("desks-in-progress read failed (non-fatal): %s", e)
    return None


async def _watch_working_turn(agent_slug: str, desk_id: str, turn_id: str,
                              started_at: float) -> None:
    """A turn that outlived the sync window (202) ends in the background.

    It used to release the agent on the spot and never write an end, so the agent
    read free while it worked and the log held a turn that never finished. This
    waits for the gateway to say the desk is idle, then writes the end and lets the
    lease go. Bounded by the lease: past it, the turn cap has already ended it.
    """
    deadline = time.monotonic() + _desk_lease_seconds()
    misses = 0
    ended = False
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            desks = await _gateway_desks_in_progress()
            if desks is None:
                misses += 1
                if misses < 6:
                    continue
                break
            misses = 0
            if not any(d.get("desk_id") == desk_id for d in desks):
                break
        # The agent is released before the end is said: a page reads who is at work
        # the moment it hears it, and read first it kept "working" on screen with no
        # later event to correct it (tester, 2026-10-30).
        await _desk_turn_end(agent_slug, turn_id)
        ended = True
        stop = _DESK_STOPS.get(desk_id)
        if stop and stop[1] >= started_at:
            await _desk_event(agent_slug, "agent.turn_stopped", desk_id,
                              {"via": "desk", "turn_id": turn_id, "stopped_by": stop[0],
                               "after_working": True})
        else:
            await _desk_event(agent_slug, "agent.turn_completed", desk_id,
                              {"via": "desk", "turn_id": turn_id, "after_working": True})
    finally:
        if not ended:
            await _desk_turn_end(agent_slug, turn_id)


async def _desk_event(
    agent_slug: str,
    event_type: str,
    desk_id: str,
    payload: dict | None = None,
    actor: str | None = None,
) -> None:
    """Best-effort: emit a sn_core.events row for a desk turn. Never raises.

    Fills the event-audit gap: desk conversations were invisible to any reader
    that checks sn_core.events (dashboards, digests, budget accounting). Using
    actor=agent:<slug> matches what the runner emits for wakes on other channels."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{_CORE_API_URL}/api/events",
                headers=_service_headers(),
                json={
                    "actor": actor or f"agent:{agent_slug}",
                    "event_type": event_type,
                    "entity_type": "desk",
                    "entity_id": desk_id,
                    "payload": payload or {},
                },
            )
    except Exception as e:
        logger.warning("desk event %s for %s failed (ignored): %s", event_type, agent_slug, e)


async def _desk_ensure(
    desk_id: str,
    agent_slug: str | None,
    studio_user: str | None,
    title: str | None = None,
) -> None:
    """Best-effort: ensure sn_core.desks has a row for this desk UUID. Never raises.

    Conversations that pre-date the UI desk-create path exist on the bus but
    have no desks row, so the chat list does not show them. This call creates
    the row if it is missing (ON CONFLICT DO NOTHING). Idempotent and silent
    on failure: a live person is waiting on the turn."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.put(
                f"{_CORE_API_URL}/api/desks/{desk_id}/ensure",
                headers=_service_headers(),
                json={
                    "studio_user": studio_user or "owner",
                    "agent_slug": agent_slug,
                    "title": (title or "")[:200] or None,
                },
            )
    except Exception as e:
        logger.warning("desk ensure %s failed (ignored): %s", desk_id, e)


@api_router.post("/message")
async def send_message(req: MessageRequest):
    """Route a message to the appropriate specialist or workflow."""
    ctx = req.context or {}
    client_turn_id = getattr(req, "client_turn_id", None)
    if req.channel:
        ctx["channel"] = req.channel

    user_id = ctx.get("user_id", "default")

    # Pre-route: handle /agent commands
    agent_cmd = _parse_agent_command(req.message)
    if agent_cmd:
        result = await _handle_agent_command(agent_cmd, user_id, req.channel or "web")
        return JSONResponse(result)

    # Check for active agent and ensure session history
    active_agent = agent_registry.get_active(user_id)
    if active_agent:
        session_history.get_or_start_session(
            active_agent.name, active_agent.history_dir,
            user_id=user_id, channel=req.channel or "web",
        )
        # Log user message
        session_history.append(active_agent.name, user_id, "user", req.message,
                               client_turn_id=str(client_turn_id) if client_turn_id else None)

    # Snapshot the largest registered ceiling once for this turn. All lease and
    # gateway waits below use the cached value, even during a core-api restart.
    await refresh_turn_ceiling()

    # Desk turn observability.
    # Desk turns bypass the runner entirely, leaving agent_runtime.status='free'
    # during a live conversation and sn_core.events with no record of the turn.
    # Fix: claim the runtime slot at turn start (prevents runner from double-
    # launching) and emit audit events matching what the runner emits for wakes
    # on other channels.  Best-effort at every step: if claim or emit fails the
    # turn still runs unaffected (a live person is waiting).
    _is_desk = str(user_id).startswith("desk:")
    _desk_agent = (req.agent or os.environ.get("BRIN_DEFAULT_AGENT")) if _is_desk else None
    _desk_id = str(user_id).split(":", 1)[1] if _is_desk else None
    _desk_claimed = False
    _turn_id = None
    if _desk_id and _desk_agent:
        # Ensure a sn_core.desks row exists for this conversation.
        # Conversations that pre-date the UI desk-create path have no row and
        # do not appear in the chat list. Best-effort: never fails the turn.
        await _desk_ensure(
            _desk_id,
            _desk_agent,
            ctx.get("studio_user"),
            title=req.message[:200] if req.message else None,
        )
        _desk_ref = f"desk-{_desk_id}"
        # One id for this turn, carried by its start and by whichever end it
        # reaches. The desk ref names the conversation, which outlives every
        # turn in it, so two turns in one desk cannot be told apart by it.
        _turn_id = uuid.uuid4().hex
        _turn_started_at = time.time()
        _desk_claimed = await _desk_turn_begin(_desk_agent, _desk_ref, _turn_id)
        await _desk_event(
            _desk_agent, "agent.turn_started", _desk_id,
            {"via": "desk", "turn_id": _turn_id, "ref": _desk_ref, "channel": "web-chat",
             "claimed": _desk_claimed},
        )

    # the fast tier: a deterministic read (v1: check_messages on telegram)
    # answered straight from the bus -- no router, no gateway, no LLM loop. The
    # classifier (not a keyword gate) decides; try_fast_path returns None for
    # everything else, so the normal path is unaffected.
    fast_agent = req.agent or os.environ.get("BRIN_DEFAULT_AGENT")
    fast = await try_fast_path(req.message, req.channel or "web", fast_agent,
                               getattr(router, "intent_service", None))
    if fast is not None:
        if active_agent:
            session_history.append(active_agent.name, user_id, "assistant", fast["content"])
        await _persist_webchat_turn(ctx, req.agent, req.message, fast.get("content", ""))
        if _desk_id and _desk_agent:
            await _desk_turn_end(_desk_agent, _turn_id)
            await _desk_event(_desk_agent, "agent.turn_completed", _desk_id,
                              {"via": "desk", "turn_id": _turn_id, "fast_path": True})
        return JSONResponse(fast)

    action = await router.route(req.message, ctx)

    # Resolve workspace for agent-scoped memory search
    agent_workspace = active_agent.workspace if active_agent else None

    # Hand the executor WHO is talking so it can fold that
    # person's cross-channel history into the turn.
    #
    # It used to be desk turns only, on the reasoning that telegram and email
    # "arrive with a channel, not a Studio session" -- as though that settled
    # it. It does not: by-contact resolves ANY handle to one contact, and says so
    # in its own docstring ("an identity (a handle, an account email, or a
    # users.handle)"). So a node holding the owner's telegram handle, his voice
    # transcripts and his mail in one history answered him as a stranger.
    #
    # The identity was already in the request the whole time: dispatch.py passes
    # the sender as `principal` and two lines below it is forwarded to the
    # executor as exactly that. Falling back to it fixes every channel the node
    # talks to the world on, with no adapter change. A wake carries no sender of
    # its own, so the runner supplies one as person_identity.
    person_identity = (ctx.get("studio_user") if str(user_id).startswith("desk:")
                       else (ctx.get("person_identity") or ctx.get("principal")))

    # Tools one turn goes without, from the caller (the Runner's chat wake
    # withholds the inbox): they can only take away from what was granted.
    _wake_tools = turn_withholds(user_id, [t for t in (ctx.get("withhold_tools") or []) if isinstance(t, str)] or None)
    # A chat wake's messages: the gateway ends the turn once each is answered.
    _wake_answer = [i for i in (ctx.get("answer_ids") or []) if isinstance(i, int)] or None
    # The chat a chat wake works for: that chat's live line shows the work.
    _wake_desk = ctx.get("wake_desk") if isinstance(ctx.get("wake_desk"), str) else None

    if isinstance(action, DirectAgent):
        turn_artifacts = TurnArtifacts()
        if req.stream:
            async def generate():
                full = []
                try:
                    async for chunk in executor.run_direct(
                        action, model=req.model, stream=True, workspace=agent_workspace, agent_override=req.agent,
                        conversation_id=user_id, channel=req.channel,
                        principal=ctx.get("principal") or person_identity,
                        person_identity=person_identity,
                        artifacts=turn_artifacts,
                        client_turn_id=str(client_turn_id) if client_turn_id else None,
                        tools=_wake_tools, answer_ids=_wake_answer, wake_desk=_wake_desk,
                    ):
                        full.append(chunk)
                        yield chunk
                except Exception:
                    # The executor now RAISES on an upstream failure rather
                    # than returning the error text as the answer. A live person is
                    # waiting on this stream, so say one plain sentence and leave
                    # the detail in the log. Never render the exception: rendering
                    # it is how internal service names and the path to a secret
                    # reached an outside address on 2026-08-04.
                    logger.exception("run_direct failed on the streaming path")
                    if _desk_id and _desk_agent:
                        await _desk_turn_end(_desk_agent, _turn_id)
                        await _desk_event(_desk_agent, "agent.turn_failed", _desk_id,
                                          {"via": "desk", "turn_id": _turn_id, "stream": True, "error": True})
                    yield _COULD_NOT_ANSWER
                    return
                # Log assistant response after stream completes
                if active_agent:
                    session_history.append(
                        active_agent.name, user_id, "assistant", "".join(full),
                    )
                await _persist_webchat_turn(ctx, req.agent, req.message, "".join(full))
                if _desk_id and _desk_agent:
                    await _desk_turn_end(_desk_agent, _turn_id)
                    await _desk_event(_desk_agent, "agent.turn_completed", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id, "stream": True})
            return StreamingResponse(generate(), media_type="text/plain")
        else:
            content = ""
            try:
                async for chunk in executor.run_direct(
                    action, model=req.model, stream=False, workspace=agent_workspace, agent_override=req.agent,
                    conversation_id=user_id, channel=req.channel,
                    principal=ctx.get("principal") or person_identity,
                    person_identity=person_identity,
                    artifacts=turn_artifacts,
                    wait_seconds=req.wait_seconds,
                    client_turn_id=str(client_turn_id) if client_turn_id else None,
                    tools=_wake_tools, answer_ids=_wake_answer, wake_desk=_wake_desk,
                ):
                    content += chunk
            except QueuedTurnCancelledError:
                if active_agent and client_turn_id:
                    session_history.discard_client_turn(active_agent.name, user_id,
                                                        str(client_turn_id))
                if _desk_id and _desk_agent:
                    await _desk_turn_end(_desk_agent, _turn_id)
                    await _desk_event(_desk_agent, "agent.turn_cancelled", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id,
                                       "client_turn_id": str(client_turn_id)})
                return JSONResponse({"cancelled": True})
            except TurnStoppedError as e:
                # a person pressed Stop. The gateway already stored what
                # the agent had said, marked partial; here the turn ends as
                # stopped, not completed and not failed, and the person gets
                # the partial text back with who stopped it. Nothing is written
                # to the web-chat transcript: an interrupted exchange is not one
                # to replay into the next turn's context as if it finished.
                if _desk_id and _desk_agent:
                    await _desk_turn_end(_desk_agent, _turn_id)
                    await _desk_event(_desk_agent, "agent.turn_stopped", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id,
                                       "stopped_by": e.stopped_by})
                return JSONResponse({
                    "content": e.content,
                    "stopped": True,
                    "stopped_by": e.stopped_by,
                    "specialist": action.specialist_id,
                })
            except GatewayWorkingError as e:
                # the gateway's sync window expired; the turn is still
                # running as a background task and will write to session history
                # when done. Tell the person to wait. Do NOT log to session
                # history or webchat turn -- the gateway handles that when the
                # turn finishes. Do NOT emit turn_failed.
                working_msg = str(e) or _STILL_WORKING
                logger.info(
                    "Gateway working (202) for user_id=%s: returning working response",
                    user_id,
                )
                if _desk_id and _desk_agent:
                    await _desk_event(_desk_agent, "agent.turn_working", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id})
                    # The turn is still running. It keeps the agent, and its end
                    # is written when the gateway says it ended.
                    asyncio.create_task(_watch_working_turn(
                        _desk_agent, _desk_id, _turn_id, _turn_started_at))
                return JSONResponse(
                    {"content": working_msg, "working": True},
                    status_code=202)
            except AgentStoppedError as e:
                # the owner stopped this agent. Say so, by name: the agent is
                # disabled on purpose and answers nothing until enabled again.
                # Not _COULD_NOT_ANSWER, which would read as a fault.
                who = e.agent or (active_agent.name if active_agent else "this agent")
                logger.info("Agent %s is stopped; turn for user_id=%s refused", who, user_id)
                if _desk_id and _desk_agent:
                    await _desk_turn_end(_desk_agent, _turn_id)
                    await _desk_event(_desk_agent, "agent.turn_failed", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id, "error_type": "agent_stopped"})
                return JSONResponse({
                    "content": f"{who} is stopped by the owner. Enable it on the Agents page to continue.",
                    "failed": True,
                    "error_type": "agent_stopped",
                    "agent": e.agent,
                })
            except SessionBusyDeferredError as e:
                # Gateway 409 that is not session_reset and not agent_stopped:
                # the session has a turn in flight and this one is either
                # queued behind it on the gateway side or is a duplicate
                # client_turn_id already being handled. Treat it like the
                # 202 working-ack: the person's answer will land in session
                # history. Returning _COULD_NOT_ANSWER here is what painted
                # red "Nothing was sent on your behalf" bubbles alongside the
                # real answer in the same desk thread (2026-11-04). Do NOT
                # end the turn: the real turn keeps running on the gateway
                # and will emit agent.turn_completed when it finishes.
                logger.info(
                    "Session busy for user_id=%s session=%s: returning working response (reason: %s)",
                    user_id, e.session_id, e.reason,
                )
                if _desk_id and _desk_agent:
                    await _desk_event(_desk_agent, "agent.turn_working", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id,
                                       "deferred": True})
                return JSONResponse(
                    {"content": _STILL_WORKING, "working": True, "deferred": True},
                    status_code=202)
            except BudgetExhaustedError as e:
                # the node has hit its spending limit. Tell the
                # person the real reason and reset time. Do not use _COULD_NOT_ANSWER.
                # Return structured data so the runner can classify this as
                # BUDGET_EXHAUSTED (not a failed wake).
                period_label = e.period or "period"
                reset_label = e.reset_at or "the next reset time"
                budget_msg = (
                    f"The node has reached its spending limit for {period_label}. "
                    f"It resets at {reset_label}. "
                    f"No new turns will be processed until then."
                )
                logger.warning(
                    "Budget exhausted for user_id=%s (period=%s reset_at=%s)",
                    user_id, e.period, e.reset_at,
                )
                if _desk_id and _desk_agent:
                    await _desk_turn_end(_desk_agent, _turn_id)
                    await _desk_event(_desk_agent, "agent.turn_failed", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id, "error_type": "budget_exhausted"})
                return JSONResponse({
                    "content": budget_msg,
                    "failed": True,
                    "error_type": "budget_exhausted",
                    "reset_at": e.reset_at,
                    "limit": e.limit,
                    "spent": e.spent,
                    "period": e.period,
                }, status_code=200)
            except Exception:
                # the synchronous twin. Same rule: a failure is not an
                # answer, and what a person sees must carry none of the detail.
                logger.exception("run_direct failed on the synchronous path")
                if _desk_id and _desk_agent:
                    await _desk_turn_end(_desk_agent, _turn_id)
                    await _desk_event(_desk_agent, "agent.turn_failed", _desk_id,
                                      {"via": "desk", "turn_id": _turn_id, "error": True})
                return JSONResponse(
                    {"content": _COULD_NOT_ANSWER, "failed": True},
                    status_code=200)
            # Log assistant response
            if active_agent:
                session_history.append(
                    active_agent.name, user_id, "assistant", content,
                )
            await _persist_webchat_turn(ctx, req.agent, req.message, content)
            if _desk_id and _desk_agent:
                await _desk_turn_end(_desk_agent, _turn_id)
                await _desk_event(_desk_agent, "agent.turn_completed", _desk_id,
                                  {"via": "desk", "turn_id": _turn_id})
            response = {"content": content, "specialist": action.specialist_id}
            response["final_answer"] = turn_artifacts.final_answer
            if turn_artifacts.memory and turn_artifacts.memory.documents:
                response["memory"] = turn_artifacts.memory.model_dump()
            if turn_artifacts.actions:
                response["actions"] = turn_artifacts.actions
            return JSONResponse(response)

    elif isinstance(action, ReinWorkflow):
        # A desk turn that routes to a workflow started a turn and claimed the
        # agent above, and used to return here with neither ended: the event log
        # held a start with no end and the agent stayed claimed until the lease ran
        # out. Every exit of a started turn writes its end.
        try:
            result = await executor.run_workflow(action)
        except Exception:
            if _desk_id and _desk_agent:
                await _desk_turn_end(_desk_agent, _turn_id)
                await _desk_event(_desk_agent, "agent.turn_failed", _desk_id,
                                  {"via": "desk", "turn_id": _turn_id, "workflow": True, "error": True})
            raise
        if _desk_id and _desk_agent:
            await _desk_turn_end(_desk_agent, _turn_id)
            await _desk_event(_desk_agent, "agent.turn_completed", _desk_id,
                              {"via": "desk", "turn_id": _turn_id, "workflow": True})
        return JSONResponse(result)


@api_router.post("/message/async")
async def send_message_async(req: MessageRequest):
    """launch an agent action in the background and return a call_id
    immediately, instead of blocking for the full (15-150s) turn. The result lands in
    the DURABLE ledger (tool.succeeded/failed, tagged with the call_id) and surfaces in
    the next turn's [tool calls] section -- so it survives a disconnect or restart. This
    is what a channel front (voice, telegram) should call instead of holding an
    ask-brin result in process memory.
    """
    ctx = req.context or {}
    if req.channel:
        ctx["channel"] = req.channel
    user_id = ctx.get("user_id", "default")
    agent_slug = req.agent or os.environ.get("BRIN_DEFAULT_AGENT")
    if not agent_slug:
        return JSONResponse({"error": "async requires a bound agent"}, status_code=400)

    action = await router.route(req.message, ctx)
    if not isinstance(action, DirectAgent):
        return JSONResponse(
            {"error": "async path supports direct-agent actions only"}, status_code=400)

    # The executor reads the cached bound when the background turn reaches the
    # gateway. Load it here, including for the first request after a restart.
    await refresh_turn_ceiling()

    active_agent = agent_registry.get_active(user_id)
    agent_workspace = active_agent.workspace if active_agent else None
    # Mirror the identity logic from the synchronous path: a desk turn
    # knows the person from its Studio session, every other turn carries the
    # sender, and by-contact resolves a raw channel handle by design.
    person_identity = (ctx.get("studio_user") if str(user_id).startswith("desk:")
                       else (ctx.get("person_identity") or ctx.get("principal")))

    async def _collect() -> str:
        # Drive the normal direct turn to completion, off the request, and return its
        # text as the operation result the runner records to the ledger.
        content = ""
        async for chunk in executor.run_direct(
            action, model=req.model, stream=False, workspace=agent_workspace,
            agent_override=req.agent, conversation_id=user_id, channel=req.channel,
            principal=ctx.get("principal") or person_identity,
            person_identity=person_identity,
        ):
            content += chunk
        return content

    call_id = await launch_async(agent_slug, "async_message", _collect())
    return JSONResponse({"call_id": call_id, "status": "running", "agent": agent_slug})


@api_router.get("/specialists")
async def list_specialists():
    """List all available specialists."""
    specialists = loader.list()
    return [
        {
            "id": s.id,
            "title": s.title,
            "description": s.description,
        }
        for s in specialists
    ]


@api_router.get("/specialists/{specialist_id}")
async def get_specialist(specialist_id: str):
    """Get specialist details by ID."""
    s = loader.get(specialist_id)
    if not s:
        return JSONResponse({"error": "Specialist not found"}, status_code=404)
    return {
        "id": s.id,
        "title": s.title,
        "description": s.description,
        "content": s.content,
    }


@api_router.get("/agents")
async def list_agents():
    """List available agents (specialists with workspace config)."""
    agents = agent_registry.list_agents()
    return [
        {"name": a.name, "workspace": a.workspace, "session_summary": a.session_summary}
        for a in agents
    ]


@api_router.post("/agents/{agent_name}/activate")
async def activate_agent(agent_name: str, user_id: str = "default"):
    """Activate an agent for a user."""
    config = agent_registry.activate(user_id, agent_name)
    if not config:
        return JSONResponse({"error": f"Agent '{agent_name}' not found"}, status_code=404)
    return {"status": "activated", "agent": config.name, "workspace": config.workspace}


@api_router.post("/agents/deactivate")
async def deactivate_agent(user_id: str = "default"):
    """Deactivate current agent for user. Triggers session summary if enabled."""
    config = agent_registry.get_active(user_id)
    summary_result = None

    if config and config.session_summary:
        # End session and get data
        session_data = session_history.end_session(config.name, user_id)
        if session_data:
            # Generate summary in background (don't block response)
            import asyncio
            asyncio.create_task(
                session_summary.summarize_and_store(
                    session_data, config.workspace, config.name,
                )
            )
            summary_result = "queued"

    was_active = agent_registry.deactivate(user_id)
    return {"status": "deactivated", "was_active": was_active, "summary": summary_result}


@api_router.get("/agents/active")
async def get_active_agent(user_id: str = "default"):
    """Get currently active agent for user."""
    config = agent_registry.get_active(user_id)
    if not config:
        return {"active": False}
    return {
        "active": True,
        "agent": config.name,
        "workspace": config.workspace,
    }


@api_router.get("/agents/{agent_name}/sessions")
async def list_agent_sessions(agent_name: str, limit: int = 20):
    """List recent sessions for an agent."""
    config = agent_registry.get(agent_name)
    if not config:
        return JSONResponse({"error": f"Agent '{agent_name}' not found"}, status_code=404)
    sessions = session_history.list_sessions(config.history_dir, limit=limit)
    return {"agent": agent_name, "sessions": sessions}


@api_router.post("/agents/{agent_name}/sessions/{session_id}/summarize")
async def summarize_session(agent_name: str, session_id: str):
    """Manually trigger summary for a specific session."""
    config = agent_registry.get(agent_name)
    if not config:
        return JSONResponse({"error": f"Agent '{agent_name}' not found"}, status_code=404)

    # Load session file
    from pathlib import Path
    session_path = Path(config.history_dir) / f"{session_id}.json"
    session_data = session_history.get_session(str(session_path))
    if not session_data:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    result = await session_summary.summarize_and_store(
        session_data, config.workspace, config.name,
    )
    if not result:
        return {"status": "skipped", "reason": "no facts extracted"}
    return {"status": "ok", "summary": result}


@api_router.get("/workflows")
async def list_workflows():
    """List available Rein workflows."""
    flows = router.available_flows()
    return [{"name": f} for f in flows]


@api_router.get("/workflows/{task_id}/status")
async def workflow_status(task_id: str):
    """Check status of a Rein workflow task."""
    return await executor.get_workflow_status(task_id)


@api_router.post("/specialists/reload")
async def reload_specialists():
    """Force reload all specialist definitions."""
    loader.reload()
    return {"status": "ok", "count": len(loader.list())}


@api_router.get("/health")
async def authenticated_health():
    """Authenticated health check (same data as public)."""
    return await get_health(len(loader.list()), len(router.available_flows()))


# --- Inbox & Media endpoints ---

@api_router.post("/inbox", response_model=InboxSaveResult)
async def save_to_inbox(msg: IncomingMessage):
    """Save incoming message to Mesh memory with auto-tagging."""
    return await inbox_service.save(msg)


@api_router.post("/media/vision", response_model=VisionResult)
async def media_vision(req: VisionRequest):
    """Analyze image using Vision AI."""
    return await media_service.recognize_image(req)


@api_router.post("/media/transcribe", response_model=TranscribeResult)
async def media_transcribe(req: TranscribeRequest):
    """Transcribe audio to text."""
    return await media_service.transcribe_audio(req)


@api_router.post("/media/generate", response_model=GenerateResult)
async def media_generate(req: GenerateRequest):
    """Generate image from text prompt."""
    return await media_service.generate_image(req)


@api_router.post("/media/download", response_model=DownloadResult)
async def media_download(req: DownloadRequest):
    """Download video from URL."""
    return await media_service.download_video(req)


# --- Intent & Notification endpoints ---

@api_router.post("/intent/classify", response_model=ClassifyResult)
async def classify_intent(req: ClassifyRequest):
    """Classify user message intent via Gemini Flash NLU."""
    return await intent_service.classify(req.text)


@api_router.post("/intent/parse-search", response_model=SearchParseResult)
async def parse_search(req: SearchParseRequest):
    """Parse natural language search query into structured parameters."""
    return await intent_service.parse_search_query(req.text)


@api_router.post("/intent/parse-model", response_model=ModelParseResult)
async def parse_model_command(req: ModelParseRequest):
    """Parse model settings command into structured parameters."""
    return await intent_service.parse_model_command(req.text)


@api_router.post("/intent/parse-session", response_model=SessionParseResult)
async def parse_session_command(req: ClassifyRequest):
    """Parse session management command into structured parameters."""
    return await intent_service.parse_session_command(req.text)


@api_router.post("/notify", response_model=NotificationResult)
async def send_notification(req: NotificationRequest):
    """Send notification via Telegram or Email."""
    return await notification_service.send(req)


# --- Diagnostics endpoints ---

@api_router.post("/diagnostics/run", response_model=DiagnosticResult)
async def diagnostics_run(req: DiagnosticRequest):
    """Run a system diagnostic check (sync, up to 30s timeout)."""
    return await diagnostics_service.run_diagnostic(req)


@api_router.post("/diagnostics/run-async", response_model=AsyncDiagnosticResult)
async def diagnostics_run_async(req: AsyncDiagnosticRequest):
    """Start a diagnostic check in the background, returns task_id."""
    return await diagnostics_service.run_diagnostic_async(req)


@api_router.get("/diagnostics/task/{task_id}", response_model=DiagnosticTaskStatus)
async def diagnostics_task_status(task_id: str):
    """Check status of an async diagnostic task."""
    return await diagnostics_service.get_task_status(task_id)


# --- Model endpoints ---

@api_router.get("/models", response_model=ModelListResult)
async def list_models(service: Optional[str] = None):
    """List available models, optionally filtered by service."""
    return await model_service.list_models(service)


@api_router.post("/models")
async def add_model(req: AddModelRequest):
    """Add a new model to the catalog."""
    ok = await model_service.add_model(req)
    return {"success": ok}


@api_router.delete("/models/{alias}/{service}")
async def remove_model(alias: str, service: str):
    """Remove a model from the catalog."""
    ok = await model_service.remove_model(alias, service)
    return {"success": ok}


@api_router.get("/models/user/{user_id}/{service}")
async def get_user_model(user_id: int, service: str):
    """Get user's preferred model for a service."""
    model_id = await model_service.get_user_model(user_id, service)
    return {"user_id": user_id, "service": service, "model_id": model_id}


@api_router.put("/models/user/{user_id}/{service}", response_model=SetModelResult)
async def set_user_model(user_id: int, service: str, req: SetUserModelRequest):
    """Set user's preferred model for a service."""
    return await model_service.set_user_model(user_id, service, req.alias_or_model)


@api_router.get("/models/user/{user_id}", response_model=UserSettingsResult)
async def get_user_settings(user_id: int):
    """Get all model settings for a user."""
    return await model_service.get_user_settings(user_id)


@api_router.delete("/models/user/{user_id}")
async def clear_user_settings(user_id: int, service: Optional[str] = None):
    """Clear user model settings (reset to defaults)."""
    ok = await model_service.clear_user_settings(user_id, service)
    return {"success": ok}


@api_router.post("/models/usage")
async def log_usage(entry: UsageLogEntry):
    """Log model usage for statistics."""
    await model_service.log_usage(entry)
    return {"success": True}


@api_router.get("/models/stats", response_model=UsageStatsResult)
async def get_usage_stats(user_id: Optional[int] = None, service: Optional[str] = None, days: int = 30):
    """Get aggregated usage statistics."""
    return await model_service.get_usage_stats(user_id, service, days)


# --- Process endpoints ---

@api_router.post("/processes", response_model=RegisterProcessResult)
async def register_process(req: RegisterProcessRequest):
    """Register a new process (pending state)."""
    return await process_service.register(req.process_type, req.name, req.input_data)


@api_router.get("/processes", response_model=ProcessListResult)
async def list_processes(active_only: bool = False, limit: int = 20):
    """List processes (active only or recent)."""
    if active_only:
        return await process_service.list_active()
    return await process_service.list_recent(limit)


@api_router.get("/processes/{process_id}")
async def get_process_status(process_id: str):
    """Get process status by ID."""
    info = await process_service.get_status(process_id)
    if not info:
        return JSONResponse({"error": "Process not found"}, status_code=404)
    return info


@api_router.post("/processes/{process_id}/stop", response_model=ProcessActionResult)
async def stop_process(process_id: str):
    """Stop a running process."""
    return await process_service.stop(process_id)


@api_router.post("/processes/{process_id}/pause", response_model=ProcessActionResult)
async def pause_process(process_id: str):
    """Pause a running process."""
    return await process_service.pause(process_id)


@api_router.post("/processes/{process_id}/resume", response_model=ProcessActionResult)
async def resume_process(process_id: str):
    """Resume a paused process."""
    return await process_service.resume(process_id)


@api_router.patch("/processes/{process_id}/progress")
async def update_process_progress(process_id: str, req: UpdateProgressRequest):
    """Update process progress (0-100)."""
    await process_service.update_progress(process_id, req.progress)
    return {"success": True}


@api_router.get("/processes/{process_id}/logs")
async def get_process_logs(process_id: str, limit: int = 50):
    """Get process log entries."""
    logs = await process_service.get_logs(process_id, limit)
    return [log.model_dump() for log in logs]


@api_router.post("/processes/cleanup")
async def cleanup_processes(days: int = 7):
    """Delete processes older than N days."""
    deleted = await process_service.cleanup_old(days)
    return {"deleted": deleted}


# --- Session endpoints ---

@api_router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest):
    """Create a shared session for cross-channel handoff."""
    return session_service.create(req)


# --- Event endpoints ---

@api_router.get("/events", response_model=EventListResult)
async def list_events_endpoint(event_type: Optional[str] = None, limit: int = 50):
    """List recent events from the EventBus."""
    events = await event_bus.list_events(event_type=event_type, limit=limit)
    unread = await event_bus.count_unread()
    from models.event import EventRecord
    return EventListResult(
        events=[EventRecord(**e) for e in events],
        total=len(events),
        unread=unread,
    )


@api_router.post("/events")
async def emit_event_endpoint(req: EmitEventRequest):
    """Emit an event manually."""
    event_id = await event_bus.emit(
        event_type=req.event_type,
        payload=req.payload,
        source=req.source or "api",
        ttl_hours=req.ttl_hours,
    )
    return {"event_id": event_id, "event_type": req.event_type}


# The live stream for pages is core-api's GET /api/events/stream:
# one stream, per person. brin's own, open to anyone, was removed.


# --- Memory endpoints ---

@api_router.post("/memory/smart-search", response_model=SmartSearchResult)
async def smart_search(req: SmartSearchRequest):
    """Smart memory search with query cleaning and parallel strategies."""
    return await memory_service.search(req)


@api_router.post("/memory/context", response_model=MemoryContextResult)
async def memory_context(req: SmartSearchRequest):
    """Search memory and format as context for prompt injection."""
    result = await memory_service.search(req)
    return memory_service.format_context(result.documents)


# --- Webchat state endpoints ---

class WebchatStateRequest(BaseModel):
    state: dict
    user_id: str = "default"


@api_router.get("/webchat/state")
async def get_webchat_state(user_id: str = "default"):
    """Load webchat state from server (for fresh browser with empty localStorage)."""
    result = webchat_store.load_state(user_id)
    if not result:
        return JSONResponse({"state": None, "updated_at": None})
    state_json, updated_at = result
    return JSONResponse({"state": json.loads(state_json), "updated_at": updated_at})


@api_router.put("/webchat/state")
async def save_webchat_state(req: WebchatStateRequest):
    """Save webchat state to server (debounced backup from browser)."""
    state_json = json.dumps(req.state)
    updated_at = webchat_store.save_state(req.user_id, state_json)
    return {"ok": True, "updated_at": updated_at}


# --- v2: Universal Agent endpoint ---

class ChatV2Request(BaseModel):
    message: str
    user_id: str = "default"
    channel: str = "telegram"
    model: str = "sonnet"
    attachments: list[dict] = []


class ChatV2Response(BaseModel):
    content: str
    tool_calls: list[dict] = []
    files: list[str] = []
    duration_ms: float = 0
    memory: Optional[dict] = None
    stm_facts: Optional[list[str]] = None


# /v2/chat and /v2/reset removed 2026-05-24 (RFC v0.3 Stage 4).
# Operator confirmed they were rudiment of prior openrouter-mode experiments.
# Canonical path now: /api/message (this file) -> executor.run_direct ->
# gateway /api/v1/sessions/v2 + /messages (session-first canon).


@api_router.post("/sessions/{session_id}/restart")
async def restart_gateway_session(session_id: str):
    """Restart a gateway CLI session's process so it reloads its .claude config
    (CLAUDE.md, .mcp.json, settings) + system prompt. Proxied to the gateway;
    the conversation is preserved via --resume."""
    import httpx
    from core.executor import GATEWAY_URL
    url = f"{GATEWAY_URL}/api/v1/sessions/{session_id}/restart"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url)
    try:
        body = r.json()
    except Exception:
        body = {"status": "error", "detail": r.text}
    return JSONResponse(body, status_code=r.status_code)


class CleanSessionRequest(BaseModel):
    requested_by: str = "studio"
    reason: str = "studio_manual_clean"


@api_router.post("/sessions/{session_id}/clean")
async def clean_gateway_session(session_id: str, req: CleanSessionRequest):
    """Retire the current gateway session without carrying its transcript over."""
    import httpx
    from core.executor import GATEWAY_URL
    url = f"{GATEWAY_URL}/api/v1/sessions/{session_id}/clean"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=req.model_dump())
    try:
        body = r.json()
    except Exception:
        body = {"status": "error", "detail": r.text}
    return JSONResponse(body, status_code=r.status_code)


class DeskStopRequest(BaseModel):
    requested_by: Optional[str] = None
    agent: Optional[str] = None


class DeskSteerRequest(BaseModel):
    text: str


class DeskCancelQueuedRequest(BaseModel):
    client_turn_id: uuid.UUID


@api_router.post("/desks/{desk_id}/cancel-queued")
async def cancel_queued_desk_turn(desk_id: str, req: DeskCancelQueuedRequest):
    """Cancel one waiting desk turn; the gateway owns the execution queue."""
    import httpx
    from core.executor import GATEWAY_URL
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{GATEWAY_URL}/api/v1/sessions/desk-cancel-queued",
                json={"desk_id": desk_id, "client_turn_id": str(req.client_turn_id)},
            )
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        logger.warning("desk queue cancellation desk=%s failed: %s", desk_id, e)
        return JSONResponse({"status": "unavailable"}, status_code=502)


@api_router.post("/desks/{desk_id}/steer")
async def steer_desk_turn(desk_id: str, req: DeskSteerRequest):
    """Proxy a live direction to Flint without queuing another chat turn."""
    import httpx
    from core.executor import GATEWAY_URL
    if not req.text.strip() or len(req.text) > 8000:
        return JSONResponse({"status": "invalid_text"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{GATEWAY_URL}/api/v1/sessions/desk-steer",
                json={"desk_id": desk_id, "text": req.text},
            )
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        logger.warning("desk steer desk=%s failed: %s", desk_id, e)
        return JSONResponse({"status": "unavailable"}, status_code=502)


@api_router.post("/desks/{desk_id}/stop")
async def stop_desk_turn(desk_id: str, req: DeskStopRequest):
    """Stop the turn running in a desk and keep the conversation.

    The gateway interrupts the CLI turn in place: the running tool is killed, the
    turn ends, the session and its context stay. The turn's own request then ends
    as agent.turn_stopped with the partial answer. This endpoint writes the other
    half of the record, that a person asked, so a stop that finds nothing running
    is visible too and is not reported to the person as done.
    """
    import httpx
    from core.executor import GATEWAY_URL
    agent = req.agent or os.environ.get("BRIN_DEFAULT_AGENT")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{GATEWAY_URL}/api/v1/sessions/desk-interrupt",
                # the agent says whose turn: the chat's own or a colleague's
                json={"desk_id": desk_id, "requested_by": req.requested_by, "agent": req.agent},
            )
        body = r.json() if r.content else {}
    except Exception as e:
        logger.warning("desk stop desk=%s failed: %s", desk_id, e)
        return JSONResponse({"interrupted": False, "reason": "gateway unreachable"}, status_code=502)
    if r.status_code != 200:
        return JSONResponse(body or {"interrupted": False}, status_code=r.status_code)
    if body.get("interrupted") and not body.get("already"):
        _DESK_STOPS[desk_id] = (req.requested_by, time.time())
    await _desk_event(
        agent or "unknown", "agent.turn_stop_requested", desk_id,
        {"via": "desk", "stopped_by": req.requested_by, "agent": agent,
         "session_id": body.get("session_id"), "interrupted": bool(body.get("interrupted")),
         "reason": body.get("reason")},
        actor=f"user:{req.requested_by}" if req.requested_by else None,
    )
    return JSONResponse(body)


@api_router.get("/desks/in-progress")
async def desks_in_progress():
    """Every desk with a turn running now, for Studio's chat tabs.

    Proxies the gateway's in-process table. On any error answers {desks: null},
    not an empty list: "nobody is working" and "could not tell" are different,
    and the Studio must not clear every tab's mark on a failed poll.
    """
    return {"desks": await _gateway_desks_in_progress()}


@api_router.delete("/sessions/{session_id}/v2")
async def abort_gateway_session(session_id: str):
    """Terminate a gateway CLI session: kill the underlying process immediately.
    Proxied to the gateway DELETE /api/v1/sessions/{id}/v2 endpoint which calls
    process.terminate() -> process.kill(). Used by the Studio stop-button."""
    import httpx
    from core.executor import GATEWAY_URL
    url = f"{GATEWAY_URL}/api/v1/sessions/{session_id}/v2"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(url)
    try:
        body = r.json()
    except Exception:
        body = {"status": "error", "detail": r.text}
    return JSONResponse(body, status_code=r.status_code)


@api_router.get("/desks/{desk_id}/activity")
async def desk_activity(desk_id: str, since: int = 0):
    """Live turn activity for a desk conversation.

    Proxies to the gateway desk-activity endpoint. Returns {in_progress, elapsed,
    action, action_elapsed} while a CLI turn is running, else {in_progress: false}.
    Never raises: applies a 3-second timeout; any error returns {in_progress: false}
    so the UI can always parse the response without handling errors.
    """
    import httpx
    from core.executor import GATEWAY_URL
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{GATEWAY_URL}/api/v1/sessions/desk-activity",
                params={"desk_id": desk_id, "since": since},
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(
            "desk-activity proxy desk=%s failed (non-fatal): %s", desk_id, e)
    return {"in_progress": False}


@api_router.get("/desks/{desk_id}/live")
async def desk_live(desk_id: str):
    """The chat's live line as server-sent events, passed through from the
    gateway as they arrive: what the agent does and the text so far."""
    import httpx
    from core.executor import GATEWAY_URL
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
    try:
        upstream = await client.send(client.build_request(
            "GET", f"{GATEWAY_URL}/api/v1/sessions/desk-live", params={"desk_id": desk_id}), stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        logger.warning("desk-live proxy desk=%s failed: %s", desk_id, e)
        return JSONResponse({"detail": "gateway unreachable"}, status_code=502)

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
    return StreamingResponse(relay(), status_code=upstream.status_code, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@api_router.get("/agents/{slug}/console")
async def agent_console(slug: str):
    """An agent's console: proxies the gateway agent-activity endpoint.

    Same contract as desk_activity: 3-second timeout, never raises, an error
    reads as nothing in flight.
    """
    import httpx
    from core.executor import GATEWAY_URL
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{GATEWAY_URL}/api/v1/sessions/agent-activity",
                params={"agent": slug},
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(
            "agent-activity proxy agent=%s failed (non-fatal): %s", slug, e)
    return {"agent": slug, "turns": [], "last": None}


app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8101)
