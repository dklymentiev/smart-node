"""Smart Node core-api -- write-side controller for SN admin entities.

v0.1.0 covered Tools registry (Stage B1).
v0.2.0 adds Integrations registry (B2-Integrations phase 2) + secrets store (now sn_config.secrets).
v0.3.0 adds Events log + emit() helper.
v0.4.0 adds MCP servers registry (manual catalog + bulk tools upsert).
v0.5.0 adds cron registry CRUD + cron-runner event emission.
"""

from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from db import init_pool, close_pool
import event_stream
from events_emit import emit
from db import cursor
from lib import secret_store_import, system_chat
from routes import contacts, tools, agents, specialists, integrations, runtimes, secrets, events, mcp, crons, system, policies, ai, messages, references, departments, database, sessions, gateway_secrets, gateway_chatgpt, models, settings, knowledge, desks, workflows, users, notifications, google, provision, planner, projects, setup, budgets, test_nodes, attachments, session_context, context_status, work, limits, actor_credentials, hitl, memory, node_settings, project_state
from routes import task_attachments
from routes import reminders
from version import __version__
from permissions import undeclared_route, validate_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("core-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    # Strict: every route declares a permission, so a route added without one
    # stops the service here, where an installer sees it, instead of answering
    # 403 to whoever calls it later. The middleware below stays as the guard
    # for a route added while the service is running.
    validate_routes(app, strict_undeclared=True)
    # Emit service.health_ok on startup so the events log shows the boot moment.
    try:
        with cursor(commit=True) as cur:
            emit(
                cur,
                actor="component:core-api",
                event_type="service.health_ok",
                entity_type="service",
                entity_id="core-api",
                payload={"version": __version__},
            )
    except Exception as e:
        log.warning("startup event emit skipped: %s", e)
    secrets.warn_legacy_writer_env()
    # The sops-era file becomes rows of the one store, once; see the module.
    secret_store_import.import_file()
    # Reminders fire from here: one process, no extra unit or cron to install.
    reminder_task = asyncio.create_task(reminders.fire_loop())
    system_chat_task = asyncio.create_task(system_chat.watch_loop())
    # The live event stream's one listener (event_stream.py).
    stream_task = asyncio.create_task(event_stream.listen_loop())
    log.info("core-api ready")
    yield
    reminder_task.cancel()
    system_chat_task.cancel()
    stream_task.cancel()
    close_pool()


app = FastAPI(
    title="Smart Node core-api",
    version=__version__,
    docs_url="/api/_docs",
    redoc_url=None,
    openapi_url="/api/_openapi.json",
    lifespan=lifespan,
)


class DenyUndeclaredRoutes:
    """Refuse a route that declares no permission, before it runs.

    A plain ASGI middleware on purpose, NOT `@app.middleware("http")`. That
    decorator runs the handler behind a `receive` of Starlette's own, and
    auth.peer_credentials reaches the unix socket through the receive uvicorn
    passed in. Behind the decorator every caller on the socket lost its kernel
    identity and became "untrusted": root, the installer and every agent. An
    update stopped at the installer's first call. This class hands `receive`
    through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = undeclared_route(Request(scope))
            if path is not None:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": f"no permission declared for route {path}"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(DenyUndeclaredRoutes)

app.include_router(tools.router, prefix="/api/tools", tags=["tools"])
app.include_router(workflows.router, prefix="/api/workflows", tags=["workflows"])
app.include_router(agents.router)
app.include_router(specialists.router)
app.include_router(integrations.router)
app.include_router(runtimes.router)
app.include_router(secrets.router, prefix="/api/secrets", tags=["secrets"])
app.include_router(events.router, prefix="/api/events", tags=["events"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(mcp.router)
app.include_router(crons.router)
app.include_router(policies.router, prefix="/api/policies", tags=["policies"])
app.include_router(references.router)
app.include_router(departments.router)
app.include_router(contacts.router)
app.include_router(contacts.agent_contacts_router)
app.include_router(ai.router)
app.include_router(budgets.router)
app.include_router(limits.router)
app.include_router(gateway_secrets.router)
app.include_router(gateway_chatgpt.router)
app.include_router(messages.router)
app.include_router(attachments.router)
# Before the planner proxy: it answers /api/planner/.../tasks/{id}/attachments itself.
app.include_router(task_attachments.router)
app.include_router(reminders.router)
app.include_router(planner.router)
app.include_router(hitl.router)
app.include_router(projects.router)
app.include_router(database.router)
app.include_router(sessions.agent_sessions_router)
app.include_router(sessions.sessions_router)
app.include_router(models.router)
app.include_router(settings.router)
app.include_router(session_context.router)
app.include_router(context_status.router)
app.include_router(knowledge.kinds_router)
app.include_router(knowledge.agent_knowledge_router)
app.include_router(desks.router)
app.include_router(memory.router)
app.include_router(node_settings.router)
app.include_router(users.router)
app.include_router(notifications.router)
app.include_router(google.router)
app.include_router(provision.router, prefix="/api/provision", tags=["provision"])
app.include_router(test_nodes.router)
app.include_router(setup.router)
app.include_router(actor_credentials.router)
app.include_router(work.router, prefix="/api/work", tags=["work"])
app.include_router(project_state.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}


@app.get("/api")
def root():
    return {
        "service": "smart-node-core-api",
        "version": __version__,
        "endpoints": [
            "/api/health",
            "/api/tools",
            "/api/integrations",
            "/api/runtimes",
            "/api/secrets",
            "/api/events",
            "/api/mcp",
            "/api/crons",
            "/api/_docs",
        ],
    }
