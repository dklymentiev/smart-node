"""Event log read+write endpoints.

GET  /api/events  -- paginated list, filterable by actor/event_type/entity_type/entity_id/since
POST /api/events  -- emit an event (loopback / Docker-network only, for bash companions)

The in-process emit() helper (events_emit.emit) is the path used by other
route modules and any Python code inside core-api. This HTTP endpoint
exists for bash companions (heartbeat.sh, cron-runner.sh, etc.).
"""

import ipaddress
import logging
import os
from datetime import datetime, timezone
from typing import Any

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from auth import Principal, get_principal
from db import cursor
import event_stream
from events_emit import emit
from permissions import requires
from schemas.event import EventCreate

log = logging.getLogger("core-api.events")

router = APIRouter()

_docker_cidr = os.environ.get("SN_DOCKER_CIDR", "")
DOCKER0_NET = ipaddress.ip_network(_docker_cidr, strict=False) if _docker_cidr else None


def _trusted(request: Request) -> bool:
    client_ip = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return ip.is_loopback or (DOCKER0_NET is not None and ip in DOCKER0_NET)


@router.get("", response_model=dict,
            dependencies=[Depends(requires("events.read"))])
def list_events(
    since: datetime | None = None,
    actor: str | None = None,
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    principal: Principal = Depends(get_principal),
):
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be 1..1000")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    where: list[str] = []
    params: list[Any] = []
    if not principal.cross_tenant:
        where.append("tenant_id = %s")
        params.append(principal.tenant_id)
    if since:
        where.append("ts >= %s")
        params.append(since)
    if actor:
        where.append("actor = %s")
        params.append(actor)
    if event_type:
        where.append("event_type = %s")
        params.append(event_type)
    if entity_type:
        where.append("entity_type = %s")
        params.append(entity_type)
    if entity_id:
        where.append("entity_id = %s")
        params.append(entity_id)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.extend([limit, offset])
    sql = f"""
        SELECT id, ts, actor, event_type, entity_type, entity_id, payload, parent_event_id
        FROM sn_core.events
        {where_clause}
        ORDER BY ts DESC, id DESC
        LIMIT %s OFFSET %s
    """
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return {
        "events": rows,
        "count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# Window (hours) -> bucket width (seconds). About 30-50 buckets per window, so
# the panel's columns stay readable whatever the range.
_HIST_BUCKETS = {1: 120, 6: 900, 24: 3600, 168: 6 * 3600}
# The families the panel stacks; everything else is "other".
_HIST_FAMILIES = ("tool", "agent", "session", "admin")
# Problems drawn in their own strip under the activity chart.
_HIST_MSG_BLOCKED = ("message.blocked", "message.routing_denied", "message.content_blocked")


@router.get("/stream", dependencies=[Depends(requires("desks.read"))])
async def stream_events(request: Request, principal: Principal = Depends(get_principal)):
    """The caller's live events (event_stream.py): one line per event about a
    chat they own, {"id", "type", "desk"}, as server-sent events. A comment
    line every 25 s keeps proxies from closing a quiet stream."""
    key = (principal.tenant_id, principal.user_id)
    q = event_stream.subscribe(*key)
    return _sse(request, q, lambda: event_stream.unsubscribe(*key, q))


@router.get("/wakes", dependencies=[Depends(requires("agents.read"))])
async def stream_wakes(request: Request):
    """For the Runner: one line {"type"} each time something that may be an
    agent's work arrives (event_stream.WAKE_TYPES), so it looks for work at
    once instead of on its next 5 s poll."""
    q = event_stream.subscribe_wakes()
    return _sse(request, q, lambda: event_stream.unsubscribe_wakes(q))


def _sse(request: Request, q: asyncio.Queue, done) -> StreamingResponse:
    async def lines():
        try:
            yield ": open\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            done()

    return StreamingResponse(lines(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/histogram", response_model=dict,
            dependencies=[Depends(requires("events.read"))])
def events_histogram(hours: int = 24, principal: Principal = Depends(get_principal)):
    """Event counts per time bucket for the panel's events chart.

    The events table only loads the newest rows (a few hundred, about an hour),
    so a day's rhythm has to be counted here, not in the browser. Every bucket in
    the window is returned, empty ones as zeros, so columns line up in time.
    """
    if hours not in _HIST_BUCKETS:
        raise HTTPException(status_code=400, detail="hours must be one of 1, 6, 24, 168")
    width = _HIST_BUCKETS[hours]
    params: list[Any] = [width, width, list(_HIST_MSG_BLOCKED), hours]
    tenant_sql = ""
    if not principal.cross_tenant:
        tenant_sql = "AND tenant_id = %s"
        params.append(principal.tenant_id)
    sql = f"""
        SELECT floor(extract(epoch FROM ts) / %s)::bigint * %s AS b,
               split_part(event_type, '.', 1) AS fam,
               count(*) AS n,
               count(*) FILTER (WHERE event_type = 'secret.access_denied') AS secret_denied,
               count(*) FILTER (WHERE event_type = 'tool.failed')           AS tool_failed,
               count(*) FILTER (WHERE event_type = ANY(%s))                 AS message_blocked
        FROM sn_core.events
        WHERE ts > now() - make_interval(hours => %s) {tenant_sql}
        GROUP BY 1, 2
    """
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    now = int(datetime.now(timezone.utc).timestamp())
    first = (now - hours * 3600) // width * width
    buckets = {}
    for t in range(first, now + 1, width):
        buckets[t] = {"t": t, **{f: 0 for f in _HIST_FAMILIES}, "other": 0,
                      "secret_denied": 0, "tool_failed": 0, "message_blocked": 0}
    for r in rows:
        bk = buckets.get(int(r["b"]))
        if bk is None:
            continue
        fam = r["fam"] if r["fam"] in _HIST_FAMILIES else "other"
        bk[fam] += int(r["n"])
        for k in ("secret_denied", "tool_failed", "message_blocked"):
            bk[k] += int(r[k])
    return {
        "hours": hours,
        "bucket_seconds": width,
        "families": list(_HIST_FAMILIES) + ["other"],
        "buckets": [buckets[t] for t in sorted(buckets)],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(requires("internal.events.write"))])
def post_event(payload: EventCreate, request: Request):
    # Held by no role. The writers are the node's own services, which prove
    # themselves, and its root tools, which reach this as the owner. The actor
    # recorded is the one the writer names in the body, as before.
    with cursor(commit=True) as cur:
        row = emit(
            cur,
            actor=payload.actor,
            event_type=payload.event_type,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            payload=payload.payload,
            parent_event_id=payload.parent_event_id,
        )
    return {"id": row["id"], "ts": row["ts"].isoformat()}
