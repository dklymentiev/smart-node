"""Work-state facts layer -- first slice.

GET /api/work/now
    One row per live agent, with the task they are working on when we can tie
    it, and an attribution block that reports how each row was tied (explicit
    | inferred | unattributed). No prose, no narrative fields: this is a facts
    layer, not a model.

Contract:

- **Planner via its API**: planner data is read through the planner HTTP
  surface only, never by DB connection to planner's schema. This module calls
  the planner service directly with the core-api operator credential so the
  same gate the agent proxy uses applies; it does not open a cursor against
  planner tables.
- **Live filter**: `agents[]` contains one row per agent that is BUSY right
  now, not per actor that has an event in the window. The filter is two
  INNER JOINs on sn_config.agents (slug is a known agent, archived_at IS
  NULL) and sn_core.agent_runtime (status='busy'). An actor such as
  `agent:admin` that is not a row in sn_config.agents is dropped; an agent
  that was busy earlier in the window but is now free or broken is dropped.
- **Minutes bound**: per-agent minutes count from `since` to the agent's
  LAST event in the window, not to `as_of`. An agent that paused half-way
  through the window stops accruing minutes at the pause.
- **Bounded read window**: fixed 60-minute window on the event log. A query
  for longer is not accepted; the preview slice does not take `window` as a
  parameter.
- **Cache**: 10-second TTL by default. `?fresh=1` bypasses.
- **Explicit-first ordering**: for the task-ref on each live agent the order
  of trust is (a) a planner tool call in this agent's recent event stream
  naming task_id, (b) a runner wake_reason tying the session to a task (not
  yet available in this preview), (c) a task_id column on ai_sessions (also
  pending). The preview reports (a) as `explicit` and falls back to the
  planner dashboard (an in-progress planner assignment) as `inferred`, and
  `unattributed` when neither signal exists.

`?price=1` returns 501: the cost extension is deferred to a later slice.

The work_now agent tool returns the same JSON as this endpoint. Register it
in sn_core.tools with http_method=GET, http_path=/api/work/now. Grants default
off.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import Principal
from permissions import requires
from db import cursor

log = logging.getLogger("core-api.work")

router = APIRouter()

# Bounded 60-minute window on the event log.
WINDOW_MINUTES = 60

# 10-second cache with ?fresh=1 bypass.
CACHE_TTL_SECONDS = 10

# One shared JSON for the whole endpoint. Keyed by principal.cross_tenant so a
# tenant-scoped caller does not read a cross-tenant answer and vice versa.
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}

# Regex to pull /tasks/<id> or /tasks/<id>/comments out of a planner tool
# payload.path. The proxy records the path with no leading slash
# ("api/v1/tasks/5025/comments"), so the pattern just looks for /tasks/<N> as a
# segment.
_TASK_PATH_RE = re.compile(r"(?:^|/)tasks/(\d+)(?:/|$)")

PLANNER_URL = os.environ.get("PLANNER_API_URL", "").rstrip("/")
PLANNER_OPERATOR_HEADER = "X-SN-Planner-Operator"
_PLANNER_TIMEOUT = 10

# Schema version. Bump on any breaking change rather than mutating in place.
SCHEMA_VERSION = 1


def _planner_key() -> str:
    return os.environ.get("PLANNER_API_KEY", "").strip()


def _operator_token() -> str:
    return os.environ.get("SN_PLANNER_OPERATOR_TOKEN", "").strip()


def _planner_get(subpath: str) -> list[dict] | dict | None:
    """Read one planner endpoint via HTTP.

    Returns the decoded JSON on 2xx, None otherwise. Never raises; the facts
    layer surfaces blind spots in `notes` instead of 500ing.
    """
    if not PLANNER_URL:
        return None
    url = f"{PLANNER_URL}/{subpath.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    # Core-api speaks to planner as itself (operator). The agent-identity header
    # is only set on the proxy, where a real agent is calling through.
    key = _planner_key()
    if key:
        req.add_header("X-API-Key", key)
    tok = _operator_token()
    if tok:
        req.add_header(PLANNER_OPERATOR_HEADER, tok)
    try:
        with urllib.request.urlopen(req, timeout=_PLANNER_TIMEOUT) as resp:
            if resp.status >= 400:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as ex:
        log.warning("work.now: planner read %s failed: %s", subpath, ex)
        return None


def _extract_task_id(path: str | None) -> int | None:
    """Pull a task_id out of a planner event payload.path, if present.

    Only paths that name /tasks/<N>/... count. A listing or search call
    (api/v1/tasks, api/v1/tasks/search) returns None here -- the agent asked
    about tasks in general, which is not a tie to one in particular.
    """
    if not path:
        return None
    m = _TASK_PATH_RE.search(path)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _render_last_step(event_type: str | None, entity_id: str | None, payload: dict | None) -> str:
    """One short label for the agent's most recent event (`last_step` field).

    Examples: "tool.invoked planner", "tool.succeeded mesh_search",
    "agent.woken". The payload's op is appended for tool.* when present, so a
    reader sees which planner op (planner.list / planner.comment / ...).
    """
    et = event_type or "unknown"
    label = et
    if entity_id:
        label = f"{et} {entity_id}"
    if isinstance(payload, dict):
        op = payload.get("op")
        if op and (not entity_id or op != entity_id):
            label = f"{label} {op}"
    return label


def _assigned_task_ids(dashboard: list[dict] | None, agent_slug: str) -> list[int]:
    """From the planner dashboard, the agent's in-progress task ids (newest first)."""
    if not dashboard:
        return []
    hits: list[tuple[datetime, int]] = []
    for row in dashboard:
        if row.get("assignee") != agent_slug:
            continue
        # `stage` is the legacy one-word name in every planner vocabulary (0010).
        if (row.get("stage") or row.get("status")) != "in_progress":
            continue
        tid = row.get("id")
        if not isinstance(tid, int):
            continue
        updated = row.get("updated_at")
        try:
            ts = datetime.fromisoformat(str(updated).replace("Z", "+00:00")) if updated else datetime.now(timezone.utc)
        except ValueError:
            ts = datetime.now(timezone.utc)
        hits.append((ts, tid))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in hits]


def _task_title(dashboard: list[dict] | None, task_id: int | None) -> str | None:
    if dashboard is None or task_id is None:
        return None
    for row in dashboard:
        if row.get("id") == task_id:
            return row.get("title")
    return None


def _build_now(principal: Principal) -> dict:
    """Compose the /api/work/now JSON. No cache concerns at this level."""
    now = datetime.now(timezone.utc)
    window_from = now - timedelta(minutes=WINDOW_MINUTES)

    notes: list[str] = []

    # ----- planner dashboard (one HTTP call, Clause 3). ------------------------
    dashboard = _planner_get("api/v1/dashboard/agents-working")
    if dashboard is None:
        notes.append("planner dashboard unavailable, task refs limited to explicit event ties")
        dashboard_list: list[dict] = []
    else:
        dashboard_list = dashboard if isinstance(dashboard, list) else []

    # ----- event-log reads (fixed 60-minute window). ---------------------------
    # Each row: (slug, event_type, entity_id, payload, ts). DISTINCT ON gives
    # one latest row per slug for `last_step`; a second query collects the
    # per-agent planner task refs in the window.
    with cursor() as cur:
        tenant_clause = ""
        params: list[Any] = [window_from]
        if not principal.cross_tenant:
            tenant_clause = " AND tenant_id = %s"
            params.append(principal.tenant_id)

        # Latest event per agent in the window. INNER JOINs restrict the row
        # set to (a) known agents (sn_config.agents), so a non-agent actor such
        # as `agent:admin` does not appear, and (b) agents the Runner reports
        # as currently working (sn_core.agent_runtime.status = 'busy'), so an
        # agent that emitted events earlier in the window but is now free or
        # broken does not appear. The screen titled "who is working now" must
        # not call an idle agent busy. `tenant_id` is the one on the event row.
        cur.execute(
            "SELECT DISTINCT ON (a.slug) "
            "  a.slug AS slug, "
            "  e.event_type, e.entity_id, e.payload, e.ts "
            "FROM sn_core.events e "
            "JOIN sn_config.agents a "
            "  ON a.slug = split_part(e.actor, ':', 2) "
            " AND a.archived_at IS NULL "
            "JOIN sn_core.agent_runtime r "
            "  ON r.agent_slug = a.slug "
            " AND r.status = 'busy' "
            "WHERE e.ts > %s" + tenant_clause.replace("tenant_id", "e.tenant_id") + " "
            "  AND e.actor LIKE 'agent:%%' "
            "ORDER BY a.slug, e.ts DESC",
            tuple(params),
        )
        last_event_rows = cur.fetchall()

        # Earliest event per agent in the window (for `since`). Same two
        # filters: known slug AND currently busy, so `since` for a stopped
        # agent is never computed.
        cur.execute(
            "SELECT DISTINCT ON (a.slug) "
            "  a.slug AS slug, e.ts "
            "FROM sn_core.events e "
            "JOIN sn_config.agents a "
            "  ON a.slug = split_part(e.actor, ':', 2) "
            " AND a.archived_at IS NULL "
            "JOIN sn_core.agent_runtime r "
            "  ON r.agent_slug = a.slug "
            " AND r.status = 'busy' "
            "WHERE e.ts > %s" + tenant_clause.replace("tenant_id", "e.tenant_id") + " "
            "  AND e.actor LIKE 'agent:%%' "
            "ORDER BY a.slug, e.ts ASC",
            tuple(params),
        )
        first_event_rows = {r["slug"]: r["ts"] for r in cur.fetchall()}

        # All planner tool.* events in the window, newest first, so we can walk
        # them and find the most recent task_id each agent explicitly named.
        # Join to sn_config.agents so a non-agent actor's planner call cannot
        # tie a non-agent row (defensive: non-agents are already removed from
        # the main set above, but a planner payload scan is cheaper to filter
        # here than to post-filter in Python).
        cur.execute(
            "SELECT a.slug AS slug, e.event_type, e.payload, e.ts "
            "FROM sn_core.events e "
            "JOIN sn_config.agents a "
            "  ON a.slug = split_part(e.actor, ':', 2) "
            " AND a.archived_at IS NULL "
            "WHERE e.ts > %s" + tenant_clause.replace("tenant_id", "e.tenant_id") + " "
            "  AND e.actor LIKE 'agent:%%' "
            "  AND e.entity_type = 'tool' AND e.entity_id = 'planner' "
            "ORDER BY e.ts DESC",
            tuple(params),
        )
        planner_events = cur.fetchall()

    # Per-agent explicit task ref: scan planner_events newest-first.
    explicit_task: dict[str, tuple[int, datetime]] = {}
    for ev in planner_events:
        slug = ev["slug"]
        if slug in explicit_task:
            continue
        tid = _extract_task_id((ev.get("payload") or {}).get("path"))
        if tid is None:
            continue
        explicit_task[slug] = (tid, ev["ts"])

    # Explicit "since": the earliest planner event in the window whose path
    # names the SAME task_id. That is when this agent first touched this task
    # in the window (not necessarily the task start, but Clause 1's `since` is
    # window-bounded).
    explicit_since: dict[str, datetime] = {}
    for ev in reversed(planner_events):
        slug = ev["slug"]
        tid = _extract_task_id((ev.get("payload") or {}).get("path"))
        if tid is None:
            continue
        exp = explicit_task.get(slug)
        if exp and exp[0] == tid and slug not in explicit_since:
            explicit_since[slug] = ev["ts"]

    # ----- compose agent rows ---------------------------------------------------
    agents_out: list[dict] = []
    explicit_minutes = 0.0
    inferred_minutes = 0.0
    unattributed_minutes = 0.0
    explicit_refs = 0
    inferred_refs = 0
    unattributed = 0

    for row in last_event_rows:
        slug = row["slug"]
        if not slug:
            continue

        explicit_tid, explicit_ts = (explicit_task[slug] if slug in explicit_task else (None, None))
        task_id: int | None = None
        attribution: str
        since: datetime

        if explicit_tid is not None:
            task_id = explicit_tid
            attribution = "explicit"
            since = explicit_since.get(slug, explicit_ts or first_event_rows.get(slug, window_from))
        else:
            # Inferred: the planner dashboard has an in-progress task assigned to
            # this agent, but no explicit event tie in the window. Pick the newest.
            inferred = _assigned_task_ids(dashboard_list, slug)
            if inferred:
                task_id = inferred[0]
                attribution = "inferred"
                since = first_event_rows.get(slug, window_from)
            else:
                attribution = "unattributed"
                since = first_event_rows.get(slug, window_from)

        # Minutes active in this window: from `since` to the agent's LAST event
        # in the window, not to `as_of`. The runtime filter above keeps only
        # agents still busy right now; even so, the minutes attributed must
        # stop at the latest observed event, so an agent that paused half-way
        # through the window does not keep accruing minutes until as_of.
        active_from = max(since, window_from)
        last_ts = row.get("ts") or now
        minutes = max(0.0, (last_ts - active_from).total_seconds() / 60.0)
        if attribution == "explicit":
            explicit_minutes += minutes
            explicit_refs += 1
        elif attribution == "inferred":
            inferred_minutes += minutes
            inferred_refs += 1
        else:
            unattributed_minutes += minutes
            unattributed += 1

        agents_out.append({
            "agent": slug,
            "task_id": task_id,
            "task_title": _task_title(dashboard_list, task_id) if task_id else None,
            "since": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "last_step": _render_last_step(
                row.get("event_type"),
                row.get("entity_id"),
                row.get("payload") if isinstance(row.get("payload"), dict) else None,
            ),
            "attribution": attribution,
        })

    agents_out.sort(key=lambda a: (0 if a["attribution"] == "explicit"
                                   else 1 if a["attribution"] == "inferred"
                                   else 2, a["agent"]))

    # Preview: the task_id column on ai_sessions / ai_session_messages is not
    # yet available. Attribution ties use planner tool-call paths and the
    # planner dashboard only; this is reported in notes.
    notes.append("task-id column on ai_sessions pending; "
                 "attribution ties use planner tool-call paths and planner dashboard only.")
    # Live filter is enforced in SQL: known agent slugs AND Runner-reported
    # busy state. Document the filter so a reader of the JSON knows what the
    # agents[] list means on this schema version.
    notes.append("agents filtered to currently busy state "
                 "(sn_core.agent_runtime.status='busy') "
                 "AND known slugs (sn_config.agents); "
                 "non-agent actors and stopped agents are omitted.")
    notes.append("minutes computed from since to each agent's last event in "
                 "the window; an agent that stopped mid-window stops accruing.")

    return {
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "window_minutes": WINDOW_MINUTES,
        "agents": agents_out,
        "attribution": {
            "explicit_refs": explicit_refs,
            "inferred_refs": inferred_refs,
            "unattributed": unattributed,
            "explicit_minutes": round(explicit_minutes, 1),
            "inferred_minutes": round(inferred_minutes, 1),
            "unattributed_minutes": round(unattributed_minutes, 1),
            "notes": notes,
        },
        "schema_version": SCHEMA_VERSION,
    }


@router.get("/now", response_model=dict)
def work_now(
    request: Request,
    fresh: int = Query(default=0, ge=0, le=1),
    price: int = Query(default=0, ge=0, le=1),
    principal: Principal = Depends(requires("work.read")),
):
    """Live work-state across the node, first slice.

    `fresh=1` bypasses the 10s cache. `price=1` is reserved for the cost
    extension; this preview returns 501 instead of a half-answer.
    """
    if price:
        raise HTTPException(
            status_code=501,
            detail={
                "error": "price_not_implemented",
                "message": "The cost extension is deferred to a later slice. "
                           "The /now slice reports attribution only.",
            },
        )

    cache_key = (principal.tenant_id, principal.cross_tenant)
    if not fresh:
        with _cache_lock:
            hit = _cache.get(cache_key)
            if hit and (time.monotonic() - hit[0]) < CACHE_TTL_SECONDS:
                return hit[1]

    data = _build_now(principal)

    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), data)

    return data


# ============================================================================
# /api/work/map -- the Work Map data layer.
#
# Same attribution rule as the lab mockup: per-agent working stretches with
# gaps up to 5 minutes, split between the tasks referenced inside the stretch
# (planner call path /tasks/<id>, task_id in the event payload, a 4-digit task
# number inside a message preview). The response keeps the keys of the lab
# mockup it replaced, so the Studio template renders the same page on live data.
#
# Planner reads go through the HTTP surface only. Events come from sn_core.events
# via the shared db cursor; tenant scope applies.
#
# Cache is longer here than /now: the Work Map is a weekly picture (168h) with
# hourly resolution, so a 60s TTL is enough and keeps the planner-fanout cheap.
# ============================================================================

# Housekeeping event types the lab excludes from the feed. They are frequent
# and would drown out everything else.
_HOUSEKEEPING_EVENT_TYPES = frozenset({
    "admin.op.executed",
    "secret.access_denied",
    "session.context_threshold_crossed",
    "node.retention_swept",
    "service.health_ok",
})

# Tool payload ops that count as "writing" to a task when inferring whether a
# stretch worked on it or only read it.
_WRITE_OPS = frozenset({"planner.comment", "planner.update", "planner.create"})

# Work Map cache: 60 seconds. The template reads /api/work/map on page load
# (and the Studio page can poll in the future), so the hot path is a cache hit.
MAP_CACHE_TTL_SECONDS = 60

# Minimum task id to accept from the event payloads. The lab guards against
# accidental 4-digit matches on numbers that are not task ids.
_MIN_TASK_ID = 3000

# Gap that breaks one working stretch into two.
_STRETCH_GAP = timedelta(minutes=5)

# Housekeeping of the planner-fanout: cap how many /tasks/<id>/log calls we
# will make per rebuild, in case a surge of referenced tasks appears.
_MAX_LOG_FANOUT = 400
_LOG_WORKERS = 32   # planner reads in flight at once while the map is built

# Window parsing. Only 24h and 168h are accepted (7d). The lab uses 7d.
_ALLOWED_WINDOWS_HOURS = {24, 168}

# Status labels that get rendered on screen. The template reads these from the
# response (the lab inlined them in the HTML); keeping them server-side lets
# the Studio page stay a thin renderer.
_STATUS_LABEL_EN = {
    "open": "opened",
    "in_progress": "to in progress",
    "pending_review": "under review",
    "done": "closed",
    "blocked": "blocked",
    "rejected": "rejected",
    "awaiting_approval": "awaiting approval",
    "escalated": "escalated",
    "assessing": "assessing",
    "incident": "incident",
}

_FEED_KIND_LABEL_EN = {
    "created": "opened",
    "done": "closed",
    "pending_review": "sent to review",
    "blocked": "blocked",
    "rejected": "rejected",
}

# Lane names in English. The lab uses Russian for a few virtual lanes; the
# response names them in English so the template does not translate them.
_NAME_NO_PROJECT = "No project"
_NAME_NO_TASK = "No task"
_NAME_WORK_OUTSIDE_TASKS = "work outside tasks"
_NAME_UNPROJECTED = "unprojected tasks"
_NAME_EMERGENT = "Emergent"
_NAME_OTHER = "others"

# task-ref regexes (same as the lab).
_TASK_NUM_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_TASK_FROM_PATH_RE = re.compile(r"tasks/(\d+)")


def _parse_ts(value: Any) -> datetime | None:
    """Parse a planner / events timestamp into an aware UTC datetime.

    The event log and the planner API both emit ISO 8601 with a trailing Z.
    The lab's build_flow.py accepts the same shape; this helper mirrors it.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _planner_get_all_tasks() -> list[dict]:
    """Walk /api/v1/tasks in pages of 500 until a page is short.

    The planner caps `limit` at 500; a short page means the walk is done. The ordering is stable for this read
    (newest first), so pages are asked for in batches that go out together, and kept in page order. A walk
    of ~6000 tasks was twelve calls one after another.
    """
    out: list[dict] = []
    page_size, batch = 500, 6
    offset = 0
    with ThreadPoolExecutor(max_workers=batch) as pool:
        while offset < 20000:
            offsets = [offset + i * page_size for i in range(batch)]
            pages = list(pool.map(lambda o: _planner_get(f"api/v1/tasks?limit={page_size}&offset={o}"), offsets))
            for page in pages:
                if not isinstance(page, list) or not page:
                    return out
                out.extend(page)
                if len(page) < page_size:
                    return out
            offset += page_size * batch
    # Hard stop; a tenant with more than 20k tasks will have other problems.
    log.warning("work.map: planner tasks walk hit 20k safety stop")
    return out


def _planner_get_projects() -> list[dict]:
    page = _planner_get("api/v1/projects?limit=500")
    return page if isinstance(page, list) else []


def _planner_get_objectives() -> list[dict]:
    page = _planner_get("api/v1/objectives?limit=500")
    return page if isinstance(page, list) else []


def _planner_task_log(task_id: int) -> list[dict]:
    page = _planner_get(f"api/v1/tasks/{task_id}/log")
    return page if isinstance(page, list) else []


def _refs_from_payload(payload: Any, max_task_id: int, known_task_ids: set[int]) -> list[int]:
    """Pull task refs out of one event payload (same rule as build_flow.py).

    - A path with /tasks/<id> is the strongest signal.
    - An integer task_id field.
    - 4-digit numbers inside the preview, bounded to known ids and to the
      configured task-id floor.
    """
    if not isinstance(payload, dict):
        return []
    out: list[int] = []
    path = payload.get("path")
    if isinstance(path, str):
        m = _TASK_FROM_PATH_RE.search(path)
        if m:
            try:
                out.append(int(m.group(1)))
            except (TypeError, ValueError):
                pass
    tid = payload.get("task_id")
    if isinstance(tid, int):
        out.append(tid)
    preview = payload.get("preview")
    if isinstance(preview, str):
        for m in _TASK_NUM_RE.finditer(preview):
            try:
                out.append(int(m.group(1)))
            except (TypeError, ValueError):
                pass
    return [i for i in out if _MIN_TASK_ID <= i <= max_task_id and i in known_task_ids]


def _render_label_task(tid: int, tasks_by_id: dict[int, dict]) -> str:
    t = tasks_by_id.get(tid)
    if not t:
        return f"#{tid}"
    title = (t.get("title") or "").strip()
    if len(title) <= 60:
        tail = title
    else:
        head = title[:58]
        if " " in head:
            head = head.rsplit(" ", 1)[0]
        tail = head + "..."
    return f"#{tid} {tail}" if tail else f"#{tid}"


def _project_of_task(
    tid: int | None, tasks_by_id: dict[int, dict], projects_by_id: dict[int, str]
) -> str:
    if tid is None:
        return _NAME_NO_TASK
    t = tasks_by_id.get(tid)
    if not t:
        return _NAME_NO_PROJECT
    pid = t.get("project_id")
    name = projects_by_id.get(pid) if isinstance(pid, int) else None
    return name or _NAME_NO_PROJECT


def _branch_of_task(
    tid: int | None, tasks_by_id: dict[int, dict], objectives_by_id: dict[int, str]
) -> str | None:
    if tid is None:
        return _NAME_WORK_OUTSIDE_TASKS
    t = tasks_by_id.get(tid)
    if not t:
        return None
    oid = t.get("objective_id")
    return objectives_by_id.get(oid) if isinstance(oid, int) else None


def _smoothed_bucket_round(arr: list[float]) -> list[float]:
    return [round(x, 2) for x in arr]


def _build_map(principal: Principal, window_hours: int) -> dict:
    """Compose /api/work/map for the given window.

    Builds the per-project, per-agent and per-task views with the same keys
    the Studio consumes, over a bounded hourly window.
    """
    import collections

    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=window_hours)
    hours = window_hours

    def hour_of(ts: datetime) -> int:
        """Map a timestamp into the hourly bucket."""
        return max(0, min(hours - 1, int((ts - t0).total_seconds() // 3600)))

    def rel_h(ts: datetime) -> float:
        return (ts - t0).total_seconds() / 3600.0

    notes: list[str] = []

    # --- planner reads (HTTP only, Clause 3) -----------------------------------
    # --- event-log reads: started now, they run while the planner is read ------------------------------------
    tenant_clause = ""
    params: list[Any] = [t0]
    if not principal.cross_tenant:
        tenant_clause = " AND tenant_id = %s"
        params.append(principal.tenant_id)

    def read_agent_events() -> list[dict]:
        out: list[dict] = []
        with cursor() as cur:
            cur.execute(
                "SELECT ts, actor, event_type, entity_type, entity_id, payload "
                "FROM sn_core.events "
                "WHERE ts > %s" + tenant_clause + " "
                "  AND event_type NOT IN ("
                "    'admin.op.executed','secret.access_denied',"
                "    'session.context_threshold_crossed','node.retention_swept',"
                "    'service.health_ok') "
                # Components, the system, cron and the OS write most of the log (permission checks, planner reads,
                # secret reads, sweeps: 546k of 686k events in a week) and none of it is an agent's work. Every
                # later step reads agent events, a person's message, or a status change; reading and decoding
                # the rest was 37 s of CPU per map.
                "  AND NOT (actor NOT LIKE 'agent:%%' AND event_type IN ("
                "    'tool.invoked','tool.succeeded','auth.permission_granted','auth.permission_denied',"
                "    'admin.permission_granted','secret.read','identity.delegated_owner',"
                "    'cron.started','cron.completed','cron.rendered','cron.failed',"
                "    'flow.sweep_started','flow.sweep_finished')) "
                "ORDER BY ts",
                tuple(params),
            )
            for r in cur.fetchall():
                out.append({
                    "ts": r["ts"] if r["ts"].tzinfo else r["ts"].replace(tzinfo=timezone.utc),
                    "actor": r["actor"],
                    "event_type": r["event_type"],
                    "entity_type": r["entity_type"],
                    "entity_id": r["entity_id"],
                    "payload": r["payload"] if isinstance(r["payload"], dict) else {},
                })
        return out

    def read_whole_log() -> tuple:
        # What the page says about the log as a whole is still about all of it: how many events, when the last
        # one was, and the raw tail. The rows above are only what the map decodes.
        with cursor() as cur:
            base = ("FROM sn_core.events WHERE ts > %s" + tenant_clause + " "
                    "AND event_type NOT IN ('admin.op.executed','secret.access_denied',"
                    "'session.context_threshold_crossed','node.retention_swept','service.health_ok') ")
            cur.execute("SELECT count(*) AS n, max(ts) AS last " + base, tuple(params))
            total = cur.fetchone()
            cur.execute("SELECT ts, actor, event_type, entity_type, entity_id " + base + "ORDER BY ts DESC LIMIT 80",
                        tuple(params))
            tail = [{**r, "ts": r["ts"] if r["ts"].tzinfo else r["ts"].replace(tzinfo=timezone.utc)}
                    for r in cur.fetchall()]
        return total, tail

    io = ThreadPoolExecutor(max_workers=2)
    rows_f = io.submit(read_agent_events)
    whole_f = io.submit(read_whole_log)
    planner_tasks_list = _planner_get_all_tasks()
    projects_list = _planner_get_projects()
    objectives_list = _planner_get_objectives()
    tasks_by_id: dict[int, dict] = {}
    for t in planner_tasks_list:
        tid = t.get("id")
        if isinstance(tid, int) and tid >= _MIN_TASK_ID:
            tasks_by_id[tid] = t
    projects_by_id: dict[int, str] = {}
    for p in projects_list:
        pid = p.get("id")
        if isinstance(pid, int):
            projects_by_id[pid] = p.get("title") or p.get("slug") or f"project:{pid}"
    objectives_by_id: dict[int, str] = {}
    for o in objectives_list:
        oid = o.get("id")
        if isinstance(oid, int):
            objectives_by_id[oid] = o.get("title") or f"objective:{oid}"
    if not tasks_by_id:
        notes.append("planner tasks unavailable; task-ref attribution is limited")
    max_task_id = max(tasks_by_id, default=_MIN_TASK_ID)
    known_task_ids = set(tasks_by_id)

    # --- event-log read (bounded window) ---------------------------------------
    rows: list[dict] = rows_f.result()
    tot, raw_rows = whole_f.result()
    io.shutdown()
    total_events = int(tot["n"] or 0)
    if tot["last"] is not None:
        t_end = tot["last"] if tot["last"].tzinfo else tot["last"].replace(tzinfo=timezone.utc)
    else:
        t_end = now

    # --- effort attribution: per-agent working stretches -----------------------
    samples: list[tuple[str, int | None, int, float]] = []  # (agent, tid|None, hour, minutes)
    segs: dict[int, list[tuple[str, float, float, str]]] = collections.defaultdict(list)
    by_actor: dict[str, list[dict]] = collections.defaultdict(list)
    for ev in rows:
        actor = ev["actor"] or ""
        if actor.startswith("agent:"):
            by_actor[actor[6:]].append(ev)

    def seg_kind(stretch: list[dict], tid: int) -> str:
        for ev in stretch:
            if tid in _refs_from_payload(ev["payload"], max_task_id, known_task_ids):
                payload = ev["payload"]
                op = payload.get("op") if isinstance(payload, dict) else None
                if op in _WRITE_OPS or ev["event_type"] == "message.inbound":
                    return "work"
        return "read"

    def flush_stretch(agent: str, stretch: list[dict]) -> None:
        minutes = max(1.0, (stretch[-1]["ts"] - stretch[0]["ts"]).total_seconds() / 60.0)
        counts: dict[int, int] = collections.Counter()
        for ev in stretch:
            for ref in _refs_from_payload(ev["payload"], max_task_id, known_task_ids):
                counts[ref] += 1
        hr = hour_of(stretch[0]["ts"])
        if not counts:
            samples.append((agent, None, hr, minutes))
            return
        total_refs = sum(counts.values())
        for tid, n in counts.items():
            samples.append((agent, tid, hr, minutes * n / total_refs))
            touched_ts = [ev["ts"] for ev in stretch
                          if tid in _refs_from_payload(ev["payload"], max_task_id, known_task_ids)]
            if touched_ts:
                segs[tid].append((agent, rel_h(min(touched_ts)), rel_h(max(touched_ts)), seg_kind(stretch, tid)))

    for agent, evs in by_actor.items():
        evs.sort(key=lambda e: e["ts"])
        stretch = [evs[0]]
        for ev in evs[1:]:
            if ev["ts"] - stretch[-1]["ts"] > _STRETCH_GAP:
                flush_stretch(agent, stretch)
                stretch = [ev]
            else:
                stretch.append(ev)
        if stretch:
            flush_stretch(agent, stretch)

    # --- planner transitions (status moves) within the window ------------------
    # Fetch /tasks/<id>/log for every task either referenced by an event or
    # recently updated. Cap the fanout to _MAX_LOG_FANOUT.
    referenced_ids: set[int] = set()
    for _, tid, _, _ in samples:
        if tid is not None:
            referenced_ids.add(tid)
    for t in planner_tasks_list:
        upd = _parse_ts(t.get("updated_at"))
        tid = t.get("id")
        if upd and upd >= t0 and isinstance(tid, int) and tid in known_task_ids:
            referenced_ids.add(tid)
    log_targets = list(referenced_ids)
    if len(log_targets) > _MAX_LOG_FANOUT:
        notes.append(
            f"planner-log fanout capped at {_MAX_LOG_FANOUT} tasks "
            f"({len(log_targets)} referenced); older transitions may be absent."
        )
        log_targets = log_targets[:_MAX_LOG_FANOUT]
    transitions: list[dict] = []
    tracked_states = {"done", "blocked", "pending_review", "rejected", "in_progress"}
    # One planner call per task, up to 400: one after another that was most of the wait once the event read
    # got cheap. They are independent reads; map() keeps the order of log_targets.
    with ThreadPoolExecutor(max_workers=_LOG_WORKERS) as pool:
        all_logs = list(pool.map(_planner_task_log, log_targets))
    for tid, log_rows in zip(log_targets, all_logs):
        for row in log_rows:
            if row.get("action") != "status_changed":
                continue
            ts = _parse_ts(row.get("created_at"))
            if ts is None or ts < t0:
                continue
            transitions.append({
                "task_id": tid,
                "from": row.get("old_value"),
                "to": row.get("new_value"),
                "actor": row.get("actor") or "?",
                "ts": ts,
            })

    # --- nodes on each lane: transitions + task creations within window --------
    nodes_all: list[dict] = []
    for tr in transitions:
        if tr["to"] in ("done", "blocked", "pending_review", "rejected"):
            nodes_all.append({
                "tid": tr["task_id"],
                "h": rel_h(tr["ts"]),
                "kind": tr["to"],
                "actor": tr["actor"],
            })
    for t in tasks_by_id.values():
        c = _parse_ts(t.get("created_at"))
        if c is not None and c >= t0:
            nodes_all.append({
                "tid": t["id"],
                "h": rel_h(c),
                "kind": "created",
                "actor": t.get("requester") or "?",
            })

    # --- build project / agent views ------------------------------------------
    def build_view(mode: str) -> list[dict]:
        if mode == "project":
            def lane_of(a: str, tid: int | None) -> str:
                return _project_of_task(tid, tasks_by_id, projects_by_id)
        else:
            def lane_of(a: str, tid: int | None) -> str:
                return a
        lane_total: dict[str, float] = collections.Counter()
        for a, tid, h, m in samples:
            lane_total[lane_of(a, tid)] += m
        if mode == "project":
            special = {_NAME_NO_PROJECT, _NAME_NO_TASK}
            total = sum(lane_total.values()) or 1.0
            main = [k for k, v in lane_total.most_common()
                    if k not in special and v >= 0.02 * total][:5]
            def lane_key(a: str, tid: int | None) -> str:
                k = _project_of_task(tid, tasks_by_id, projects_by_id)
                return k if k in main else _NAME_EMERGENT
            order = main + [_NAME_EMERGENT]
        else:
            main = [k for k, _ in lane_total.most_common(7)]
            def lane_key(a: str, tid: int | None) -> str:
                return a if a in main else _NAME_OTHER
            order = main + ([_NAME_OTHER] if len(lane_total) > 7 else [])
        per_lane_task: dict[str, dict[int, float]] = collections.defaultdict(collections.Counter)
        for a, tid, h, m in samples:
            per_lane_task[lane_key(a, tid)][tid] += m
        def bkey(lane: str, a: str, tid: int | None) -> str:
            if mode == "agent":
                return _project_of_task(tid, tasks_by_id, projects_by_id)
            if lane == _NAME_EMERGENT:
                if tid is None:
                    return _NAME_WORK_OUTSIDE_TASKS
                p = _project_of_task(tid, tasks_by_id, projects_by_id)
                return p if p != _NAME_NO_PROJECT else _NAME_UNPROJECTED
            br = _branch_of_task(tid, tasks_by_id, objectives_by_id)
            return br or f"task:{tid}"
        raw: dict[str, dict[str, list[float]]] = collections.defaultdict(
            lambda: collections.defaultdict(lambda: [0.0] * hours))
        btask: dict[str, dict[str, dict[int, float]]] = collections.defaultdict(
            lambda: collections.defaultdict(collections.Counter))
        for a, tid, h, m in samples:
            L = lane_key(a, tid)
            B = bkey(L, a, tid)
            raw[L][B][h] += m
            btask[L][B][tid] += m
        lanes_out: list[dict] = []
        for L in order:
            if L not in raw:
                continue
            bs = sorted(raw[L].items(), key=lambda kv: -sum(kv[1]))
            keep, rest = bs[:3], bs[3:]
            branches = []
            for key, arr in keep:
                if key.startswith("task:"):
                    name = _render_label_task(int(key[5:]), tasks_by_id)
                else:
                    name = key
                top = []
                for t_id, _ in sorted(btask[L][key].items(), key=lambda kv: -kv[1])[:3]:
                    if t_id:
                        top.append(_render_label_task(int(t_id), tasks_by_id))
                branches.append({
                    "key": key, "name": name,
                    "h": _smoothed_bucket_round(arr),
                    "top": top,
                })
            if rest:
                arr = [sum(x) for x in zip(*[a for _, a in rest])]
                tops = []
                for key, _ in rest[:3]:
                    for t_id, _ in sorted(btask[L][key].items(), key=lambda kv: -kv[1])[:1]:
                        if t_id:
                            tops.append(_render_label_task(int(t_id), tasks_by_id))
                branches.append({
                    "key": "__rest", "name": f"other ({len(rest)})",
                    "h": _smoothed_bucket_round(arr), "top": tops,
                })
            tmap: dict[int, str] = {}
            for key, counts in btask[L].items():
                target = key if any(b["key"] == key for b in branches) else "__rest"
                for t_id in counts:
                    if t_id:
                        tmap[int(t_id)] = target
            lane_nodes: list[dict] = []
            for n in nodes_all:
                if n["tid"] in tmap:
                    lane_nodes.append({**n, "b": tmap[n["tid"]],
                                       "title": tasks_by_id[n["tid"]].get("title") or ""})
            agents_counter: dict[str, float] = collections.Counter()
            for a, tid, h, m in samples:
                if lane_key(a, tid) == L:
                    agents_counter[a if mode == "project"
                                   else _project_of_task(tid, tasks_by_id, projects_by_id)] += m
            lanes_out.append({
                "name": L,
                "branches": branches,
                "nodes": lane_nodes,
                "who": [k for k, _ in sorted(agents_counter.items(), key=lambda kv: -kv[1])[:4]],
                "total": round(sum(sum(b["h"]) for b in branches)),
            })
        return lanes_out

    def steps_for(tid: int, touching: list[dict]) -> list[dict]:
        """One step per turn of work by one actor. Reads are omitted."""
        acts: list[tuple[datetime, str, str, Any, Any]] = []
        for ev in touching:
            if ev["ts"] < t0:
                continue
            actor = ev["actor"] or ""
            if not actor.startswith("agent:"):
                continue
            a = actor[6:]
            payload = ev["payload"]
            if ev["event_type"] == "message.inbound" and payload.get("channel") == "internal":
                to = payload.get("to") or ""
                if ":" in to:
                    continue
                text = (payload.get("preview") or "").split("\n")[0][:220]
                acts.append((ev["ts"], a, "msg", to, text))
            elif ev["event_type"] == "tool.invoked" and payload.get("op") == "planner.comment":
                acts.append((ev["ts"], a, "comment", None, None))
            elif ev["event_type"] == "tool.invoked" and payload.get("op") == "planner.create":
                acts.append((ev["ts"], a, "create", None, None))
        for tr in transitions:
            if tr["task_id"] == tid:
                acts.append((tr["ts"], tr["actor"], "status", tr["to"], tr["from"]))
        acts.sort(key=lambda a: a[0])
        steps: list[dict] = []
        for ts, a, kind, arg, text in acts:
            s = steps[-1] if steps else None
            if s and s["a"] == a and (ts - s["_e"]).total_seconds() <= 1800:
                s["_e"] = ts
            else:
                s = {"a": a, "_s": ts, "_e": ts, "comments": 0, "status": [], "msgs": []}
                steps.append(s)
            if kind == "comment":
                s["comments"] += 1
            elif kind == "status":
                s["status"].append(_STATUS_LABEL_EN.get(arg, arg or "?"))
            elif kind == "msg":
                s["msgs"].append({"to": arg, "text": text})
        out: list[dict] = []
        for s in steps:
            what = []
            if s["status"]:
                what.append(" / ".join(s["status"]))
            if s["comments"]:
                what.append("comment" + (f" x{s['comments']}" if s["comments"] > 1 else ""))
            if s["msgs"]:
                recipients = sorted({m["to"] for m in s["msgs"] if m.get("to")})
                if recipients:
                    what.append("wrote to " + ", ".join(recipients))
            out.append({
                "a": s["a"],
                "s": rel_h(s["_s"]),
                "e": rel_h(s["_e"]),
                "what": ", ".join(what),
                "msgs": s["msgs"],
            })
        return out

    def build_tasks_view() -> list[dict]:
        week: dict[int, float] = collections.Counter()
        day: dict[int, float] = collections.Counter()
        per: dict[int, dict[str, list[float]]] = collections.defaultdict(
            lambda: collections.defaultdict(lambda: [0.0] * hours))
        for a, tid, h, m in samples:
            if tid is None:
                continue
            week[tid] += m
            if h >= hours - 24:
                day[tid] += m
            per[tid][a][h] += m
        last_touch: dict[int, float] = {}
        for tid_, lst in segs.items():
            work_ends = [e for a, s, e, k in lst if k == "work"]
            if work_ends:
                last_touch[tid_] = max(work_ends)
        recent = sorted(
            [t for t, h in last_touch.items() if h >= hours - 3],
            key=lambda t: -last_touch[t],
        )
        chosen: list[int] = []
        for tid in recent[:6] + [t for t, _ in day.most_common(6)] + [t for t, _ in week.most_common(8)]:
            if tid not in chosen and tid in tasks_by_id:
                chosen.append(tid)
        chosen = chosen[:12]
        chosen.sort(key=lambda t: -last_touch.get(t, -1.0))
        lanes_out: list[dict] = []
        for tid in chosen:
            t = tasks_by_id[tid]
            bs = sorted(per[tid].items(), key=lambda kv: -sum(kv[1]))
            keep, rest = bs[:4], bs[4:]
            branches = [
                {"key": a, "name": a, "h": _smoothed_bucket_round(arr), "top": []}
                for a, arr in keep
            ]
            if rest:
                arr = [sum(x) for x in zip(*[a for _, a in rest])]
                branches.append({
                    "key": "__rest",
                    "name": "others: " + ", ".join(a for a, _ in rest),
                    "h": _smoothed_bucket_round(arr),
                    "top": [],
                })
            keys = {b["key"] for b in branches}
            lane_nodes = [
                {**n, "b": n["actor"] if n["actor"] in keys else branches[0]["key"],
                 "title": t.get("title") or ""}
                for n in nodes_all if n["tid"] == tid
            ]
            touching = [ev for ev in rows
                        if tid in _refs_from_payload(ev["payload"], max_task_id, known_task_ids)]
            msgs_t: list[dict] = []
            for ev in touching:
                payload = ev["payload"]
                if ev["event_type"] == "message.inbound" and payload.get("channel") == "internal" and ev["ts"] >= t0:
                    recipient = payload.get("to")
                    after = [x["ts"] for x in touching
                             if x["actor"] == f"agent:{recipient}" and x["ts"] > ev["ts"]]
                    msgs_t.append({
                        "h": rel_h(ev["ts"]),
                        "from": payload.get("from"),
                        "to": recipient,
                        "pick": rel_h(min(after)) if after else None,
                        "text": (payload.get("preview") or "").split("\n")[0][:200],
                    })
            st_t = [{"h": rel_h(tr["ts"]), "to": tr["to"], "from": tr["from"], "actor": tr["actor"]}
                    for tr in transitions if tr["task_id"] == tid]
            sg = [{"a": a, "s": s, "e": e, "k": k}
                  for a, s, e, k in sorted(segs.get(tid, []), key=lambda x: x[1])]
            relay_rows: list[str] = []
            for item in ([(x["s"], x["a"]) for x in sg]
                         + [(m["h"], m["from"]) for m in msgs_t]
                         + [(m["h"], m["to"]) for m in msgs_t]
                         + [(x["h"], x["actor"]) for x in st_t]):
                participant = item[1]
                if isinstance(participant, str) and participant and ":" not in participant and participant not in relay_rows:
                    relay_rows.append(participant)
            hs = ([x["s"] for x in sg] + [x["e"] for x in sg]
                  + [m["h"] for m in msgs_t] + [x["h"] for x in st_t])
            relay = {
                "rows": relay_rows,
                "segs": sg,
                "msgs": msgs_t,
                "status": st_t,
                "span": [min(hs), max(hs)] if hs else [0, 1],
                "steps": steps_for(tid, touching),
            }
            lanes_out.append({
                "name": f"#{tid}",
                "title": t.get("title") or "",
                "branches": branches,
                "nodes": lane_nodes,
                "relay": relay,
                "who": [a for a, _ in keep],
                "meta": f"{_project_of_task(tid, tasks_by_id, projects_by_id)} · "
                        f"{t.get('status') or '?'} · "
                        f"assignee {t.get('assignee') or 'nobody'}",
                "proj": _project_of_task(tid, tasks_by_id, projects_by_id),
                "total": round(week[tid]),
                "last": last_touch.get(tid),
            })
        return lanes_out

    def now_board() -> list[dict]:
        """Every agent with an event in the last 60 minutes, with the task
        their last 15 min of events point at."""
        if not rows:
            return []
        out: list[dict] = []
        t_now_ref = t_end
        for agent, evs in by_actor.items():
            last = max(ev["ts"] for ev in evs)
            age_min = (t_now_ref - last).total_seconds() / 60.0
            if age_min > 60:
                continue
            recent_evs = [ev for ev in evs
                          if (last - ev["ts"]).total_seconds() <= 900]
            c: dict[int, int] = collections.Counter()
            for ev in recent_evs:
                for ref in _refs_from_payload(ev["payload"], max_task_id, known_task_ids):
                    c[ref] += 1
            tid = c.most_common(1)[0][0] if c else None
            title = tasks_by_id[tid].get("title") if tid in tasks_by_id else None
            out.append({
                "a": agent,
                "age": round(age_min, 1),
                "tid": tid,
                "title": title,
                "proj": _project_of_task(tid, tasks_by_id, projects_by_id) if tid else None,
            })
        out.sort(key=lambda x: x["age"])
        return out

    # --- feed and raw ----------------------------------------------------------
    feed: list[dict] = []
    for ev in rows:
        payload = ev["payload"]
        if ev["event_type"] == "message.inbound" and payload.get("channel") == "internal":
            refs = _refs_from_payload(payload, max_task_id, known_task_ids)
            proj_name = (_project_of_task(refs[0], tasks_by_id, projects_by_id)
                         if refs else None)
            feed.append({
                "h": rel_h(ev["ts"]),
                "who": f"{payload.get('from') or '?'} -> {payload.get('to') or '?'}",
                "text": (payload.get("preview") or "").split("\n")[0][:160],
                "proj": proj_name,
                "tids": sorted(set(refs)),
                "kind": "message",
            })
    for n in nodes_all:
        tid = n["tid"]
        title_src = tasks_by_id.get(tid, {}).get("title", "") or ""
        feed.append({
            "h": n["h"],
            "who": n["actor"] or "?",
            "text": f"{_FEED_KIND_LABEL_EN.get(n['kind'], n['kind'])} #{tid} {title_src[:120]}",
            "proj": _project_of_task(tid, tasks_by_id, projects_by_id),
            "tids": [tid],
            "kind": n["kind"],
        })
    feed.sort(key=lambda x: -x["h"])
    raw_feed = [
        {
            "h": rel_h(ev["ts"]),
            "who": ev["actor"] or "?",
            "text": f"{ev['event_type']}  {(ev['entity_type'] or '')}/{(ev['entity_id'] or '')}",
        }
        for ev in raw_rows
    ]

    handoffs: dict[str, int] = collections.Counter()
    for f in feed:
        if f["kind"] == "message":
            handoffs[(f["proj"] or _NAME_NO_TASK)] += 1

    # --- compose response ------------------------------------------------------
    attribution = {
        "explicit_refs": sum(1 for _, tid, _, _ in samples if tid is not None),
        "inferred_refs": 0,
        "unattributed": sum(1 for _, tid, _, _ in samples if tid is None),
        "explicit_minutes": round(sum(m for _, tid, _, m in samples if tid is not None), 1),
        "inferred_minutes": 0.0,
        "unattributed_minutes": round(sum(m for _, tid, _, m in samples if tid is None), 1),
        "notes": notes + [
            "effort attribution: per-agent working stretches with gaps <= 5 min, "
            "split between task refs found inside the stretch (planner path, "
            "task_id payload, 4-digit number in message preview).",
            "about a quarter of agent time typically ties to no task "
            "(known gap in current attribution).",
        ],
    }

    return {
        "as_of": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_hours": window_hours,
        "t0": t0.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tend": t_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "H": hours,
        "project": build_view("project"),
        "agent": build_view("agent"),
        "task": build_tasks_view(),
        "now": now_board(),
        "feed": feed[:400],
        "raw": raw_feed,
        "handoffs": dict(handoffs),
        "events": total_events,
        "attribution": attribution,
        "schema_version": SCHEMA_VERSION,
    }


# Separate cache for /map -- different TTL from /now.
_map_cache_lock = threading.Lock()
_map_cache: dict[tuple, tuple[float, dict]] = {}


_WINDOW_LABEL_RE = re.compile(r"^(\d+)h$")


def _parse_window(window: str) -> int:
    """Parse a window label like '168h' into an integer hour count.

    Only values in _ALLOWED_WINDOWS_HOURS are accepted. Anything else is
    refused with 400 so the layer does not silently widen a query.
    """
    m = _WINDOW_LABEL_RE.match(window)
    if not m:
        raise HTTPException(
            status_code=400,
            detail={"error": "window_invalid",
                    "message": f"window must be one of {sorted(_ALLOWED_WINDOWS_HOURS)} hours (e.g. '168h')"},
        )
    hours = int(m.group(1))
    if hours not in _ALLOWED_WINDOWS_HOURS:
        raise HTTPException(
            status_code=400,
            detail={"error": "window_not_allowed",
                    "message": f"window must be one of {sorted(_ALLOWED_WINDOWS_HOURS)} hours"},
        )
    return hours


@router.get("/map", response_model=dict)
def work_map(
    request: Request,
    window: str = Query(default="168h"),
    fresh: int = Query(default=0, ge=0, le=1),
    principal: Principal = Depends(requires("work.read")),
):
    """Work Map data layer: 7-day (or 24h) weekly picture of where work
    actually goes, with the shape the Studio template expects.

    Same attribution rule as the lab mockup. Planner reads go through the
    planner HTTP surface only. Cache is 60 s; `?fresh=1` bypasses.
    """
    hours = _parse_window(window)
    cache_key = (principal.tenant_id, principal.cross_tenant, hours)
    if not fresh:
        with _map_cache_lock:
            hit = _map_cache.get(cache_key)
            if hit and (time.monotonic() - hit[0]) < MAP_CACHE_TTL_SECONDS:
                return hit[1]

    data = _build_map(principal, hours)

    with _map_cache_lock:
        _map_cache[cache_key] = (time.monotonic(), data)

    return data
