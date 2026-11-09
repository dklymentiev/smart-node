"""The runtime: the process that owns an agent's Claude Code processes.

Sibling of main.py (the door) in the one gateway companion, run as its own
unit (smart-node-runtime.service, loopback only). The door forwards everything
under /api/v1/sessions and every chat on the local CLI here, and stays up while
this process is restarted. This process spawns, watches and stops the agent
processes and never calls a model itself: its children do, through the door,
with a token minted here (model_tokens.py) in place of the node credential.
"""
import asyncio
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from config import (
    RUNTIME_HOST, RUNTIME_PORT, ALLOWED_IPS, CLI_PATH, FLINT_PATH, FLINT_ENABLED,
)
from models import (
    CreateSessionRequest, CreateSessionResponse,
    SessionChatRequest, SessionChatResponse,
    SessionListResponse, SessionInfo, SessionCloseResponse,
    ChatRequest, ChatResponse,
)
from session_manager import SessionManager
from request_logger import request_logger
from shadow_cache import shadow_cache
from model_route import route_model, UnknownRuntime
from model_catalog import model_allowed
import backends
import model_tokens
from sessions_v2_routes import router as sessions_v2_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

session_manager: SessionManager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_manager
    logger.info("Starting the runtime...")

    cli_resolved = shutil.which(str(CLI_PATH))
    if not cli_resolved:
        logger.warning(f"CLI_PATH '{CLI_PATH}' not found in PATH. CLI sessions will fail.")
    else:
        logger.info(f"CLI found at: {cli_resolved}")
    if FLINT_ENABLED and not shutil.which(str(FLINT_PATH)):
        logger.warning("FLINT_ENABLED but FLINT_PATH '%s' is not executable", FLINT_PATH)

    session_manager = SessionManager()

    # No process this runtime's predecessor minted a token for is ours now; an
    # orphan that outlived it is refused at the door from here on.
    try:
        n = await asyncio.to_thread(model_tokens.revoke_all, "runtime started")
        if n:
            logger.info("%d model token(s) of a previous runtime revoked", n)
    except Exception as e:  # noqa: BLE001
        logger.error("model tokens of a previous runtime not revoked: %s", e)

    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("Runtime started")
    yield

    logger.info("Shutting down the runtime...")
    cleanup_task.cancel()
    await session_manager.shutdown()
    # Own the END of what we spawn. session_manager.shutdown() closes the
    # sessions it knows about; this catches every CLI child regardless, including
    # any whose session was dropped from the registry while the process lived --
    # the exact shape found on a live node, where orphans outlived three different gateway
    # pids. A restart must not leave children behind.
    try:
        from backends.cli_claude import shutdown_all as _cli_shutdown_all
        await _cli_shutdown_all(reason="runtime shutting down")
    except Exception as e:
        logger.warning("CLI child shutdown failed: %s", e)
    try:
        await asyncio.to_thread(model_tokens.revoke_all, "runtime shutdown")
    except Exception as e:  # noqa: BLE001
        logger.error("model tokens not revoked at shutdown: %s", e)
    logger.info("Runtime stopped")


async def periodic_cleanup():
    """Periodically clean up stale sessions"""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        try:
            await session_manager.cleanup_stale_sessions()
            await asyncio.to_thread(model_tokens.prune)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


app = FastAPI(
    title="Smart Node runtime",
    description="Owns the agent CLI processes; reached through the gateway",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    """Callers reach the runtime through the door, from this host. The door
    says who asked in x-forwarded-for; that is what the request log records."""
    direct_ip = request.client.host if request.client else "unknown"
    request.state.client_ip = request.headers.get("x-forwarded-for") or direct_ip
    if not any(direct_ip.startswith(prefix) for prefix in ALLOWED_IPS):
        return PlainTextResponse("Forbidden", status_code=403)
    return await call_next(request)


# Session-first endpoints: /api/v1/sessions/v2, /{id}/messages, activity,
# interrupt, steer, processes, agent-stop. Same router as before the split;
# it only moved process. Included here, before the v1 routes below, because
# FastAPI matches routes in registration order and the v1
# GET /api/v1/sessions/{session_id} would otherwise take every v2 GET path
# ("processes", "v2", "agent-activity") as a session id and answer 400.
app.include_router(sessions_v2_router)


def _validate_session_id(session_id: str):
    """P0.6: Validate session_id is a valid UUID"""
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session ID format")


# ============== Session Endpoints (CLI) ==============

@app.post("/api/v1/sessions", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest, req: Request):
    """Create new CLI session"""
    start_time = time.time()
    client_ip = getattr(req.state, 'client_ip', 'unknown')

    # This path is the Claude CLI only. Callers that pass an agent's model (rein
    # reads it from agent.yaml) send the canonical claude-cli/<model> now;
    # the CLI itself takes the bare name. A cli_providers row that runs the
    # Claude Code binary under its own slug is the Claude CLI too
    # (backends.claude_cli_upstreams). Any other runtime is not ours to run here.
    runtime = "claude-cli"
    if request.model and "/" in request.model:
        runtime, _, cli_model = request.model.partition("/")
        if runtime not in backends.claude_cli_upstreams():
            raise HTTPException(status_code=400,
                                detail=f"Model '{request.model}' is not a Claude CLI model; this endpoint runs the Claude CLI only")
        request.model = cli_model

    # Validate model: the catalog, the built-in list, or the row's supported_models.
    if request.model and not (model_allowed(runtime, request.model, "cli")
                              or backends.cli_model_supported(runtime, request.model)):
        raise HTTPException(status_code=400, detail=f"Model '{request.model}' not allowed for CLI provider {runtime}")

    try:
        session = await session_manager.create_session(
            model=request.model,
            system_prompt=request.system_prompt,
            service=request.service,
            ttl=request.timeout,
            cwd=request.cwd
        )
        duration_ms = (time.time() - start_time) * 1000

        # Log session creation
        request_logger.log_create_session(
            session_id=session.session_id,
            ip=client_ip,
            model=session.model,
            system_prompt=request.system_prompt,
            duration_ms=duration_ms,
            service=request.service,
        )

        return CreateSessionResponse(
            session_id=session.session_id,
            provider="cli",
            model=session.model,
            created_at=session.created_at,
            status="ready"
        )
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        error_id = f"err-{uuid.uuid4()}"
        request_logger.log_error(
            session_id=error_id,
            ip=client_ip,
            error=str(e),
            event_type="create_error",
        )
        raise HTTPException(status_code=500, detail="Failed to create session")


@app.post("/api/v1/sessions/{session_id}/chat", response_model=SessionChatResponse)
async def session_chat(session_id: str, request: SessionChatRequest, req: Request):
    """Send message to CLI session"""
    _validate_session_id(session_id)
    start_time = time.time()
    client_ip = getattr(req.state, 'client_ip', 'unknown')

    try:
        # Shadow cache: check before request
        session = session_manager.sessions.get(session_id)
        model = session.model if session else None
        sys_prompt = getattr(session, 'system_prompt', None) if session else None
        cache_hit = shadow_cache.check(model or "", sys_prompt, request.content)

        result = await session_manager.chat(session_id, request.content)
        duration_ms = (time.time() - start_time) * 1000

        # Log chat
        tokens = None
        if result.get("usage"):
            tokens = {
                "input": result["usage"].get("input_tokens", 0),
                "output": result["usage"].get("output_tokens", 0),
            }

        # Shadow cache: store after response
        shadow_cache.store(model or "", sys_prompt, request.content, tokens)

        request_logger.log_chat(
            session_id=session_id,
            ip=client_ip,
            message=request.content,
            response=result.get("content", ""),
            tokens=tokens,
            duration_ms=duration_ms,
            model=model,
            service=getattr(session, 'service', None),
            cache_hit=cache_hit,
        )

        return SessionChatResponse(**result)
    except ValueError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        logger.error(f"Session chat error: {e}")
        request_logger.log_error(session_id=session_id, ip=client_ip, error=str(e))
        raise HTTPException(status_code=500, detail="Chat request failed")


@app.delete("/api/v1/sessions/{session_id}", response_model=SessionCloseResponse)
async def close_session(session_id: str, req: Request):
    """Close CLI session"""
    _validate_session_id(session_id)
    client_ip = getattr(req.state, 'client_ip', 'unknown')

    try:
        result = await session_manager.close_session(session_id)

        # Log session close
        request_logger.log_close_session(
            session_id=session_id,
            ip=client_ip,
            total_messages=result.get("total_messages", 0),
            duration_seconds=result.get("duration_seconds", 0),
        )

        return SessionCloseResponse(**result)
    except ValueError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        request_logger.log_error(session_id=session_id, ip=client_ip, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to close session")


@app.get("/api/v1/sessions", response_model=SessionListResponse)
async def list_sessions():
    """List all active CLI sessions"""
    sessions = session_manager.list_sessions()
    return SessionListResponse(
        sessions=[SessionInfo(**s) for s in sessions]
    )


@app.get("/api/v1/sessions/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str):
    """Get single session info (health check)"""
    _validate_session_id(session_id)
    sessions = session_manager.list_sessions()
    for s in sessions:
        if s["session_id"] == session_id:
            return SessionInfo(**s)
    raise HTTPException(status_code=404, detail="Session not found")


# ============== A chat on the local CLI ==============

# Warm CLI sessions for the unified /chat entry, keyed by the caller's session_id
# (the conversation id). Lets a bare-model chat reuse one claude process across
# turns instead of cold-starting each turn. Reaped by cleanup_stale_sessions.
_cli_chat_sessions: dict = {}


def _extract_system_and_user(request):
    """Pull the system prompt and the latest user message out of a messages[]
    payload (the cloud-style shape) so a bare-model chat can be bridged to a CLI
    session, which is stateful and only needs the new turn."""
    system = request.system
    user_content = None
    for m in (request.messages or []):
        if m.role == "system" and not system:
            system = m.content if isinstance(m.content, str) else system
        elif m.role == "user" and m.content is not None:
            user_content = m.content
    if isinstance(user_content, list):
        user_content = " ".join(
            b.get("text", "") for b in user_content if isinstance(b, dict)
        ).strip()
    return system, user_content


async def _cli_bridge_chat(request) -> ChatResponse:
    """Run a bare-model chat on the local Claude CLI via a warm session keyed by
    the conversation's session_id. First turn creates the session (carrying the
    system prompt); later turns reuse it and send only the new user message."""
    model = request.model or "sonnet"
    system, user_content = _extract_system_and_user(request)
    if not user_content:
        raise HTTPException(status_code=400, detail="no user message for CLI chat")

    key = request.session_id or str(uuid.uuid4())
    mgr_sid = _cli_chat_sessions.get(key)
    session = session_manager.sessions.get(mgr_sid) if mgr_sid else None
    if session is None:
        session = await session_manager.create_session(
            model=model, system_prompt=system, service=request.service or "studio",
        )
        mgr_sid = session.session_id
        _cli_chat_sessions[key] = mgr_sid

    result = await session_manager.chat(mgr_sid, user_content)
    return ChatResponse(
        id=mgr_sid,
        provider="cli",
        model=model,
        content=result.get("content", "") or "",
        usage=result.get("usage", {}) or {},
    )


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """A chat on the local CLI. The door routes by model and sends only CLI
    chats here; a cloud model asked of the runtime is refused, not guessed."""
    if request.provider:
        provider_name = request.provider.value
    else:
        try:
            provider_name, request.model = route_model(request.model, {"cli"})
        except UnknownRuntime as e:
            raise HTTPException(status_code=400, detail=str(e))
    if provider_name != "cli":
        raise HTTPException(status_code=400,
                            detail=f"the runtime runs the local CLI only, not {provider_name}")
    try:
        return await _cli_bridge_chat(request)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error("CLI bridge error: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"cli bridge failed: {type(e).__name__}: {e}")


# ============== Health ==============

@app.get("/api/v1/health")
async def health_check():
    """What the door reports as its `cli` provider: the binary is there, how
    many sessions are open, how many claude processes are alive."""
    cli_ok = shutil.which(str(CLI_PATH)) is not None
    try:
        from backends.cli_claude import live_process_count
        live_children = live_process_count()
    except Exception:
        live_children = None
    return {
        "status": "ok" if cli_ok else "unavailable",
        "active_sessions": len(session_manager.sessions) if session_manager else 0,
        "live_children": live_children,
    }



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=RUNTIME_HOST, port=RUNTIME_PORT)
