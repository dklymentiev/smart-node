"""Web UI routes.

Renders Jinja2 templates from ``planner.ui.templates``. Each handler reads
through the same repos the REST layer uses, so the UI is automatically in
parity with the API.

Authentication: pages require a ``planner_key`` cookie (or ``?key=...``
query parameter once, after which we set the cookie). The cookie holds the
same plaintext API key used by REST.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Union

import asyncpg
from fastapi import APIRouter, Cookie, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from planner import __version__
from planner.auth import (
    ApiKey,
    ApiKeysRepo,
    DEV_ACTOR_ID,
    _ensure_proxy_actor,
    _normalize_proxy_user,
    _proxy_header_name,
    _synthetic_apikey,
    get_auth_mode,
)
from planner.pipeline import Pipeline, PipelineError, allowed_from
from planner.repos.ancillary import (
    CommentsRepo,
    LinksRepo,
    LogRepo,
    RelationsRepo,
    TagsRepo,
)
from planner.repos.actors import ActorsRepo
from planner.repos.dashboard import DashboardRepo
from planner.repos.objectives import ObjectivesRepo
from planner.repos.projects import ProjectsRepo
from planner.repos.tasks import TasksRepo
from planner.ui import charts

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)

_COOKIE_NAME = "planner_key"


async def _resolve_session(
    request: Request,
    cookie_key: str | None,
    query_key: str | None,
) -> ApiKey | None:
    """Resolve the request's actor for browser callers (DD-008).

    In ``dev`` mode, every request is the seeded ``dev`` actor.
    In ``proxy`` mode, the trusted ``Remote-User`` header decides; missing
    or invalid header returns None (caller will 303 to /ui/login as a
    diagnostic, even though the form will not help).
    In ``apikey`` mode, cookie or one-shot ``?key=`` query is looked up
    against the ``api_keys`` table.
    """
    mode = get_auth_mode()

    if mode == "dev":
        return _synthetic_apikey(DEV_ACTOR_ID, "dev-auto")

    pool: asyncpg.Pool = request.app.state.pool

    if mode == "proxy":
        header = _proxy_header_name()
        raw = request.headers.get(header, "")
        actor_id = _normalize_proxy_user(raw)
        if actor_id is None:
            return None
        await _ensure_proxy_actor(pool, actor_id)
        return _synthetic_apikey(actor_id, f"proxy:{actor_id}")

    # mode == "apikey"
    plaintext = cookie_key or query_key
    if not plaintext:
        return None
    return await ApiKeysRepo(pool).lookup_by_plaintext(plaintext)


def _redirect_to_login() -> RedirectResponse:
    # 303 so any client coming from a POST follows with GET. In `dev` /
    # `proxy` modes _resolve_session never returns None, so this is only
    # reached in `apikey` mode.
    return RedirectResponse(url="/ui/login", status_code=303)


@router.get("/ui/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request) -> Union[HTMLResponse, RedirectResponse]:
    # In `dev` / `proxy` modes there is no password to type; redirect to
    # the dashboard so the first hit lands where the operator expects.
    if get_auth_mode() != "apikey":
        return RedirectResponse(url="/ui/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"version": __version__, "page_title": "Sign in"},
    )


@router.post("/ui/login")
async def login_submit(request: Request) -> RedirectResponse:
    # Form submission is meaningful only in `apikey` mode.
    if get_auth_mode() != "apikey":
        return RedirectResponse(url="/ui/", status_code=303)
    pool: asyncpg.Pool = request.app.state.pool
    form = await request.form()
    plaintext = (form.get("key") or "").strip()
    api_key = (
        await ApiKeysRepo(pool).lookup_by_plaintext(plaintext) if plaintext else None
    )
    if api_key is None:
        return RedirectResponse(url="/ui/login?error=invalid", status_code=303)
    response = RedirectResponse(url="/ui/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME,
        plaintext,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@router.post("/ui/logout")
async def logout() -> RedirectResponse:
    # In `dev` / `proxy` the cookie is irrelevant; we still clear it
    # in case the operator switched modes after logging in.
    target = "/ui/login" if get_auth_mode() == "apikey" else "/ui/"
    response = RedirectResponse(url=target, status_code=303)
    response.delete_cookie(_COOKIE_NAME)
    return response


@router.get("/ui/", response_class=HTMLResponse, response_model=None)
async def dashboard(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "Dashboard",
        },
    )


# ---------------------------------------------------------------------------
# Dashboard partials
#
# Each partial is a thin renderer: fetch a read-model from DashboardRepo,
# render a template. Aggregation SQL lives in repos/dashboard.py; chart
# geometry lives in ui/charts.py; the same read-models are exposed as JSON
# by routers/dashboard.py.
# ---------------------------------------------------------------------------

_RECENT_TASKS_LIMIT = 15


def _humanize_age(ts: datetime | None) -> str:
    """Compact relative age, e.g. ``3h`` / ``5d``. Registered as the Jinja
    ``age`` filter so templates format timestamps with no Python-side prep."""
    if ts is None:
        return ""
    delta = datetime.now(timezone.utc) - ts
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "<1h"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


templates.env.filters["age"] = _humanize_age


@router.get(
    "/ui/_partials/projects", response_class=HTMLResponse, response_model=None
)
async def projects_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    projects = await DashboardRepo(pool).project_progress()
    return templates.TemplateResponse(
        request, "_partials/projects.html", {"projects": projects}
    )


@router.get(
    "/ui/_partials/task_flow_chart",
    response_class=HTMLResponse,
    response_model=None,
)
async def task_flow_chart_partial(
    request: Request,
    range: int = Query(default=30),
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    days = range if range in charts.FLOW_CHART_RANGES else 30
    series = await DashboardRepo(pool).task_flow_series(days)
    chart = charts.build_flow_chart(series)
    return templates.TemplateResponse(
        request,
        "_partials/task_flow_chart.html",
        {"chart": chart, "days": days, "ranges": charts.FLOW_CHART_RANGES},
    )


@router.get(
    "/ui/_partials/recent_tasks",
    response_class=HTMLResponse,
    response_model=None,
)
async def recent_tasks_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    tasks = await DashboardRepo(pool).recent_tasks(_RECENT_TASKS_LIMIT)
    return templates.TemplateResponse(
        request, "_partials/recent_tasks.html", {"tasks": tasks}
    )


@router.get(
    "/ui/_partials/goals",
    response_class=HTMLResponse,
    response_model=None,
)
async def goals_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    items = await DashboardRepo(pool).objective_progress("goal")
    return templates.TemplateResponse(
        request,
        "_partials/objective_rings.html",
        {"items": items, "kind": "goal", "ring_circ": charts.RING_CIRC},
    )


@router.get(
    "/ui/_partials/strategies",
    response_class=HTMLResponse,
    response_model=None,
)
async def strategies_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    items = await DashboardRepo(pool).objective_progress("strategy")
    return templates.TemplateResponse(
        request,
        "_partials/objective_rings.html",
        {"items": items, "kind": "strategy", "ring_circ": charts.RING_CIRC},
    )


@router.get(
    "/ui/_partials/attention",
    response_class=HTMLResponse,
    response_model=None,
)
async def attention_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    tasks = await DashboardRepo(pool).attention_tasks()
    return templates.TemplateResponse(
        request, "_partials/attention.html", {"tasks": tasks}
    )


@router.get(
    "/ui/_partials/my_tasks",
    response_class=HTMLResponse,
    response_model=None,
)
async def my_tasks_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    tasks = await DashboardRepo(pool).tasks_for_actor(api_key.actor_id)
    return templates.TemplateResponse(
        request,
        "_partials/my_tasks.html",
        {"tasks": tasks, "actor_id": api_key.actor_id},
    )


@router.get(
    "/ui/_partials/needs_review",
    response_class=HTMLResponse,
    response_model=None,
)
async def needs_review_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    tasks = await DashboardRepo(pool).tasks_in_review()
    return templates.TemplateResponse(
        request, "_partials/needs_review.html", {"tasks": tasks}
    )


@router.get(
    "/ui/_partials/agents_working",
    response_class=HTMLResponse,
    response_model=None,
)
async def agents_working_partial(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    tasks = await DashboardRepo(pool).agent_tasks()
    return templates.TemplateResponse(
        request, "_partials/agents_working.html", {"tasks": tasks}
    )


# ---------------------------------------------------------------------------
# Task board (kanban)
# ---------------------------------------------------------------------------

#: Pipeline columns shown on the kanban board, left-to-right.
BOARD_COLUMNS: list[tuple[str, str]] = [
    ("open", "Open"),
    ("awaiting_approval", "Awaiting approval"),
    ("in_progress", "In progress"),
    ("pending_review", "Pending review"),
    ("done", "Done"),
]


async def _board_data(pool: asyncpg.Pool, slug: str) -> dict | None:
    """Return ``{"project": ..., "columns": [(status, label, [tasks])]}`` or
    ``None`` if the project does not exist."""
    project = await ProjectsRepo(pool).get_by_slug(slug)
    if project is None:
        return None
    repo = TasksRepo(pool)
    tags_repo = TagsRepo(pool)
    columns: list[tuple[str, str, list[dict]]] = []
    for status, label in BOARD_COLUMNS:
        tasks = await repo.list(
            project_id=project.id, status=status, limit=200
        )
        cards = []
        for t in tasks:
            tags = await tags_repo.list_for_task(t.id)
            cards.append(
                {
                    "id": t.id,
                    "title": t.title,
                    "assignee": t.assignee,
                    "priority": t.priority,
                    "eta": t.eta,
                    "tags": tags,
                }
            )
        columns.append((status, label, cards))
    return {"project": project, "columns": columns}


@router.get(
    "/ui/projects",
    response_class=HTMLResponse,
    response_model=None,
)
async def projects_index(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    rows = await pool.fetch(
        """
        SELECT p.id, p.slug, p.title, p.description, p.status, p.updated_at,
               COUNT(t.id) AS total,
               COUNT(t.id) FILTER (WHERE t.stage = 'done') AS done,
               COUNT(t.id) FILTER (WHERE t.stage NOT IN
                   ('done', 'rejected', 'incident')) AS active
          FROM projects p
          LEFT JOIN tasks t ON t.project_id = p.id
         GROUP BY p.id
         ORDER BY (p.status = 'archived'), p.sort_order, p.slug
        """
    )
    projects = []
    for r in rows:
        total = r["total"] or 0
        done = r["done"] or 0
        projects.append(
            {
                "slug": r["slug"],
                "title": r["title"],
                "description": r["description"],
                "status": r["status"],
                "total": total,
                "done": done,
                "active": r["active"] or 0,
                "pct": int(round(100 * done / total)) if total else 0,
                "updated_at": r["updated_at"],
            }
        )
    return templates.TemplateResponse(
        request,
        "projects_index.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "Projects",
            "projects": projects,
        },
    )


@router.get(
    "/ui/projects/{slug}",
    response_class=HTMLResponse,
    response_model=None,
)
async def project_overview(
    request: Request,
    slug: str,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    project = await ProjectsRepo(pool).get_by_slug(slug)
    if project is None:
        return HTMLResponse(
            templates.get_template("not_found.html").render(
                request=request,
                version=__version__,
                actor_id=api_key.actor_id,
                what=f"Project {slug!r}",
                page_title="Not found",
            ),
            status_code=404,
        )

    status_rows = await pool.fetch(
        "SELECT stage AS status, COUNT(*) AS n FROM tasks WHERE project_id = $1 GROUP BY stage",
        project.id,
    )
    by_status = {r["status"]: r["n"] for r in status_rows}
    task_total = sum(by_status.values())
    task_done = by_status.get("done", 0)
    task_active = by_status.get("in_progress", 0) + by_status.get("pending_review", 0)
    task_open = by_status.get("open", 0) + by_status.get("assessing", 0) + by_status.get("awaiting_approval", 0)
    task_pct = int(round(100 * task_done / task_total)) if task_total else 0

    objective_total = await pool.fetchval(
        "SELECT COUNT(*) FROM objectives WHERE project_id = $1", project.id
    )
    recent_tasks = await pool.fetch(
        """
        SELECT id, title, stage AS status, updated_at
        FROM tasks
        WHERE project_id = $1
        ORDER BY updated_at DESC
        LIMIT 8
        """,
        project.id,
    )

    return templates.TemplateResponse(
        request,
        "project_overview.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": project.title,
            "project": project,
            "active_tab": "overview",
            "task_total": task_total,
            "task_done": task_done,
            "task_active": task_active,
            "task_open": task_open,
            "task_pct": task_pct,
            "objective_total": objective_total,
            "recent_tasks": recent_tasks,
        },
    )


@router.get(
    "/ui/projects/{slug}/board",
    response_class=HTMLResponse,
    response_model=None,
)
async def project_board(
    request: Request,
    slug: str,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    data = await _board_data(pool, slug)
    if data is None:
        return HTMLResponse(
            templates.get_template("not_found.html").render(
                request=request,
                version=__version__,
                actor_id=api_key.actor_id,
                what=f"Project {slug!r}",
                page_title="Not found",
            ),
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "board.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": data["project"].title,
            "project": data["project"],
            "columns": data["columns"],
            "active_tab": "board",
        },
    )


@router.get(
    "/ui/projects/{slug}/_board",
    response_class=HTMLResponse,
    response_model=None,
)
async def project_board_partial(
    request: Request,
    slug: str,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    data = await _board_data(pool, slug)
    if data is None:
        return HTMLResponse("<div>project not found</div>", status_code=404)
    return templates.TemplateResponse(
        request,
        "_partials/board.html",
        {"columns": data["columns"]},
    )


# ---------------------------------------------------------------------------
# Task detail
# ---------------------------------------------------------------------------


async def _task_detail_data(pool: asyncpg.Pool, task_id: int) -> dict | None:
    repo = TasksRepo(pool)
    task = await repo.get(task_id)
    if task is None:
        return None
    project = (
        await ProjectsRepo(pool).get(task.project_id) if task.project_id else None
    )
    comments = await CommentsRepo(pool).list_for_task(task_id)
    log = await LogRepo(pool).list_for_task(task_id, limit=200)
    tags = await TagsRepo(pool).list_for_task(task_id)
    links = await LinksRepo(pool).list_for_task(task_id)
    rels_out = await RelationsRepo(pool).list_for_task(task_id)
    rels_in = await RelationsRepo(pool).list_for_task(task_id, as_target=True)
    transitions = sorted(allowed_from(task.status))
    projects = await ProjectsRepo(pool).list()
    objectives = await ObjectivesRepo(pool).list()
    actors = await ActorsRepo(pool).list(active=True)
    return {
        "task": task,
        "project": project,
        "comments": comments,
        "log": log,
        "tags": tags,
        "links": links,
        "relations_out": rels_out,
        "relations_in": rels_in,
        "transitions": transitions,
        "projects": projects,
        "objectives": objectives,
        "actors": actors,
        "all_priorities": ("critical", "high", "medium", "low"),
        "all_complexities": ("simple", "moderate", "complex", "unknown"),
        "all_blocked_reasons": (
            "task_dependency",
            "external_approval",
            "scheduled_time",
        ),
    }


@router.get(
    "/ui/tasks/new", response_class=HTMLResponse, response_model=None
)
async def task_new_page(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    projects = await ProjectsRepo(pool).list()
    objectives = await ObjectivesRepo(pool).list()
    actors = await ActorsRepo(pool).list(active=True)
    return templates.TemplateResponse(
        request,
        "task_new.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "New task",
            "projects": projects,
            "objectives": objectives,
            "actors": actors,
            "all_priorities": ("critical", "high", "medium", "low"),
            "all_complexities": ("simple", "moderate", "complex", "unknown"),
            "error": None,
        },
    )


@router.post(
    "/ui/tasks/new", response_class=HTMLResponse, response_model=None
)
async def task_new_submit(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    form = await request.form()

    title = (form.get("title") or "").strip()
    if not title:
        return RedirectResponse(url="/ui/tasks/new?error=title", status_code=303)

    fields: dict[str, object] = {"title": title}
    for name in ("description", "assignee", "requester", "scope", "eta"):
        raw = (form.get(name) or "").strip()
        if raw:
            fields[name] = raw
    for name in ("project_id", "objective_id"):
        raw = (form.get(name) or "").strip()
        if raw and raw.lstrip("-").isdigit():
            fields[name] = int(raw)
    pri = (form.get("priority") or "").strip()
    if pri:
        fields["priority"] = pri
    cplx = (form.get("complexity_hint") or "").strip()
    if cplx:
        fields["complexity_hint"] = cplx
    raw_due = (form.get("due_date") or "").strip()
    if raw_due:
        try:
            fields["due_date"] = date.fromisoformat(raw_due)
        except ValueError:
            pass

    try:
        task = await TasksRepo(pool).create(actor_id=api_key.actor_id, **fields)  # type: ignore[arg-type]
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "task_new.html",
            {
                "version": __version__,
                "actor_id": api_key.actor_id,
                "page_title": "New task",
                "projects": await ProjectsRepo(pool).list(),
                "objectives": await ObjectivesRepo(pool).list(),
                "actors": await ActorsRepo(pool).list(active=True),
                "all_priorities": ("critical", "high", "medium", "low"),
                "all_complexities": ("simple", "moderate", "complex", "unknown"),
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/ui/tasks/{task.id}", status_code=303)


@router.get(
    "/ui/tasks/{task_id}", response_class=HTMLResponse, response_model=None
)
async def task_detail(
    request: Request,
    task_id: int,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    data = await _task_detail_data(pool, task_id)
    if data is None:
        return HTMLResponse(
            templates.get_template("not_found.html").render(
                request=request,
                version=__version__,
                actor_id=api_key.actor_id,
                what=f"Task #{task_id}",
                page_title="Not found",
            ),
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": data["task"].title,
            **data,
        },
    )


def _detail_partial(request: Request, data: dict) -> HTMLResponse:
    """Render the inner detail body (everything below the page header).

    HTMX form posts target ``#task-detail`` and swap-in this partial,
    refreshing comments/log/tags/transitions in one request.
    """
    return templates.TemplateResponse(
        request, "_partials/task_detail.html", data
    )


@router.post(
    "/ui/tasks/{task_id}/comment",
    response_class=HTMLResponse,
    response_model=None,
)
async def task_add_comment(
    request: Request,
    task_id: int,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    form = await request.form()
    content = (form.get("content") or "").strip()
    if content:
        try:
            await CommentsRepo(pool).add(
                task_id, author=api_key.actor_id, content=content
            )
        except Exception:
            pass
    data = await _task_detail_data(pool, task_id)
    if data is None:
        return HTMLResponse("<div>task not found</div>", status_code=404)
    return _detail_partial(request, data)


@router.post(
    "/ui/tasks/{task_id}/tag",
    response_class=HTMLResponse,
    response_model=None,
)
async def task_add_tag(
    request: Request,
    task_id: int,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    form = await request.form()
    tag = (form.get("tag") or "").strip()
    if tag:
        try:
            await TagsRepo(pool).add(task_id, tag)
        except Exception:
            pass
    data = await _task_detail_data(pool, task_id)
    if data is None:
        return HTMLResponse("<div>task not found</div>", status_code=404)
    return _detail_partial(request, data)


@router.post(
    "/ui/tasks/{task_id}/link",
    response_class=HTMLResponse,
    response_model=None,
)
async def task_add_link(
    request: Request,
    task_id: int,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    form = await request.form()
    kind = (form.get("kind") or "").strip()
    url = (form.get("url") or "").strip()
    label = (form.get("label") or "").strip() or None
    if kind and url:
        try:
            await LinksRepo(pool).add(task_id, kind=kind, url=url, label=label)
        except Exception:
            pass
    data = await _task_detail_data(pool, task_id)
    if data is None:
        return HTMLResponse("<div>task not found</div>", status_code=404)
    return _detail_partial(request, data)


@router.post(
    "/ui/tasks/{task_id}/transition",
    response_class=HTMLResponse,
    response_model=None,
)
async def task_transition(
    request: Request,
    task_id: int,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    form = await request.form()
    to_status = (form.get("to") or "").strip()
    comment = (str(form.get("comment") or "")).strip() or None
    error: str | None = None
    if to_status:
        try:
            await Pipeline(pool).status_change(
                task_id, to_status, actor=api_key.actor_id,  # type: ignore[arg-type]
                comment=comment,
            )
        except PipelineError as exc:
            error = str(exc)
    data = await _task_detail_data(pool, task_id)
    if data is None:
        return HTMLResponse("<div>task not found</div>", status_code=404)
    if error:
        data["error"] = error
    return _detail_partial(request, data)


_EDITABLE_INT_FIELDS = ("project_id", "objective_id", "blocked_by")
_EDITABLE_TEXT_FIELDS = (
    "title",
    "description",
    "assignee",
    "requester",
    "priority",
    "complexity_hint",
    "scope",
    "eta",
    "blocked_reason",
    "external_detail",
)


def _parse_optional_int(raw: str | None) -> int | None | object:
    """Return None for blank input (=> clear), int for digits, sentinel for invalid."""
    if raw is None:
        return _UNSET
    s = raw.strip()
    if s == "":
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    return _UNSET  # treat garbage as 'do not touch'


def _parse_optional_text(raw: str | None) -> str | None | object:
    if raw is None:
        return _UNSET
    s = raw.strip()
    return s if s else None


class _Unset:  # sentinel
    pass


_UNSET = _Unset()


@router.post(
    "/ui/tasks/{task_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def task_edit(
    request: Request,
    task_id: int,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    form = await request.form()

    fields: dict[str, object] = {}
    for name in _EDITABLE_INT_FIELDS:
        if name in form:
            v = _parse_optional_int(form.get(name))
            if not isinstance(v, _Unset):
                fields[name] = v
    for name in _EDITABLE_TEXT_FIELDS:
        if name in form:
            v = _parse_optional_text(form.get(name))
            if not isinstance(v, _Unset):
                fields[name] = v
    if "due_date" in form:
        raw = (form.get("due_date") or "").strip()
        if raw == "":
            fields["due_date"] = None
        else:
            try:
                fields["due_date"] = date.fromisoformat(raw)
            except ValueError:
                pass

    error: str | None = None
    if fields:
        try:
            await TasksRepo(pool).update(task_id, actor_id=api_key.actor_id, **fields)
        except Exception as exc:
            error = f"Update rejected: {exc}"

    data = await _task_detail_data(pool, task_id)
    if data is None:
        return HTMLResponse("<div>task not found</div>", status_code=404)
    if error:
        data["error"] = error
    return _detail_partial(request, data)




# ---------------------------------------------------------------------------
# Objective tree
# ---------------------------------------------------------------------------


async def _objective_progress_map(
    pool: asyncpg.Pool, project_id: int
) -> dict[int, dict]:
    """For every objective in ``project_id``, return totals over the
    objective's own tasks PLUS tasks linked to any descendant objective.
    """
    rows = await pool.fetch(
        """
        WITH RECURSIVE descendants(root_id, id) AS (
            SELECT id, id FROM objectives WHERE project_id = $1
            UNION ALL
            SELECT d.root_id, o.id
            FROM objectives o
            JOIN descendants d ON o.parent_id = d.id
            WHERE o.project_id = $1
        )
        SELECT d.root_id AS objective_id,
               COUNT(t.id) AS total,
               COUNT(t.id) FILTER (WHERE t.stage = 'done') AS done_count
          FROM descendants d
          LEFT JOIN tasks t ON t.objective_id = d.id
         GROUP BY d.root_id
        """,
        project_id,
    )
    out = {}
    for r in rows:
        total = r["total"] or 0
        done = r["done_count"] or 0
        out[r["objective_id"]] = {
            "total": total,
            "done": done,
            "pct": int(round(100 * done / total)) if total else 0,
        }
    return out


def _flatten_tree(nodes, out):
    for n in nodes:
        out.append(n)
        _flatten_tree(n.children, out)
    return out


@router.get(
    "/ui/projects/{slug}/objectives",
    response_class=HTMLResponse,
    response_model=None,
)
async def objective_tree_page(
    request: Request,
    slug: str,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    project = await ProjectsRepo(pool).get_by_slug(slug)
    if project is None:
        return HTMLResponse(
            templates.get_template("not_found.html").render(
                request=request,
                version=__version__,
                actor_id=api_key.actor_id,
                what=f"Project {slug!r}",
                page_title="Not found",
            ),
            status_code=404,
        )
    forest = await ObjectivesRepo(pool).tree(project_id=project.id)
    flat = _flatten_tree(forest, [])
    progress = await _objective_progress_map(pool, project.id)
    return templates.TemplateResponse(
        request,
        "objective_tree.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": f"{project.title} -- Objectives",
            "project": project,
            "nodes": flat,
            "progress": progress,
            "active_tab": "objectives",
        },
    )


# ---------------------------------------------------------------------------
# Project-wide objectives index (linked from nav)
# ---------------------------------------------------------------------------


@router.get(
    "/ui/objectives", response_class=HTMLResponse, response_model=None
)
async def objectives_index(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    projects = await ProjectsRepo(pool).list(status="active")
    objectives_repo = ObjectivesRepo(pool)
    groups = []
    for project in projects:
        forest = await objectives_repo.tree(project_id=project.id)
        flat = _flatten_tree(forest, [])
        if not flat:
            continue
        progress = await _objective_progress_map(pool, project.id)
        groups.append({"project": project, "nodes": flat, "progress": progress})
    return templates.TemplateResponse(
        request,
        "objectives_index.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "Objectives",
            "groups": groups,
        },
    )


# ---------------------------------------------------------------------------
# Global tasks list
# ---------------------------------------------------------------------------

_ALL_STATUSES: tuple[str, ...] = (
    "open",
    "awaiting_approval",
    "in_progress",
    "pending_review",
    "done",
    "blocked",
    "escalated",
    "rejected",
)

_ALL_PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")


@router.get(
    "/ui/tasks", response_class=HTMLResponse, response_model=None
)
async def tasks_index(
    request: Request,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    assignee: str | None = Query(default=None),
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()

    norm_project_id: int | None = None
    if project_id and project_id.strip().isdigit():
        norm_project_id = int(project_id.strip())
    norm_status = status if status in _ALL_STATUSES else None
    norm_priority = priority if priority in _ALL_PRIORITIES else None
    norm_assignee = assignee.strip() if assignee and assignee.strip() else None

    tasks = await TasksRepo(pool).list(
        project_id=norm_project_id,
        status=norm_status,
        priority=norm_priority,
        assignee=norm_assignee,
        limit=500,
    )
    projects = await ProjectsRepo(pool).list()
    projects_by_id = {p.id: p for p in projects}

    tags_repo = TagsRepo(pool)
    tags_by_task: dict[int, list[str]] = {}
    for t in tasks:
        tags_by_task[t.id] = await tags_repo.list_for_task(t.id)

    return templates.TemplateResponse(
        request,
        "tasks_index.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "Tasks",
            "tasks": tasks,
            "projects": projects,
            "projects_by_id": projects_by_id,
            "tags_by_task": tags_by_task,
            "all_statuses": _ALL_STATUSES,
            "all_priorities": _ALL_PRIORITIES,
            "filters": {
                "project_id": norm_project_id,
                "status": norm_status,
                "priority": norm_priority,
                "assignee": norm_assignee,
            },
        },
    )


# ---------------------------------------------------------------------------
# Schema docs
# ---------------------------------------------------------------------------


@router.get(
    "/ui/docs", response_class=HTMLResponse, response_model=None
)
async def docs_page(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    return templates.TemplateResponse(
        request,
        "docs_index.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "Data model",
        },
    )


# ---------------------------------------------------------------------------
# API keys management
# ---------------------------------------------------------------------------


@router.get(
    "/ui/keys", response_class=HTMLResponse, response_model=None
)
async def keys_page(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    query_key: str | None = Query(default=None, alias="key"),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, query_key)
    if api_key is None:
        return _redirect_to_login()
    keys = await ApiKeysRepo(pool).list(include_revoked=True)
    actors = await ActorsRepo(pool).list(active=True)
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "API keys",
            "keys": keys,
            "actors": actors,
            "new_key": None,
            "error": None,
        },
    )


@router.post(
    "/ui/keys/create", response_class=HTMLResponse, response_model=None
)
async def keys_create(
    request: Request,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    form = await request.form()
    label = (form.get("label") or "").strip()
    actor_id = (form.get("actor_id") or "").strip()
    new_key: dict | None = None
    error: str | None = None
    if not label or not actor_id:
        error = "label and actor are required"
    else:
        try:
            row, plaintext = await ApiKeysRepo(pool).create(
                label=label, actor_id=actor_id
            )
            new_key = {"label": row.label, "plaintext": plaintext}
        except Exception as exc:
            error = str(exc)
    keys = await ApiKeysRepo(pool).list(include_revoked=True)
    actors = await ActorsRepo(pool).list(active=True)
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "version": __version__,
            "actor_id": api_key.actor_id,
            "page_title": "API keys",
            "keys": keys,
            "actors": actors,
            "new_key": new_key,
            "error": error,
        },
    )


@router.post(
    "/ui/keys/{key_id}/revoke",
    response_class=HTMLResponse,
    response_model=None,
)
async def keys_revoke(
    request: Request,
    key_id: int,
    cookie_key: str | None = Cookie(default=None, alias=_COOKIE_NAME),
) -> Union[HTMLResponse, RedirectResponse]:
    pool: asyncpg.Pool = request.app.state.pool
    api_key = await _resolve_session(request, cookie_key, None)
    if api_key is None:
        return _redirect_to_login()
    await ApiKeysRepo(pool).revoke(key_id)
    return RedirectResponse(url="/ui/keys", status_code=303)
