"""Project state facts layer -- all eight sources, one HTTP read.

GET /api/project-state/collect?slug=<slug>&since=<iso|-NDd|-NDh>&until=<iso>

One deterministic read across the sources (planner, mesh, Doci,
sn_core.messages). Returns the raw rows and counts the agent needs to
write a fixed-shape stand-up card for one project over one window. The
collector produces facts; the agent writes prose ("facts, not narrative").

Contract:

- Planner is read through the planner HTTP surface only, never by DB
  connection to the planner schema (Clause 3). Mesh and Doci are read
  through their own HTTP APIs, never their storage. Owner channels
  (sn_core.messages) ARE core-api's own data: no HTTP hop, just the
  shared `db.cursor()` every core-api route uses on sn_core.
- The collector is deterministic: no model in this module.
- Cache: 30 seconds by default. `?fresh=1` bypasses. One shared cache
  keyed by (slug, since, until, cross_tenant).
- Permission: reuses `work.read`, since the layer lives next to
  /api/work/* as a sibling facts projection of the work state. A dedicated
  project_state.read permission can be split out if the grant model has to differ later.

Window:

- `since` accepts ISO 8601 UTC (`2026-10-10T00:00:00Z`), a relative
  shorthand `-7d` / `-72h` / `-30m`, or an unqualified date
  (`2026-10-10`, interpreted as that day's midnight UTC).
- `until` is optional and defaults to the current UTC time.

The route surfaces blind spots (no planner URL, slug not found, missing
collector sources) in a top-level `notes` list rather than 500ing: the
card is useful even with a partial read. A missing slug returns 404
because the question has no answer.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import Principal
from permissions import requires
from lib import agent_day as agent_day_lib
from lib import project_state as collector

log = logging.getLogger("core-api.project_state")

router = APIRouter(prefix="/api/project-state", tags=["project-state"])

# 30-second TTL. The underlying planner walk is bounded at 20000 rows per
# filter and three filters are run (project / objective / tag, though tag
# is skipped in this slice). A 30-second cache keeps planner fanout cheap
# for repeated reads while a stand-up is being written.
CACHE_TTL_SECONDS = 30

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}

# Relative-window shorthand: -<integer><unit>, unit in d / h / m.
_REL_RE = re.compile(r"^-(\d+)([dhm])$")

# Bare date form: YYYY-MM-DD (treated as that day's 00:00:00 UTC).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_window(value: str, now: datetime) -> datetime:
    """Parse a since/until spec into an aware UTC datetime.

    Accepts:
      - "-7d", "-72h", "-30m" (relative to `now`)
      - "2026-10-10" (bare date; 00:00:00 UTC)
      - full ISO 8601 ("2026-10-10T12:00:00Z" or with offset)

    Raises HTTPException(400) with a clear message if the spec cannot be
    parsed. The spec stays small on purpose: parsing surprises are the
    kind of thing that costs mornings.
    """
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="window: empty value")

    m = _REL_RE.match(text)
    if m:
        qty = int(m.group(1))
        unit = m.group(2)
        if unit == "d":
            delta = timedelta(days=qty)
        elif unit == "h":
            delta = timedelta(hours=qty)
        else:
            delta = timedelta(minutes=qty)
        return now - delta

    if _DATE_RE.match(text):
        try:
            d = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as ex:
            raise HTTPException(status_code=400,
                                detail=f"window: cannot parse date {text!r}: {ex}") from ex
        return d.replace(tzinfo=timezone.utc)

    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as ex:
        raise HTTPException(
            status_code=400,
            detail=(
                f"window: cannot parse {text!r}. Use ISO 8601 "
                f"(2026-10-10T00:00:00Z), a bare date (2026-10-10), "
                f"or a relative shorthand (-7d, -72h, -30m)."
            ),
        ) from ex
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/collect", response_model=dict)
def project_state_collect(
    request: Request,
    slug: str = Query(..., min_length=1, max_length=128,
                      description="Planner project slug (e.g. flint, smart-node)."),
    since: str = Query(..., description=(
        "Start of the window. ISO 8601 UTC (2026-10-10T00:00:00Z), bare "
        "date (2026-10-10), or relative shorthand (-7d, -72h, -30m).")),
    until: str | None = Query(default=None, description=(
        "End of the window; defaults to now when omitted. Same formats as since.")),
    fresh: int = Query(default=0, ge=0, le=1,
                       description="1 bypasses the 30-second cache."),
    principal: Principal = Depends(requires("work.read")),
):
    """Collect project state facts for one project over one window.

    Reads all eight sources: goals, linked tasks, moved, comments,
    cross-project mentions, mesh memory, Doci documents, owner channels.
    Any single source failure is surfaced in `notes` so a partial card
    is still useful.

    Also emits two derived fields the card needs:
    - `waiting_for_owner`: linked tasks in awaiting_approval or
      escalated, of any age, oldest first.
    - `where_it_stopped`: the most recent linked-task move across all
      time, populated only when the window has no `moved` rows.
    """
    now = datetime.now(timezone.utc)
    since_dt = _parse_window(since, now)
    until_dt = _parse_window(until, now) if until else now
    if since_dt >= until_dt:
        raise HTTPException(
            status_code=400,
            detail=f"window: since ({since_dt.isoformat()}) is not before "
                   f"until ({until_dt.isoformat()}).",
        )

    cache_key = (
        slug,
        since_dt.isoformat(),
        until_dt.isoformat(),
        principal.cross_tenant,
    )
    if not fresh:
        with _cache_lock:
            hit = _cache.get(cache_key)
            if hit and (time.monotonic() - hit[0]) < CACHE_TTL_SECONDS:
                return hit[1]

    data = collector.collect(slug, since_dt, until_dt)

    if data.get("project") is None:
        # Slug does not resolve. 404 is the right answer: the question
        # has none, and the caller should fix the slug rather than
        # paper over an empty card.
        raise HTTPException(
            status_code=404,
            detail={
                "error": "project_not_found",
                "slug": slug,
                "notes": data.get("notes", []),
            },
        )

    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), data)

    return data


# ---------------------------------------------------------------- agent's day

_DAY_CACHE_TTL_SECONDS = 30
_day_cache_lock = threading.Lock()
_day_cache: dict[tuple, tuple[float, dict]] = {}
_AGENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _agent_behind(principal: Principal) -> str | None:
    """The agent slug when the caller is an agent (directly, or a service acting
    for one), None for a person or a whole-node seat."""
    if getattr(principal, "actor_kind", None) != "agent":
        return None
    aid = principal.actor_id or ""
    slug = aid.split(":", 1)[1] if ":" in aid else (principal.username or "")
    return slug.strip().lower() or None


@router.get("/agent-day", response_model=dict)
def project_state_agent_day(
    agent: str | None = Query(default=None, max_length=64,
                              description="Agent slug. Ignored for an agent caller, who reads only itself."),
    hours: int = Query(default=24, ge=1, le=168, description="Look-back window in hours."),
    fresh: int = Query(default=0, ge=0, le=1, description="1 bypasses the 30-second cache."),
    principal: Principal = Depends(requires("work.read")),
):
    """What one agent did and was asked to do in the last `hours`, and which tasks
    name it as the doer and have not been started.

    Facts only, assembled from the event journal, the planner and the internal
    mail (lib/agent_day.py). An agent may read only its own day; a person or a
    whole-node seat names the agent. Used by brin to fold a "your day" block into
    the prompt of a wake, and by anyone who asks what an agent has been doing.
    """
    own = _agent_behind(principal)
    if own:
        if agent and agent.strip().lower() != own:
            raise HTTPException(status_code=403, detail="an agent may read only its own day")
        agent = own
    agent = (agent or "").strip().lower()
    if not agent:
        raise HTTPException(status_code=400, detail="agent is required")
    if not _AGENT_SLUG_RE.match(agent):
        raise HTTPException(status_code=400, detail="agent is not a valid slug")

    key = (agent, hours)
    if not fresh:
        with _day_cache_lock:
            hit = _day_cache.get(key)
            if hit and (time.monotonic() - hit[0]) < _DAY_CACHE_TTL_SECONDS:
                return hit[1]

    now = datetime.now(timezone.utc)
    data = agent_day_lib.collect(agent, now - timedelta(hours=hours), now)
    with _day_cache_lock:
        _day_cache[key] = (time.monotonic(), data)
    return data
