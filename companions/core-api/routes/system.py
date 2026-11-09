"""System status and summary endpoints.

GET /api/system/status -- aggregated health snapshot:
  - host (hostname + uptime)
  - db (smartnode reachable?)
  - heartbeat (last controller.heartbeat ts + delta)
  - registry counts (mcp_servers, crons, tools, integrations, secrets, agents)
  - service version

GET /api/system/summary?hours=N -- operational summary for the node over N hours.
  Deterministic: SQL and file reads only. No model call in this path.
  Returns verdict (ok/degraded/broken) plus reasons[], tool counts, refusals,
  agent activity, gateway stats, node timer health, health probe results, messages,
  and the active release objective. Absence is a value: what could not be read
  appears in degraded[] instead of being silently omitted.

GET /api/system/services, /api/system/crons -- the host as the host collector
  last saw it (companions, containers, units; timers and crontabs). The panel
  asks here, never the collector's files.

GET /api/system/runner, /api/system/models -- live state behind the Overview
  switches (agents paused; all model traffic stopped).
POST the same paths {action: start|stop[, confirm_token(s)]} -- the switches;
  browser session only, executed by admin-runner.

Every route declares its permission. The reads, /status included, need an
identity that holds system.status.read.
"""

import json
import logging
import os
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from auth import Principal, get_principal, require_session
from db import cursor
from permissions import requires
from lib.admin_runner import runner_auth_headers
from version import __version__

# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

_OP_LABELS: dict[str, str] = {
    "planner.comment":      "commenting on a task",
    "planner.list":         "listing tasks",
    "planner.task":         "reading a task",
    "planner.advance":      "advancing a task",
    "planner.file":         "filing a task",
    "planner.comments":     "reading task comments",
    "planner.search":       "searching tasks",
    "mesh.search":          "searching memory",
    "mesh.add":             "writing to memory",
    "mesh.get":             "reading from memory",
    "mesh.update":          "updating a memory note",
    "mesh.bytag":           "browsing memory by tag",
    "mesh.recent":          "reading recent memory",
    "doci.get":             "reading a document",
    "doci.create":          "writing a document",
    "doci.update":          "updating a document",
    "doci.search":          "searching documents",
    "doci.inbox":           "capturing to document inbox",
    "messaging.send_message": "sending a message",
    "messaging.inbox_read": "checking inbox",
    "messaging.inbox_reply": "replying to a message",
    "messaging.inbox_dismiss": "dismissing a message",
    "messages_search":      "searching messages",
    "system.summary":       "reading system summary",
    "node_summary":         "reading system summary",
}


def _render_last_action(event_type: str, payload: dict | None, entity_id: str | None) -> str:
    """Human-readable description of the last thing an agent did."""
    p = payload or {}
    op = p.get("op") or ""
    eid = p.get("entity_id") or entity_id or ""

    if event_type == "agent.woken":
        return "woken up"
    if event_type in ("tool.invoked", "tool.succeeded", "tool.failed"):
        label = _OP_LABELS.get(op)
        if not label:
            label = f"ran {op}" if op else event_type
        # Append entity reference when it adds meaning (task number, etc.)
        if eid and op and ("planner" in op):
            label = label.replace("a task", f"#{eid}")
        return label
    return event_type

# The planner's own base -- matches the proxy in routes/planner.py.
_PLANNER_URL = os.environ.get("PLANNER_API_URL", "").rstrip("/")
_PLANNER_TIMEOUT = 10
_GW_LOG = "/var/log/smart-node/gateway/service.log"


def _planner_key() -> str:
    return os.environ.get("PLANNER_API_KEY", "").strip()

log = logging.getLogger("core-api.system")

router = APIRouter()


def _read_uptime_secs() -> int | None:
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


def _read_loadavg() -> dict:
    try:
        with open("/proc/loadavg", "r") as f:
            p = f.read().split()
        return {"1m": float(p[0]), "5m": float(p[1]), "15m": float(p[2])}
    except Exception:
        return {}


def _read_meminfo() -> dict:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, _, v = line.partition(":")
                if v:
                    info[k.strip()] = int(v.strip().split()[0])  # value is in kB
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
        used_kb = max(0, total_kb - avail_kb)
        return {"total_mb": total_kb // 1024, "used_mb": used_kb // 1024}
    except Exception:
        return {}


def _read_disk() -> dict:
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = max(0, total - free)
        return {
            "total_gb": round(total / 1e9, 1),
            "used_gb": round(used / 1e9, 1),
            "pct": round(used / total * 100) if total else 0,
        }
    except Exception:
        return {}


@router.get("/status", response_model=dict, dependencies=[Depends(requires("system.status.read"))])
def system_status():
    now = datetime.now(timezone.utc)
    host = {
        "hostname": socket.gethostname(),
        "uptime_secs": _read_uptime_secs(),
    }

    db_status = {"reachable": False}
    counts = {}
    heartbeat = {"last_ts": None, "age_secs": None}

    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            db_status["reachable"] = True

            cur.execute(
                """
                SELECT ts FROM sn_core.events
                WHERE event_type = 'controller.heartbeat'
                ORDER BY id DESC LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                last_ts = row["ts"]
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                heartbeat["last_ts"] = last_ts.isoformat()
                heartbeat["age_secs"] = int((now - last_ts).total_seconds())

            count_specs = (
                ("mcp_servers", "SELECT COUNT(*) AS n FROM sn_config.mcp_servers"),
                ("crons", "SELECT COUNT(*) AS n FROM sn_core.crons"),
                ("crons_enabled", "SELECT COUNT(*) AS n FROM sn_core.crons WHERE enabled"),
                ("tools", "SELECT COUNT(*) AS n FROM sn_core.tools"),
                ("integrations", "SELECT COUNT(*) AS n FROM sn_config.integrations"),
                ("agents", "SELECT COUNT(*) AS n FROM sn_config.agents"),
                ("events_total", "SELECT COUNT(*) AS n FROM sn_core.events"),
            )
            for label, sql in count_specs:
                # SAVEPOINT per count so a single failure does not abort the txn.
                cur.execute("SAVEPOINT s_count")
                try:
                    cur.execute(sql)
                    counts[label] = cur.fetchone()["n"]
                    cur.execute("RELEASE SAVEPOINT s_count")
                except Exception as ex:
                    log.warning("count %s failed: %s", label, ex)
                    counts[label] = None
                    cur.execute("ROLLBACK TO SAVEPOINT s_count")
    except Exception as ex:
        log.warning("system status db probe failed: %s", ex)

    healthy = (
        db_status["reachable"]
        and heartbeat["age_secs"] is not None
        and heartbeat["age_secs"] < 120
    )

    return {
        "healthy": healthy,
        "version": __version__,
        "checked_at": now.isoformat(),
        "host": host,
        "db": db_status,
        "heartbeat": heartbeat,
        "registry": counts,
    }


@router.get("/overview", response_model=dict, dependencies=[Depends(requires("system.status.read"))])
def overview(principal: Principal = Depends(get_principal)):
    """Node overview for the Server page. Host metrics come from /proc (the node
    core-api runs on); events_24h is tenant-scoped. The container list needs the
    docker socket (not mounted in the default stack) so it stays empty here.
    """
    now = datetime.now(timezone.utc)
    with cursor() as cur:
        if principal.cross_tenant:
            cur.execute(
                "SELECT count(*) AS n FROM sn_core.events "
                "WHERE ts >= now() - interval '24 hours'"
            )
        else:
            cur.execute(
                "SELECT count(*) AS n FROM sn_core.events "
                "WHERE ts >= now() - interval '24 hours' AND tenant_id = %s",
                (principal.tenant_id,),
            )
        events_24h = cur.fetchone()["n"]

    return {
        "generated_at": now.isoformat(),
        "uptime_seconds": _read_uptime_secs(),
        "load": _read_loadavg(),
        "mem": _read_meminfo(),
        "disk": _read_disk(),
        # Needs the docker socket mounted; honest null/empty rather than faked.
        "containers": {"running": None, "names": []},
        "events_24h": events_24h,
    }


# The host collector (host-tools/sn-panel-data.sh, sn-panel-data.timer, every
# 30 s) writes what it sees on the host as JSON here. core-api is the only
# reader the panel talks to: the panel never learns this
# path, so the source can change without touching the panel.
HOST_DATA_DIR = "/srv/smart-node-hq/data"

# Four missed collector runs. Older than this the numbers are still returned
# but flagged stale, so the page can say so instead of showing them as live.
HOST_DATA_STALE_SECS = 120


def _read_host_data(name: str) -> dict:
    """One collector file plus its age. 503 when it is missing or unreadable:
    an absent collector is a fault to show, not an empty list to pass off."""
    path = os.path.join(HOST_DATA_DIR, name)
    try:
        age = datetime.now(timezone.utc).timestamp() - os.stat(path).st_mtime
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError) as ex:
        log.warning("host collector file %s unreadable: %s", name, ex)
        raise HTTPException(status_code=503, detail=f"host collector has no readable {name}")
    data["collector"] = {"age_secs": int(age), "stale": age > HOST_DATA_STALE_SECS}
    return data


@router.get("/services", response_model=dict, dependencies=[Depends(requires("system.status.read"))])
def services(principal: Principal = Depends(get_principal)):
    """What the node is made of, and is it up: companions (installer stages from
    companions.d, with version and install date), containers and systemd units,
    as the host collector last saw them.

    This used to read the agent-cron registry (retired) as "companions"
    and an sn_core.node_services table nothing fills, so the page showed zeros
    while the host had the real picture. The host inventory is
    node-global, so it is shown to every authenticated principal.
    """
    return _read_host_data("services.json")


@router.get("/crons", response_model=dict, dependencies=[Depends(requires("system.status.read"))])
def crons(principal: Principal = Depends(get_principal)):
    """What the host runs on a schedule: systemd timers and every crontab line,
    as the host collector last saw them. Agents are not on cron; the
    runner wakes them. Node-global, like /services."""
    return _read_host_data("crons.json")


# ---------------------------------------------------------------------------
# Overview switches: pause the agents, stop all model traffic
# ---------------------------------------------------------------------------
#
# Agents = the runner (pausing the node = stopping the runner).
# Models = every unit that talks to a model provider: the gateway (its CLI
# sessions run in its cgroup, so stopping it kills them too) and voice, which
# still calls Gemini directly with its own key. When voice goes through
# the gateway, it leaves this list.

RUNNER_UNIT = "smart-node-runner.service"
# The runtime is listed with the door: "models off" must also end the agent
# processes, or they sit retrying a closed door.
MODEL_UNITS = ("smart-node-gateway.service", "smart-node-runtime.service", "smart-node-voice.service")

# admin-runner is the node's door for privileged operations: owner gate, an
# audit row per call, confirm-before-destructive. core-api has no root and
# should not get any; it forwards the owner's request there. Its bind is the
# documented default (admin-runner README).
ADMIN_RUNNER_URL = "http://127.0.0.1:8041"


def _unit_state(unit: str) -> dict:
    """A unit's state, read live (no privilege needed to read it), so a switch
    shows the result of a click at once, not after the next collector sweep."""
    proc = subprocess.run(
        ["systemctl", "show", "-p", "ActiveState", "-p", "SubState",
         "-p", "ActiveEnterTimestamp", "-p", "InactiveEnterTimestamp",
         "-p", "UnitFileState", "-p", "LoadState", unit],
        capture_output=True, text=True, timeout=5,
    )
    kv = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    active = kv.get("ActiveState") or "unknown"
    since = kv.get("ActiveEnterTimestamp") if active == "active" else kv.get("InactiveEnterTimestamp")
    return {
        "unit": unit,
        "active": active,
        "sub": kv.get("SubState") or "",
        "since": since or None,
        "enabled": kv.get("UnitFileState") or "",
        # "not-found": no such unit on this node (its module is not installed)
        "installed": kv.get("LoadState") == "loaded",
    }


def _models_state() -> dict:
    """All model units as one switch: active only when every one is, inactive
    only when none is; anything between is 'partial' and the page says which.
    Only the units this node has: voice is a module, and on a node without it
    the switch read 'partial' and looked off while the gateway ran and agents
    answered."""
    units = [u for u in (_unit_state(u) for u in MODEL_UNITS) if u.get("installed", True)]
    up = [u["active"] == "active" for u in units]
    active = "active" if all(up) else ("inactive" if not any(up) else "partial")
    return {"active": active, "since": units[0]["since"], "units": units}


def _service_control(unit: str, action: str, token: str | None, username: str) -> dict:
    payload = {"args": {"unit": unit, "action": action}}
    if token:
        payload["confirm_token"] = str(token)
    req = urllib.request.Request(
        ADMIN_RUNNER_URL + "/operations/service_control",
        data=json.dumps(payload).encode(),
        method="POST",
        headers=runner_auth_headers(username),
    )
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.load(r)
    except urllib.error.HTTPError as ex:
        try:
            detail = json.load(ex).get("detail")
        except ValueError:
            detail = None
        raise HTTPException(status_code=ex.code, detail=detail or f"admin-runner answered {ex.code}")
    except (urllib.error.URLError, TimeoutError) as ex:
        log.warning("admin-runner unreachable: %s", ex)
        raise HTTPException(status_code=503, detail="admin-runner is not reachable")


def _action(body: dict) -> str:
    action = body.get("action")
    if action not in ("start", "stop"):
        raise HTTPException(status_code=400, detail="action must be start or stop")
    return action


@router.get("/runner", response_model=dict, dependencies=[Depends(requires("system.runner.read"))])
def runner_state(principal: Principal = Depends(get_principal)):
    return _unit_state(RUNNER_UNIT)


@router.post("/runner", response_model=dict, dependencies=[Depends(requires("system.runner.control"))])
def runner_control(body: dict = Body(...), principal: Principal = Depends(require_session)):
    """Start or stop the runner. Session only: an agent on the node can type
    `Remote-User: admin`, and stopping every agent is exactly the kind of
    decision a person must have made (auth.require_session).

    Two calls, as admin-runner wants for anything destructive: the first
    returns {needs_confirm, confirm_token}; the same body plus that token
    executes. admin-runner checks the owner role and writes admin.op.*
    events either way.
    """
    action = _action(body)
    out = _service_control(RUNNER_UNIT, action, body.get("confirm_token"), principal.username)
    out["state"] = _unit_state(RUNNER_UNIT)
    return out


@router.get("/models", response_model=dict, dependencies=[Depends(requires("system.status.read"))])
def models_state(principal: Principal = Depends(get_principal)):
    return _models_state()


@router.post("/models", response_model=dict, dependencies=[Depends(requires("system.models.control"))])
def models_control(body: dict = Body(...), principal: Principal = Depends(require_session)):
    """Emergency stop (and restart) of every unit that talks to a model
    provider. Same gate and two-step confirm as /runner, one admin-runner token
    per unit: the first call returns {needs_confirm, confirm_tokens: {unit:
    token}}, the second passes them back. Units go in list order (gateway
    first: it carries almost all the traffic); a unit that fails does not stop
    the next one from being tried, and each result is returned.
    """
    action = _action(body)
    tokens = body.get("confirm_tokens")
    units = [u["unit"] for u in _models_state()["units"]]   # the ones on this node
    if not isinstance(tokens, dict) or not tokens:
        staged = {}
        for unit in units:
            out = _service_control(unit, action, None, principal.username)
            if not out.get("needs_confirm"):
                raise HTTPException(status_code=502, detail=f"admin-runner ran {action} on {unit} without a confirm")
            staged[unit] = out["confirm_token"]
        return {"needs_confirm": True, "confirm_tokens": staged}
    results = {}
    for unit in units:
        try:
            results[unit] = _service_control(unit, action, tokens.get(unit), principal.username).get("result")
        except HTTPException as ex:
            results[unit] = {"exit_code": None, "stderr": str(ex.detail)}
    failed = [u for u, r in results.items() if not r or r.get("exit_code") != 0]
    return {"ok": not failed, "results": results, "failed": failed, "state": _models_state()}


# ---------------------------------------------------------------------------
# Summary endpoint
# ---------------------------------------------------------------------------

@router.get("/summary", response_model=dict, dependencies=[Depends(requires("system.status.read"))])
def system_summary(
    hours: float = Query(default=1.0, ge=0.0, le=168.0),
    principal: Principal = Depends(get_principal),
):
    """Operational summary for the node over the last N hours.

    All numbers are reproducible from the rows they came from. Absence is a
    value: a node timer that has never run reports last_run='never_run', not null.
    What could not be read appears in degraded[] rather than being silently
    omitted -- a summary that hides its own blind spots is how we got here.

    Thresholds (proposed 2026-09-01, pending architect approval): the rules the
    code applies are returned verbatim in the response's `thresholds` field;
    that list, not a copy here, is the reference.
    A verdict with no reasons[] is a bug.
    """
    now = datetime.now(timezone.utc)
    window_from = now - timedelta(hours=hours)
    degraded_notices: list[str] = []

    # Collect each section independently; a single bad section must not abort
    # the rest. SAVEPOINTs allow per-section rollback inside one transaction.

    tools_data: dict = {
        "invoked": 0, "succeeded": 0, "failed": 0, "unresolved": 0,
        "failures": [], "series": [],
    }
    refusals_list: list[dict] = []
    agents_data: dict = {
        "busy": [], "free": [], "wakes": 0, "by_agent": {}, "series": [],
        # "activity" is added below: one row per agent with status + last action
        "activity": [],
    }
    messages_list: list[dict] = []
    health_probe_data: dict = {"fails": [], "count_by_agent": {}}
    crons_list: list[dict] = []

    # Bucket size in seconds for time-series charts. Chosen so each window gives
    # roughly 12-48 data points, which is enough to show a curve without noise.
    if hours <= 2:
        bucket_secs = 300       # 5-minute buckets -> up to 24 points
    elif hours <= 12:
        bucket_secs = 1800      # 30-minute buckets -> up to 24 points
    elif hours <= 48:
        bucket_secs = 3600      # 1-hour buckets -> up to 48 points
    else:
        bucket_secs = 21600     # 6-hour buckets -> up to 28 points per week

    with cursor() as cur:

        # -- tool call counts --
        try:
            cur.execute("SAVEPOINT s_tools")
            cur.execute(
                "SELECT event_type, count(*) AS n FROM sn_core.events "
                "WHERE ts > %s GROUP BY event_type",
                (window_from,),
            )
            ev = {r["event_type"]: r["n"] for r in cur.fetchall()}
            inv = ev.get("tool.invoked", 0)
            suc = ev.get("tool.succeeded", 0)
            fai = ev.get("tool.failed", 0)
            tools_data["invoked"] = inv
            tools_data["succeeded"] = suc
            tools_data["failed"] = fai
            tools_data["unresolved"] = max(0, inv - suc - fai)
            cur.execute("RELEASE SAVEPOINT s_tools")
        except Exception as ex:
            degraded_notices.append(f"tools.counts: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_tools")

        # -- tool failures grouped by op and status --
        try:
            cur.execute("SAVEPOINT s_tfail")
            cur.execute(
                "SELECT payload->>'op' AS op, payload->>'status' AS status, count(*) AS n "
                "FROM sn_core.events "
                "WHERE ts > %s AND event_type = 'tool.failed' "
                "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20",
                (window_from,),
            )
            tools_data["failures"] = [
                {"op": r["op"], "status": r["status"], "count": r["n"]}
                for r in cur.fetchall()
            ]
            cur.execute("RELEASE SAVEPOINT s_tfail")
        except Exception as ex:
            degraded_notices.append(f"tools.failures: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_tfail")

        # -- tool time series (epoch-bucketed for arbitrary granularity) --
        try:
            cur.execute("SAVEPOINT s_tseries")
            cur.execute(
                "SELECT to_timestamp(floor(extract(epoch from ts) / %s) * %s) AS bucket, "
                "count(*) FILTER (WHERE event_type = 'tool.invoked') AS invoked, "
                "count(*) FILTER (WHERE event_type = 'tool.failed') AS failed "
                "FROM sn_core.events "
                "WHERE ts > %s AND event_type IN ('tool.invoked', 'tool.failed') "
                "GROUP BY 1 ORDER BY 1",
                (bucket_secs, bucket_secs, window_from),
            )
            tools_data["series"] = [
                {
                    "bucket": r["bucket"].isoformat(),
                    "invoked": r["invoked"],
                    "failed": r["failed"],
                }
                for r in cur.fetchall()
            ]
            cur.execute("RELEASE SAVEPOINT s_tseries")
        except Exception as ex:
            degraded_notices.append(f"tools.series: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_tseries")

        # -- agent wakes time series (epoch-bucketed) --
        try:
            cur.execute("SAVEPOINT s_aseries")
            cur.execute(
                "SELECT to_timestamp(floor(extract(epoch from ts) / %s) * %s) AS bucket, "
                "count(*) AS wakes "
                "FROM sn_core.events "
                "WHERE ts > %s AND event_type = 'agent.woken' "
                "GROUP BY 1 ORDER BY 1",
                (bucket_secs, bucket_secs, window_from),
            )
            agents_data["series"] = [
                {"bucket": r["bucket"].isoformat(), "wakes": r["wakes"]}
                for r in cur.fetchall()
            ]
            cur.execute("RELEASE SAVEPOINT s_aseries")
        except Exception as ex:
            degraded_notices.append(f"agents.series: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_aseries")

        # -- refusals (outcome=rejected), including reasonless ones --
        try:
            cur.execute("SAVEPOINT s_ref")
            cur.execute(
                "SELECT actor, payload->>'op' AS op, "
                "coalesce(payload->>'reason', 'no reason recorded') AS reason, "
                "count(*) AS n "
                "FROM sn_core.events "
                "WHERE ts > %s AND payload->>'outcome' = 'rejected' "
                "GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 30",
                (window_from,),
            )
            refusals_list = [
                {"actor": r["actor"], "op": r["op"], "reason": r["reason"], "count": r["n"]}
                for r in cur.fetchall()
            ]
            cur.execute("RELEASE SAVEPOINT s_ref")
        except Exception as ex:
            degraded_notices.append(f"refusals: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_ref")

        # -- agent runtime state + wakes + turns per agent --
        try:
            cur.execute("SAVEPOINT s_ag")
            cur.execute(
                "SELECT agent_slug, status FROM sn_core.agent_runtime ORDER BY agent_slug"
            )
            for r in cur.fetchall():
                (agents_data["busy"] if r["status"] == "busy" else agents_data["free"]).append(
                    r["agent_slug"]
                )

            cur.execute(
                "SELECT count(*) AS n FROM sn_core.events "
                "WHERE ts > %s AND event_type = 'agent.woken'",
                (window_from,),
            )
            row = cur.fetchone()
            agents_data["wakes"] = row["n"] if row else 0

            cur.execute(
                "SELECT split_part(actor, ':', 2) AS slug, count(*) AS n "
                "FROM sn_core.events "
                "WHERE ts > %s AND actor LIKE 'agent:%%' "
                "GROUP BY 1 ORDER BY 2 DESC",
                (window_from,),
            )
            agents_data["by_agent"] = {r["slug"]: r["n"] for r in cur.fetchall()}
            cur.execute("RELEASE SAVEPOINT s_ag")
        except Exception as ex:
            degraded_notices.append(f"agents: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_ag")

        # -- per-agent activity: 3 states (running / in_conversation / idle) --
        # Liveness = latest of three sources: agent_runtime, sn_core.events,
        # sn_core.messages. Source is reported so a stale row is visible.
        # in_conversation: no turn in flight but last message < 10 min ago.
        # Telegram bot names are resolved via sn_config.agent_channels.instance_ref.
        _IN_CONVO_SECS = 600
        try:
            cur.execute("SAVEPOINT s_activity")

            # All known agents with runtime state (including current_ref for running)
            cur.execute(
                "SELECT a.slug, a.title, rt.status, rt.updated_at, rt.current_ref "
                "FROM sn_config.agents a "
                "LEFT JOIN sn_core.agent_runtime rt ON rt.agent_slug = a.slug "
                "WHERE a.enabled AND a.archived_at IS NULL "
                "ORDER BY a.slug"
            )
            agent_rows = list(cur.fetchall())

            # Last event per agent (no cutoff; we want actual last event)
            cur.execute(
                "SELECT DISTINCT ON (split_part(actor, ':', 2)) "
                "  split_part(actor, ':', 2) AS slug, "
                "  event_type, entity_id, payload, ts "
                "FROM sn_core.events "
                "WHERE actor LIKE 'agent:%%' "
                "ORDER BY split_part(actor, ':', 2), ts DESC"
            )
            last_events: dict = {r["slug"]: r for r in cur.fetchall()}

            # Last message per agent: direct slug match (sender or recipient)
            cur.execute(
                "SELECT DISTINCT ON (ag_slug) ag_slug, received_at, channel_type "
                "FROM ( "
                "  SELECT sender AS ag_slug, received_at, channel_type "
                "  FROM sn_core.messages "
                "  WHERE sender IN "
                "    (SELECT slug FROM sn_config.agents WHERE archived_at IS NULL) "
                "  UNION ALL "
                "  SELECT recipient AS ag_slug, received_at, channel_type "
                "  FROM sn_core.messages "
                "  WHERE recipient IN "
                "    (SELECT slug FROM sn_config.agents WHERE archived_at IS NULL) "
                ") x ORDER BY ag_slug, received_at DESC"
            )
            last_msg_direct: dict = {r["ag_slug"]: dict(r) for r in cur.fetchall()}

            # Last message via bot name (e.g. telegram instance_ref -> agent slug)
            cur.execute(
                "SELECT DISTINCT ON (a.slug) "
                "  a.slug AS ag_slug, m.received_at, c.slug AS channel_type "
                "FROM sn_core.messages m "
                "JOIN sn_config.agent_channels ac "
                "  ON (m.sender = ac.instance_ref OR m.recipient = ac.instance_ref) "
                "  AND ac.instance_ref IS NOT NULL "
                "JOIN sn_config.agents a ON a.id = ac.agent_id "
                "JOIN sn_config.channels c ON c.id = ac.channel_id "
                "ORDER BY a.slug, m.received_at DESC"
            )
            last_msg_resolved: dict = {r["ag_slug"]: dict(r) for r in cur.fetchall()}

            # Per-agent channel breakdown (direct names): count + last_at per channel
            cur.execute(
                "SELECT ag_slug, channel_type, count(*) AS n, max(received_at) AS last_at "
                "FROM ( "
                "  SELECT sender AS ag_slug, channel_type, received_at "
                "  FROM sn_core.messages "
                "  WHERE sender IN "
                "    (SELECT slug FROM sn_config.agents WHERE archived_at IS NULL) "
                "  UNION ALL "
                "  SELECT recipient AS ag_slug, channel_type, received_at "
                "  FROM sn_core.messages "
                "  WHERE recipient IN "
                "    (SELECT slug FROM sn_config.agents WHERE archived_at IS NULL) "
                ") x GROUP BY ag_slug, channel_type ORDER BY ag_slug, max(received_at) DESC"
            )
            channels_by_agent: dict = {}
            for r in cur.fetchall():
                channels_by_agent.setdefault(r["ag_slug"], []).append({
                    "channel": r["channel_type"],
                    "count": r["n"],
                    "last_at": r["last_at"].isoformat() if r["last_at"] else None,
                })

            # Channel breakdown via bot name (merged in, no double-counting)
            cur.execute(
                "SELECT a.slug AS ag_slug, c.slug AS channel_type, "
                "  count(*) AS n, max(m.received_at) AS last_at "
                "FROM sn_core.messages m "
                "JOIN sn_config.agent_channels ac "
                "  ON (m.sender = ac.instance_ref OR m.recipient = ac.instance_ref) "
                "  AND ac.instance_ref IS NOT NULL "
                "JOIN sn_config.agents a ON a.id = ac.agent_id "
                "JOIN sn_config.channels c ON c.id = ac.channel_id "
                "GROUP BY a.slug, c.slug ORDER BY a.slug, max(m.received_at) DESC"
            )
            for r in cur.fetchall():
                s, ch = r["ag_slug"], r["channel_type"]
                existing = channels_by_agent.setdefault(s, [])
                if not any(x["channel"] == ch for x in existing):
                    existing.append({
                        "channel": ch,
                        "count": r["n"],
                        "last_at": r["last_at"].isoformat() if r["last_at"] else None,
                    })

            activity_rows = []
            for ag in agent_rows:
                slug = ag["slug"]
                rt_status = ag["status"] or "free"
                rt_ts = ag["updated_at"]

                ev = last_events.get(slug)
                ev_ts = ev["ts"] if ev else None

                # Pick newest message row: direct or bot-resolved
                dm = last_msg_direct.get(slug)
                rm = last_msg_resolved.get(slug)
                if dm and rm:
                    if rm["received_at"] >= dm["received_at"]:
                        msg_ts, msg_chan, msg_src = rm["received_at"], rm["channel_type"], "messages (resolved)"
                    else:
                        msg_ts, msg_chan, msg_src = dm["received_at"], dm["channel_type"], "messages"
                elif rm:
                    msg_ts, msg_chan, msg_src = rm["received_at"], rm["channel_type"], "messages (resolved)"
                elif dm:
                    msg_ts, msg_chan, msg_src = dm["received_at"], dm["channel_type"], "messages"
                else:
                    msg_ts = msg_chan = msg_src = None

                chans = sorted(
                    channels_by_agent.get(slug, []),
                    key=lambda x: x["last_at"] or "",
                    reverse=True,
                )

                if rt_status == "busy":
                    state_secs = int((now - rt_ts).total_seconds()) if rt_ts else None
                    activity_rows.append({
                        "slug": slug,
                        "title": ag["title"],
                        "state": "running",
                        "state_for_secs": state_secs,
                        "task_ref": ag.get("current_ref"),
                        "liveness_source": "runtime",
                        "channels": chans,
                    })
                else:
                    msg_age = int((now - msg_ts).total_seconds()) if msg_ts else None
                    if msg_ts and msg_age is not None and msg_age < _IN_CONVO_SECS:
                        activity_rows.append({
                            "slug": slug,
                            "title": ag["title"],
                            "state": "in_conversation",
                            "waiting_secs": msg_age,
                            "last_msg_channel": msg_chan,
                            "liveness_source": msg_src,
                            "channels": chans,
                        })
                    else:
                        # Idle: report from the latest source
                        candidates = [
                            (ts, src)
                            for ts, src in (
                                (ev_ts, "events"),
                                (msg_ts, msg_src),
                                (rt_ts, "runtime"),
                            )
                            if ts is not None
                        ]
                        if candidates:
                            latest_ts, latest_src = max(candidates, key=lambda x: x[0])
                        else:
                            latest_ts = latest_src = None
                        idle_secs = (
                            int((now - latest_ts).total_seconds()) if latest_ts else None
                        )
                        if ev:
                            p = ev["payload"] if isinstance(ev["payload"], dict) else {}
                            last_action = _render_last_action(
                                ev["event_type"], p, ev["entity_id"]
                            )
                            last_action_ts = ev_ts.isoformat() if ev_ts else None
                        else:
                            last_action = last_action_ts = None

                        activity_rows.append({
                            "slug": slug,
                            "title": ag["title"],
                            "state": "idle",
                            "idle_for_secs": idle_secs,
                            "last_action": last_action,
                            "last_action_ts": last_action_ts,
                            "liveness_source": latest_src,
                            "channels": chans,
                        })

            agents_data["activity"] = activity_rows
            cur.execute("RELEASE SAVEPOINT s_activity")
        except Exception as ex:
            degraded_notices.append(f"agents.activity: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_activity")

        # -- inbound/outbound messages per channel --
        try:
            cur.execute("SAVEPOINT s_msg")
            cur.execute(
                "SELECT channel_type, direction, count(*) AS n "
                "FROM sn_core.messages "
                "WHERE received_at > %s "
                "GROUP BY 1, 2 ORDER BY 1, 2",
                (window_from,),
            )
            messages_list = [
                {"channel": r["channel_type"], "direction": r["direction"], "count": r["n"]}
                for r in cur.fetchall()
            ]
            cur.execute("RELEASE SAVEPOINT s_msg")
        except Exception as ex:
            degraded_notices.append(f"messages: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_msg")

        # -- health probe: FAIL rows from sn_core.agent_health_checks --
        try:
            cur.execute("SAVEPOINT s_probe")
            cur.execute(
                "SELECT agent_slug, grant_name, check_type, error_detail, last_checked_at "
                "FROM sn_core.agent_health_checks WHERE status = 'fail' "
                "ORDER BY agent_slug, grant_name"
            )
            fails = []
            by_agent: dict[str, int] = {}
            for r in cur.fetchall():
                fails.append({
                    "agent": r["agent_slug"],
                    "grant": r["grant_name"],
                    "type": r["check_type"],
                    "error": r["error_detail"],
                    "last_checked": (
                        r["last_checked_at"].isoformat() if r["last_checked_at"] else None
                    ),
                })
                by_agent[r["agent_slug"]] = by_agent.get(r["agent_slug"], 0) + 1
            health_probe_data["fails"] = fails
            health_probe_data["count_by_agent"] = by_agent
            cur.execute("RELEASE SAVEPOINT s_probe")
        except Exception as ex:
            degraded_notices.append(f"health_probe: {ex}")
            cur.execute("ROLLBACK TO SAVEPOINT s_probe")

        # -- scheduled jobs: the node's own timers as the host collector sees
        # them. This used to read the agent-cron registry
        # (sn_core.crons / cron_runs), retired now and empty, so the
        # section and its verdict rules saw nothing at all.
        try:
            timers = _read_host_data("crons.json").get("timers", [])
            units = {u["name"]: u for u in _read_host_data("services.json").get("systemd", [])}
            for t in timers:
                if t.get("scope") != "smart-node":
                    continue
                result = units.get(t.get("target"), {}).get("last_result")
                crons_list.append({
                    "slug": t["name"].removesuffix(".timer"),
                    "schedule": t.get("schedule"),
                    "enabled": True,
                    "last_run": t.get("last_iso") or "never_run",
                    # systemd's Result of the last run: success, exit-code, signal, timeout, ...
                    "result": result,
                    "exit_code": 0 if result == "success" else None,
                    "last_error": None if result in (None, "success") else f"last run: {result}",
                })
        except HTTPException as ex:
            degraded_notices.append(f"crons: {ex.detail}")

    # -- gateway log (no timestamps; whole-file counts; stated in response) --
    gateway_data: dict = {"cap_refusals": None, "http_502": None}
    try:
        txt = open(_GW_LOG, errors="replace").read()
        gateway_data["cap_refusals"] = txt.count("agent spawn cap reached")
        gateway_data["http_502"] = txt.count("502 Bad Gateway")
        gateway_data["coverage"] = (
            "whole log file: gateway log carries no timestamps; "
            "window scoping not possible until timestamps are added"
        )
    except FileNotFoundError:
        degraded_notices.append(f"gateway log not readable: {_GW_LOG} not found")
        gateway_data["coverage"] = "log not found"
    except Exception as ex:
        degraded_notices.append(f"gateway log: {ex}")
        gateway_data["coverage"] = f"read error: {ex}"

    # -- release: active release project (project 79) tasks from planner --
    release_data: dict = {}
    try:
        key = _planner_key()
        req = urllib.request.Request(
            f"{_PLANNER_URL}/api/v1/tasks?project_id=79",
            headers={"X-API-Key": key} if key else {},
        )
        with urllib.request.urlopen(req, timeout=_PLANNER_TIMEOUT) as r:
            payload = json.loads(r.read())
        tasks = payload if isinstance(payload, list) else payload.get("tasks", payload)
        by_status: dict[str, int] = {}
        for t in tasks:
            s = t.get("stage") or t.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
        release_data = {
            "project_id": 79,
            "task_count": sum(by_status.values()),
            "by_status": by_status,
            "pending_review": by_status.get("pending_review", 0),
        }
    except Exception as ex:
        degraded_notices.append(f"release (planner project 79): {ex}")

    # -- verdict --
    # Thresholds are proposed; see docstring above and pending architect review.
    invoked = tools_data["invoked"]
    failed = tools_data["failed"]
    unresolved = tools_data["unresolved"]
    probe_fail_count = len(health_probe_data["fails"])
    gw_502 = gateway_data.get("http_502") or 0
    gw_cap = gateway_data.get("cap_refusals") or 0

    reasons: list[str] = []

    if failed > 0:
        pct = f" ({failed / invoked:.0%} of {invoked})" if invoked > 0 else ""
        reasons.append(f"{failed} tool calls failed{pct}")

    if unresolved > 0:
        pct = f" ({unresolved / invoked:.0%} of {invoked})" if invoked > 0 else ""
        reasons.append(f"{unresolved} tool calls started and never finished{pct}")

    anon_refusals = sum(
        r["count"] for r in refusals_list
        if r.get("actor", "") in ("user:anonymous", "anonymous")
    )
    if anon_refusals > 10:
        reasons.append(f"user:anonymous refused {anon_refusals}x in window")

    if probe_fail_count > 0:
        agents_affected = len(health_probe_data["count_by_agent"])
        reasons.append(
            f"health probe: {probe_fail_count} failing checks across {agents_affected} agents"
        )

    for c in crons_list:
        if c["last_run"] == "never_run" and c["enabled"]:
            reasons.append(f"timer {c['slug']} has never run")
        elif c.get("result") not in (None, "success"):
            reasons.append(f"timer {c['slug']} last run: {c['result']}")

    if gw_502 > 0:
        reasons.append(f"gateway: {gw_502} HTTP 502s (whole log)")
    if gw_cap > 0:
        reasons.append(f"gateway: {gw_cap} spawn-cap refusals (whole log)")

    # Tier determination (thresholds proposed 2026-09-01, pending architect approval;
    # written verbatim in the response so every caller can see what decided it).
    is_broken = (
        (invoked > 0 and failed / invoked > 0.25)
        or gw_502 > 0
    )
    is_degraded = (
        (invoked > 0 and (failed / invoked > 0.10 or unresolved / invoked > 0.05))
        or probe_fail_count > 0
        or any(c["last_run"] == "never_run" and c["enabled"] for c in crons_list)
        or any(c.get("result") not in (None, "success") for c in crons_list)
        or gw_cap > 0
        or anon_refusals > 10
    )

    if is_broken:
        verdict = "broken"
    elif is_degraded or reasons:
        verdict = "degraded"
    else:
        verdict = "ok"

    return {
        "window": {
            "hours": hours,
            "bucket_seconds": bucket_secs,
            "from": window_from.isoformat(),
            "to": now.isoformat(),
            "generated_at": now.isoformat(),
        },
        "verdict": verdict,
        "reasons": reasons,
        "thresholds": {
            "broken": [
                "tool failure rate > 25% of invoked",
                "gateway log contains any 502 (whole-log; no timestamps on gw log)",
            ],
            "degraded": [
                "tool failure rate > 10% of invoked",
                "unresolved tools > 5% of invoked",
                "any health_probe row in fail state",
                "any node timer that has never run",
                "any node timer whose last run did not succeed",
                "gateway log contains any spawn-cap refusal (whole-log)",
                "user:anonymous refusals > 10 in window",
            ],
            "note": "proposed 2026-09-01; pending architect approval",
        },
        "tools": tools_data,
        "refusals": refusals_list,
        "agents": agents_data,
        "gateway": gateway_data,
        "crons": crons_list,
        "health_probe": health_probe_data,
        "messages": messages_list,
        "release": release_data,
        "degraded": degraded_notices,
    }
