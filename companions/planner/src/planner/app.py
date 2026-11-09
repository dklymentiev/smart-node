"""FastAPI application factory.

Registers per-resource routers, sets up an async-context
lifespan that opens and closes the DB pool, and wires uniform error handlers.

Health endpoint reports two facts:

- ``db_ok`` -- can we round-trip ``SELECT 1`` against the pool?
- ``migrations_applied`` -- is the ``_migrations`` table present and non-empty?

Both must be true for the service to be considered healthy.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from planner import __version__
from planner import statuses as _st
from planner.auth import (
    ApiKey,
    ensure_dev_actor,
    ensure_root_key,
    get_auth_mode,
    require_api_key,
)
from planner.db import lifespan as db_lifespan
from planner.notify import run_stale_in_progress_scanner, run_stale_pending_review_scanner
from planner.routers import actors as actors_router
from planner.routers import ancillary as ancillary_router
from planner.routers import dashboard as dashboard_router
from planner.routers import dictionaries as dictionaries_router
from planner.routers import maintenance as maintenance_router
from planner.routers import objectives as objectives_router
from planner.routers import projects as projects_router
from planner.routers import tasks as tasks_router
from planner.repos.actors import ActorError
from planner.repos.ancillary import AncillaryError
from planner.repos.objectives import ObjectiveError
from planner.repos.projects import ProjectError
from planner.repos.tasks import TaskError

logger = logging.getLogger(__name__)


def _problem(status_code: int, title: str, detail: str | None = None) -> JSONResponse:
    """RFC 7807-shaped error body. Concise, easy to consume from any client."""
    body = {"status": status_code, "title": title}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content=body)


@asynccontextmanager
async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with db_lifespan() as pool:
        app.state.pool = pool
        mode = get_auth_mode()
        app.state.auth_mode = mode
        print(f"Planner: auth mode = {mode}", flush=True)

        # `dev` actor is harmless in any mode -- seed it always so a
        # later mode switch does not require a manual INSERT.
        try:
            await ensure_dev_actor(pool)
        except Exception as exc:
            logger.warning("dev actor bootstrap skipped: %s", exc)

        # Root API key is only meaningful in `apikey` mode. In `dev` /
        # `proxy` modes there is no login screen to paste it into.
        if mode == "apikey":
            try:
                plaintext = await ensure_root_key(pool)
            except Exception as exc:
                logger.warning("root key bootstrap skipped: %s", exc)
            else:
                if plaintext:
                    # Print exactly once. Operator must capture it now.
                    print("=" * 60, flush=True)
                    print("Planner: generated root API key (label=root, actor=system)", flush=True)
                    print(f"  X-API-Key: {plaintext}", flush=True)
                    print("This is the ONLY time the plaintext will be shown.", flush=True)
                    print("=" * 60, flush=True)

        # Stale pending_review scanner: runs in background for the app lifetime.
        scanner_task = asyncio.create_task(
            run_stale_pending_review_scanner(pool),
            name="stale-pending-review-scanner",
        )
        # Stale in_progress watchdog: the clock for a task whose agent stopped.
        in_progress_task = asyncio.create_task(
            run_stale_in_progress_scanner(pool),
            name="stale-in-progress-scanner",
        )
        try:
            yield
        finally:
            for task in (scanner_task, in_progress_task):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


#: How the database words its refusal to start work on a task with no doer.
_NEEDS_DOER = "a task needs a doer"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Planner",
        description=(
            "Self-hostable task management with a six-stage pipeline, "
            "objective hierarchy, and CLI / REST / MCP / UI parity."
        ),
        version=__version__,
        lifespan=_app_lifespan,
    )

    # ---- Error handlers ----------------------------------------------------

    @app.exception_handler(ActorError)
    async def _actor_err(_: Request, exc: ActorError) -> JSONResponse:
        return _problem(400, "invalid actor input", str(exc))

    @app.exception_handler(ProjectError)
    async def _project_err(_: Request, exc: ProjectError) -> JSONResponse:
        return _problem(400, "invalid project input", str(exc))

    @app.exception_handler(ObjectiveError)
    async def _objective_err(_: Request, exc: ObjectiveError) -> JSONResponse:
        return _problem(400, "invalid objective input", str(exc))

    @app.exception_handler(TaskError)
    async def _task_err(_: Request, exc: TaskError) -> JSONResponse:
        if str(exc).startswith(_NEEDS_DOER):
            return _problem(422, "a task needs a doer", str(exc))
        return _problem(400, "invalid task input", str(exc))

    @app.exception_handler(asyncpg.CheckViolationError)
    async def _check_violation(_: Request, exc: asyncpg.CheckViolationError) -> JSONResponse:
        # The database refuses work on a task with nobody to do it (migration 0013).
        # Any other check violation is not ours to explain: it stays the server error
        # it always was.
        msg = str(exc).splitlines()[0] if str(exc) else ""
        if msg.startswith(_NEEDS_DOER):
            return _problem(422, "a task needs a doer", msg)
        raise exc

    @app.exception_handler(AncillaryError)
    async def _ancillary_err(_: Request, exc: AncillaryError) -> JSONResponse:
        return _problem(400, "invalid input", str(exc))

    @app.exception_handler(HTTPException)
    async def _http_err(_: Request, exc: HTTPException) -> JSONResponse:
        # exc.detail carries the human-readable message (e.g. "missing X-API-Key").
        # Render it in both `title` (short summary) and `detail` (RFC 7807 long form).
        msg = str(exc.detail) if exc.detail else "error"
        return _problem(exc.status_code, msg, msg)

    @app.exception_handler(Exception)
    async def _fallback(_: Request, exc: Exception) -> JSONResponse:
        # Production logging is wired in main.py; here we only ensure the
        # client gets a well-shaped 500 instead of a stack trace.
        return _problem(500, "internal error", str(exc))

    # ---- Health ------------------------------------------------------------

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        pool: asyncpg.Pool = app.state.pool
        db_ok = False
        migrations_applied = 0
        try:
            async with pool.acquire() as conn:
                db_ok = (await conn.fetchval("SELECT 1")) == 1
                migrations_applied = await conn.fetchval(
                    "SELECT COUNT(*) FROM _migrations"
                )
        except Exception:
            db_ok = False
        ok = db_ok and migrations_applied > 0
        return {
            "status": "ok" if ok else "degraded",
            "version": __version__,
            "db_ok": db_ok,
            "migrations_applied": migrations_applied,
        }

    @app.get("/", tags=["meta"])
    async def root() -> dict:
        return {
            "name": "Planner",
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
        }

    @app.get("/whoami", tags=["meta"])
    async def whoami(api_key: ApiKey = Depends(require_api_key)) -> dict:
        return {
            "actor_id": api_key.actor_id,
            "label": api_key.label,
            "key_id": api_key.id,
        }

    # ---- Routers -----------------------------------------------------------
    @app.middleware("http")
    async def _status_vocabulary(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Which status words this request wants back (planner.statuses).

        ``X-Status-Vocabulary: new`` (or ``?vocabulary=new``) makes ``status`` the
        three-valued state in every task of the response; ``legacy`` the old
        one-word stage; nothing: the node default. Per request, so a consumer
        changes when it is ready and the others are not touched.
        """
        asked = _st.normalize_vocabulary(
            request.headers.get(_st.VOCABULARY_HEADER) or request.query_params.get("vocabulary"))
        token = _st.REQUEST_VOCABULARY.set(asked)
        try:
            return await call_next(request)
        finally:
            _st.REQUEST_VOCABULARY.reset(token)

    app.include_router(actors_router.router)
    app.include_router(projects_router.router)
    app.include_router(objectives_router.router)
    app.include_router(tasks_router.router)
    app.include_router(dictionaries_router.router)
    app.include_router(ancillary_router.router)
    app.include_router(dashboard_router.router)
    app.include_router(maintenance_router.router)

    # Web UI -- imported lazily so the optional jinja2 dep stays optional
    # if a deployment is API-only.
    try:
        from planner.ui.router import router as ui_router

        app.include_router(ui_router)
    except ImportError:
        logger.info("UI not available: jinja2 not installed")

    return app


app = create_app()
