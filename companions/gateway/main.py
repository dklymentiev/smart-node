"""
AI Gateway - the door: one address for every model call on the node.

Cloud providers are called from here; an agent's Claude Code process calls
the model through /anthropic/* here. The agent processes themselves belong
to the runtime (runtime_main.py, its own unit); everything under
/api/v1/sessions and a chat on the local CLI is forwarded to it.
"""
import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime
from contextlib import asynccontextmanager

from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from config import (
    HOST, PORT, CORS_ORIGINS, GATEWAY_API_KEY, ALLOWED_IPS, TRUSTED_PROXIES,
    ENABLE_DOCS, ENABLE_USAGE_ENDPOINT, CLI_PATH, FLINT_PATH, FLINT_ENABLED,
    flint_stdio_safe, RUNTIME_URL,
)
from rate_limiter import rate_limiter
from models import (
    ChatRequest, ChatResponse,
    HealthResponse, Provider
)
from providers import AnthropicProvider, GeminiProvider, OpenRouterProvider
from request_logger import request_logger
from shadow_cache import shadow_cache
import chat_logger
import turn_limits
from model_route import route_model, runtime_of, UnknownRuntime
from model_catalog import model_allowed
import time
import db
import secrets_store
from backends import UPSTREAMS, upstreams
import anthropic_relay
import model_door
import openai_relay
import runtime_proxy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global instances
providers = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown"""
    global providers

    logger.info("Starting AI Gateway (the door)...")
    providers = {
        "anthropic": AnthropicProvider(),
        "gemini": GeminiProvider(),
        "openrouter": OpenRouterProvider(),
    }
    await asyncio.to_thread(model_door.live_clear)
    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("AI Gateway started; agent processes are the runtime's at %s", RUNTIME_URL)
    yield

    logger.info("Shutting down AI Gateway...")
    cleanup_task.cancel()
    for p in providers.values():
        await p.close()
    await model_door.close()
    await runtime_proxy.close()
    logger.info("AI Gateway stopped")


async def periodic_cleanup():
    """Periodic housekeeping of the door's own state."""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        try:
            rate_limiter.cleanup()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


app = FastAPI(
    title="AI Gateway",
    description="Universal API for AI model access",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# The runtime owns the agent processes and answers for them: everything
# under /api/v1/sessions (the CLI sessions, the session-first v2 API, activity,
# interrupt, steer) is forwarded to it as it arrived. Callers keep this one
# address; the door can be restarted while agents work.
app.include_router(runtime_proxy.router)

# budgets for callers that spend without holding a gateway session.
# Voice talks to its provider directly, so without this the node total is wrong
# by however much anybody has spoken.
from external_spend_routes import router as external_spend_router
app.include_router(external_spend_router)

# Every model call of an agent's Claude Code process comes through here: the
# process's token is swapped for the node credential, the call is recorded
# per session. The gateway is the one door to the model for CLI agents too.
app.include_router(anthropic_relay.router)
app.include_router(openai_relay.router)

# Flint and any other agent that speaks the OpenAI chat format: the same door,
# the provider key stays here and the process holds a token.


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Log validation errors (422) to request log"""
    client_ip = get_client_ip(request)
    path = request.url.path
    method = request.method
    errors = exc.errors()
    error_msg = "; ".join(f"{e.get('loc',['?'])[-1]}: {e.get('msg','')}" for e in errors)

    # Get body from exc (FastAPI stores parsed body)
    body_str = ""
    if exc.body is not None:
        if isinstance(exc.body, (dict, list)):
            body_str = json.dumps(exc.body, ensure_ascii=False)[:1000]
        elif isinstance(exc.body, (bytes, str)):
            body_str = str(exc.body)[:1000]
        else:
            body_str = str(exc.body)[:1000]

    user_agent = request.headers.get("user-agent", "")

    request_logger.log_request(
        session_id="validation_error",
        ip=client_ip,
        event_type="error",
        error=f"422 {method} {path} - {error_msg}",
        request_data={
            "method": method,
            "path": path,
            "body": body_str,
            "user_agent": user_agent,
            "errors": [{"field": str(e.get('loc',[])), "msg": e.get('msg','')} for e in errors],
        },
    )

    return JSONResponse(status_code=422, content={"detail": errors})


def get_client_ip(request: Request) -> str:
    """Extract client IP from request headers.
    Only trust proxy headers (cf-connecting-ip, x-real-ip) when
    the direct connection comes from a known proxy network.
    """
    direct_ip = request.client.host if request.client else "unknown"

    # Only trust forwarded headers from known proxies
    is_trusted_proxy = any(direct_ip.startswith(p) for p in TRUSTED_PROXIES)
    if is_trusted_proxy:
        return (
            request.headers.get("cf-connecting-ip") or
            request.headers.get("x-real-ip") or
            direct_ip
        )

    return direct_ip


@app.middleware("http")
async def ip_whitelist(request: Request, call_next):
    """Check if client IP is allowed"""
    client_ip = get_client_ip(request)

    # Store IP in request state for later use
    request.state.client_ip = client_ip

    # P0.2: API key authentication (if configured)
    if GATEWAY_API_KEY:
        api_key = request.headers.get("x-api-key")
        if api_key == GATEWAY_API_KEY:
            # Valid API key bypasses IP check
            return await call_next(request)

    # Check if IP matches any allowed prefix
    allowed = any(client_ip.startswith(prefix) for prefix in ALLOWED_IPS)

    if not allowed:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Forbidden", status_code=403)

    # Rate limiting (skip health/docs endpoints)
    if request.url.path.startswith("/api/v1/") and request.url.path not in ("/api/v1/health", "/api/v1/providers", "/api/v1/backends"):
        if not rate_limiter.allow(client_ip):
            from fastapi.responses import JSONResponse
            request_logger.log_request(
                session_id="rate_limited",
                ip=client_ip,
                event_type="rate_limit",
                error=f"429 {request.method} {request.url.path}",
            )
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

    return await call_next(request)


# ============== Cloud Provider Endpoints ==============

@app.post("/api/v1/conversations/{client_session_id}/note")
async def conversation_note(client_session_id: str, body: dict):
    """Append a ground-truth note (e.g. verified tool activity) to a conversation."""
    role = (body.get("role") or "tool")
    content = body.get("content") or ""
    sid = chat_logger.append_message(client_session_id, role, content)
    return {"ok": bool(sid), "session_id": sid}


@app.get("/api/v1/conversations/{client_session_id}/messages")
async def conversation_messages(client_session_id: str, limit: int = 40):
    """Persisted user/assistant history for a conversation. brin hydrates the
    tool loop from this so memory survives restarts."""
    return {"messages": chat_logger.load_conversation(client_session_id, limit)}


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, req: Request):
    """Unified entry: route by model. The model is <runtime>/<model>:
    claude-cli/sonnet runs on the local Claude CLI via a warm session,
    openrouter/openai/gpt-5 on OpenRouter as "openai/gpt-5". Callers send the id
    as stored and need not choose a provider; an explicit provider still wins if
    supplied, with the model taken as that provider's own id."""
    if request.provider:
        provider_name = request.provider.value
    else:
        try:
            provider_name, request.model = route_model(request.model, set(providers) | {"cli"})
        except UnknownRuntime as e:
            raise HTTPException(status_code=400, detail=str(e))

    if provider_name == "cli":
        # The local CLI is the runtime's. The body goes as it arrived; the
        # runtime routes the model id again.
        return await runtime_proxy.forward(req, "/api/v1/chat")

    provider = providers.get(provider_name)
    if not provider:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_name}")
    model_door.call_for(request.agent, request.session_id, request.service)

    # Validate model against whitelist. Allow any OpenRouter model
    # (provider-prefixed slugs like "anthropic/claude-sonnet-4-5") since
    # OpenRouter routes to many backend models that aren't worth pinning here.
    if request.model and provider_name != "openrouter":
        if not model_allowed(provider_name, request.model, provider_name):
            raise HTTPException(status_code=400, detail=f"Model '{request.model}' not allowed for {provider_name}")

    try:
        result = await provider.chat(
            request.messages,
            model=request.model,
            max_tokens=request.max_tokens,
            tools=request.tools,
            tool_choice=request.tool_choice,
            system=request.system,
            timeout=turn_limits.agent_turn_limit_seconds(request.agent),
        )
        # Best-effort central persistence of this turn to sn_core.* (never breaks the chat).
        try:
            chat_logger.log_chat_turn(
                client_session_id=request.session_id,
                agent_slug=request.agent,
                user=request.user,
                service=request.service,
                provider=runtime_of(provider_name),  # the runtime, as session rows name it
                model=request.model or provider.model,
                messages=[mm.model_dump() if hasattr(mm, 'model_dump') else mm for mm in request.messages],
                assistant_content=result.get('content', '') or '',
                usage=result.get('usage', {}),
                raw_user_content=request.raw_user_content,
                log_user=not request.continues_turn,
            )
        except Exception as _persist_err:  # noqa: BLE001 -- logging must never break chat
            logger.warning(f"chat persistence wrapper failed (ignored): {_persist_err}")
        return ChatResponse(
            id=result.get("id", str(uuid.uuid4())),
            provider=provider_name,
            model=request.model or provider.model,
            content=result.get("content", "") or "",
            usage=result.get("usage", {}),
            tool_calls=result.get("tool_calls"),
        )
    except model_door.Refused as refused:
        # The door's rules (model_door.check): structured, so the caller can
        # tell the person why instead of a bare 500.
        status, detail = refused.http_error()
        raise HTTPException(status_code=status, detail=detail)
    except TypeError:
        # Provider hasn't been upgraded to accept tools/system; fall back.
        result = await provider.chat(request.messages, model=request.model, max_tokens=request.max_tokens)
        # Best-effort central persistence of this turn to sn_core.* (never breaks the chat).
        try:
            chat_logger.log_chat_turn(
                client_session_id=request.session_id,
                agent_slug=request.agent,
                user=request.user,
                service=request.service,
                provider=runtime_of(provider_name),  # the runtime, as session rows name it
                model=request.model or provider.model,
                messages=[mm.model_dump() if hasattr(mm, 'model_dump') else mm for mm in request.messages],
                assistant_content=result.get('content', '') or '',
                usage=result.get('usage', {}),
                raw_user_content=request.raw_user_content,
                log_user=not request.continues_turn,
            )
        except Exception as _persist_err:  # noqa: BLE001 -- logging must never break chat
            logger.warning(f"chat persistence wrapper failed (ignored): {_persist_err}")
        return ChatResponse(
            id=result.get("id", str(uuid.uuid4())),
            provider=provider_name,
            model=request.model or provider.model,
            content=result.get("content", "") or "",
            usage=result.get("usage", {}),
        )
    except Exception as e:
        # An httpx timeout has an empty message, so the type is the only thing
        # that says what went wrong. Name it in the log and to the caller.
        logger.error("Chat error (%s): %s: %s", provider_name, type(e).__name__, e)
        raise HTTPException(status_code=500,
                            detail=f"chat request failed: {type(e).__name__}")


@app.post("/api/v1/chat/stream")
async def chat_stream(request: ChatRequest):
    """Stream response from cloud provider"""
    provider_name = request.provider.value
    provider = providers.get(provider_name)

    if not provider:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_name}")

    if request.model and provider_name != "openrouter":
        if not model_allowed(provider_name, request.model, provider_name):
            raise HTTPException(status_code=400, detail=f"Model '{request.model}' not allowed for {provider_name}")

    async def event_generator():
        model_door.call_for(request.agent, request.session_id, request.service)
        try:
            async for chunk in provider.chat_stream(request.messages, model=request.model):
                yield {"data": chunk}
        except Exception as e:
            yield {"data": {"error": str(e)}}

    return EventSourceResponse(event_generator())


# ============== Utility Endpoints ==============

@app.get("/api/v1/providers")
async def list_providers():
    """List available providers"""
    return {
        "providers": [
            {
                "id": "cli",
                "name": "Claude CLI",
                "type": "session",
                "models": ["haiku", "sonnet", "opus"],
                "features": ["file_access", "bash", "tools", "sessions"],
                "status": "available"
            },
            {
                "id": "anthropic",
                "name": "Anthropic API",
                "type": "stateless",
                "models": ["claude-3-haiku-20240307", "claude-3-sonnet-20240229", "claude-3-opus-20240229"],
                "features": [],
                "status": "available" if providers["anthropic"].api_key else "not_configured"
            },
            {
                "id": "gemini",
                "name": "Google Gemini",
                "type": "stateless",
                "models": ["gemini-pro", "gemini-ultra"],
                "features": [],
                "status": "available" if providers["gemini"].api_key else "not_configured"
            },
            {
                "id": "openrouter",
                "name": "OpenRouter",
                "type": "stateless",
                "models": ["anthropic/claude-3-haiku", "meta-llama/llama-3-70b", "mistralai/mixtral-8x7b"],
                "features": [],
                "status": "available" if providers["openrouter"].api_key else "not_configured"
            },
        ]
    }


@app.get("/api/v1/backends")
async def list_backends():
    """The upstreams this gateway can run an agent on, and whether each can run now.

    Unlike /api/v1/providers (a fixed display list from before the model catalog),
    this is read from backends.upstreams(), the same table resolve_backend
    dispatches on (UPSTREAMS plus the enabled cli_providers rows that run the
    Claude Code binary), so it cannot name a backend that does not exist or miss
    one that does. core-api validates new catalog rows against it and shows it
    to the configurator as the provider status.

    status: 'available' when the upstream can serve (CLI binary present; for http,
    a key stored at key_ref), else 'not_configured'. Key VALUES never leave here.
    """
    out = []
    for upstream, kind in sorted(upstreams().items()):
        reason = None
        if upstream == "flint-cli":
            key_ref = None
            try:
                rows = db.query(
                    "SELECT enabled, env_vars FROM sn_config.cli_providers WHERE slug = %s",
                    ("flint-cli",), fetch=True, as_dict=True,
                )
                row = rows[0] if rows else None
                provider_env = (row or {}).get("env_vars") or {}
                key_name = {
                    "openrouter": "OPENROUTER_API_KEY",
                    "openai": "OPENAI_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                }.get(provider_env.get("FLINT_PROVIDER"))
                key_value = provider_env.get(key_name) if key_name else None
                key_ref = (key_value[7:] if isinstance(key_value, str)
                           and key_value.startswith("secret:") else None)
                try:
                    has_key = bool(key_ref and secrets_store.resolve_value("secret:" + key_ref))
                except RuntimeError:
                    has_key = False
                binary = shutil.which(str(FLINT_PATH))
                ready = bool(FLINT_ENABLED and binary and flint_stdio_safe()
                             and row and row["enabled"] and has_key)
                if not FLINT_ENABLED:
                    reason = "Flint runtime is disabled (FLINT_ENABLED=false)"
                elif not binary:
                    reason = f"Flint binary is missing: {FLINT_PATH}"
                elif not flint_stdio_safe():
                    reason = "Flint binary lacks the ephemeral-key stdio safety check"
                elif not row or not row["enabled"]:
                    reason = "Flint CLI provider is not enabled in sn_config.cli_providers"
                elif not key_name or not key_ref:
                    reason = "Flint provider/key configuration is incomplete"
                elif not has_key:
                    reason = f"Flint provider key is missing at {key_ref or 'cli_providers.env_vars'}"
            except Exception as exc:
                ready = False
                reason = f"Flint provider configuration cannot be read: {exc}"
        elif kind == "cli":
            # claude-cli and every row that runs the Claude Code binary: the
            # runtime runs CLI_PATH for all of them (backends.runs_claude_code).
            ready = shutil.which(str(CLI_PATH)) is not None
            key_ref = None
            if not ready:
                reason = f"Claude Code binary is missing: {CLI_PATH}"
        else:
            key_ref = f"gateway/{upstream}-key"
            ready = secrets_store.get_provider_key(upstream) is not None
        out.append({
            "id": upstream,
            "kind": kind,
            "status": "available" if ready else "not_configured",
            "key_ref": key_ref,
            "reason": reason,
        })
    return {"backends": out}


@app.get("/api/v1/cache/stats")
async def cache_stats():
    """Get shadow cache statistics"""
    return shadow_cache.stats()


@app.get("/api/v1/health", response_model=HealthResponse)
async def health_check():
    """Check health of all providers"""
    # The CLI is the runtime's: its health says whether the binary is there,
    # how many sessions are open and how many claude PROCESSES are alive
    # (live_children, the count that once went wrong by eight orphans on a live node).
    # A runtime that does not answer is reported as such, not guessed.
    runtime = await runtime_proxy.get_json("/api/v1/health")
    if runtime is None:
        cli_ok = False
        provider_health = {"cli": {"status": "unavailable",
                                   "detail": f"runtime not reachable at {RUNTIME_URL}"}}
    else:
        cli_ok = runtime.get("status") == "ok"
        provider_health = {"cli": {
            "status": runtime.get("status") or "unavailable",
            "active_sessions": runtime.get("active_sessions"),
            "live_children": runtime.get("live_children"),
        }}

    # Check cloud providers (quick check - just verify API key exists)
    for name, provider in providers.items():
        if provider.api_key:
            provider_health[name] = {"status": "ok"}
        else:
            provider_health[name] = {"status": "not_configured"}

    # Healthy if any backend can actually serve: CLI present, or a cloud provider configured.
    any_cloud = any(p.get("status") == "ok" for n, p in provider_health.items() if n != "cli")
    overall = "healthy" if (cli_ok or any_cloud) else "degraded"

    return HealthResponse(status=overall, providers=provider_health)


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    """WebSocket endpoint for real-time log streaming to dashboards"""
    await websocket.accept()
    queue = asyncio.Queue(maxsize=1000)
    request_logger.add_ws_client(queue)
    try:
        # Send active sessions on connect
        listed = await runtime_proxy.get_json("/api/v1/sessions")
        if listed is not None:
            await websocket.send_json({"type": "active_sessions", "data": listed.get("sessions", [])})

        while True:
            entry = await queue.get()
            await websocket.send_json({"type": "log_entry", "data": entry})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
    finally:
        request_logger.remove_ws_client(queue)


@app.get("/api/v1/usage")
async def get_usage():
    """Get CLI usage statistics and limits"""
    if not ENABLE_USAGE_ENDPOINT:
        raise HTTPException(status_code=404, detail="Not found")
    import os
    from datetime import date, datetime, timedelta

    claude_dir = Path(os.path.expanduser("~/.claude"))
    result = {
        "subscription": None,
        "rate_limit_tier": None,
        "today": None,
        "recent_days": [],
        "week": {"messages": 0, "tool_calls": 0, "sessions": 0},
        "totals": {"messages": 0, "sessions": 0, "tool_calls": 0}
    }

    # Read credentials for subscription info (no secrets)
    creds_file = claude_dir / ".credentials.json"
    if creds_file.exists():
        try:
            import json
            creds = json.loads(creds_file.read_text())
            oauth = creds.get("claudeAiOauth", {})
            result["subscription"] = oauth.get("subscriptionType")
            result["rate_limit_tier"] = oauth.get("rateLimitTier")
        except Exception:
            pass


    # Read stats cache for daily activity
    stats_file = claude_dir / "stats-cache.json"
    if stats_file.exists():
        try:
            import json
            stats = json.loads(stats_file.read_text())
            daily = stats.get("dailyActivity", [])

            today_str = date.today().isoformat()
            week_ago = (date.today() - timedelta(days=7)).isoformat()

            for day in reversed(daily):
                day_date = day.get("date", "")

                if day_date == today_str:
                    result["today"] = day

                # Last 7 days for display
                if len(result["recent_days"]) < 7:
                    result["recent_days"].append(day)

                # Sum last 7 days
                if day_date >= week_ago:
                    result["week"]["messages"] += day.get("messageCount", 0)
                    result["week"]["tool_calls"] += day.get("toolCallCount", 0)
                    result["week"]["sessions"] += day.get("sessionCount", 0)

                # All time totals
                result["totals"]["messages"] += day.get("messageCount", 0)
                result["totals"]["sessions"] += day.get("sessionCount", 0)
                result["totals"]["tool_calls"] += day.get("toolCallCount", 0)
        except Exception:
            pass

    # Add active sessions from the runtime
    runtime = await runtime_proxy.get_json("/api/v1/health")
    result["active_sessions"] = (runtime or {}).get("active_sessions", 0)

    return result


@app.get("/", response_class=HTMLResponse)
async def root():
    """Display README as HTML"""
    readme_path = Path(__file__).parent / "README.md"
    try:
        content = readme_path.read_text()
    except Exception:
        content = "# AI Gateway\n\nREADME not found"

    # Convert markdown to simple HTML
    import re
    html_content = content
    # Headers
    html_content = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html_content, flags=re.MULTILINE)
    # Code blocks
    html_content = re.sub(r'```(\w+)?\n(.*?)```', r'<pre><code>\2</code></pre>', html_content, flags=re.DOTALL)
    # Inline code
    html_content = re.sub(r'`([^`]+)`', r'<code>\1</code>', html_content)
    # Bold
    html_content = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', html_content)
    # Lists
    html_content = re.sub(r'^- \[x\] (.+)$', r'<li>[x] \1</li>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^- \[ \] (.+)$', r'<li>[ ] \1</li>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^- (.+)$', r'<li>\1</li>', html_content, flags=re.MULTILINE)
    # Paragraphs
    html_content = re.sub(r'\n\n', '</p><p>', html_content)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AI Gateway</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
               max-width: 900px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #24292e; }}
        h1, h2, h3 {{ border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }}
        pre {{ background: #f6f8fa; padding: 16px; overflow: auto; border-radius: 6px; }}
        code {{ background: #f6f8fa; padding: 0.2em 0.4em; border-radius: 3px; font-size: 85%; }}
        pre code {{ background: none; padding: 0; }}
        li {{ margin: 4px 0; }}
        a {{ color: #0366d6; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <p>{html_content}</p>
</body>
</html>"""
    return html


# ============== Run ==============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
