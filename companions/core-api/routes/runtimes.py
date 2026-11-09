"""CRUD endpoints for the Runtimes registry (sn_config.runtimes).

Mirror of routes/integrations.py -- different table, no tools_count
(runtimes don't expose tools; agents do, bound to a runtime).

POST is upsert-by-slug. Defensive policy on upsert for purpose and
install_path matches integrations -- a re-running installer that doesn't
yet pass those fields won't blank them.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from auth import Principal, get_actor
from db import cursor
from permissions import requires
from events_emit import emit
from schemas.runtime import Runtime, RuntimeCreate, RuntimeUpdate

log = logging.getLogger("core-api.runtimes")

router = APIRouter(prefix="/api/runtimes", tags=["runtimes"])


SELECT_COLS = (
    "id, slug, label, purpose, description, kind, status, "
    "install_uri, install_path, companion_slug, metadata, "
    "created_at, updated_at, archived_at"
)


def _row(r) -> Runtime:
    return Runtime(**r)


@router.get("", response_model=dict, dependencies=[Depends(requires("runtimes.read"))])
def list_runtimes(
    status_filter: str | None = None,
    kind: str | None = None,
    companion_slug: str | None = None,
    include_archived: bool = False,
):
    where, params = [], []
    if status_filter:
        where.append("status = %s"); params.append(status_filter)
    if kind:
        where.append("kind = %s"); params.append(kind)
    if companion_slug:
        where.append("companion_slug = %s"); params.append(companion_slug)
    if not include_archived:
        where.append("archived_at IS NULL")
    sql = f"SELECT {SELECT_COLS} FROM sn_config.runtimes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY slug"
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return {"runtimes": [Runtime(**r).model_dump(mode="json") for r in rows]}


# ---------------------------------------------------------------------------
# Live view. The registry above is empty on example-node: nothing writes
# it (the agent-runtimes companion installs CLIs but registers no rows), so the
# page showed nothing while 13 agents ran on two engines. This view is read
# from the node itself instead.
#
# A runtime here = whatever drives an agent's turn loop (model -> tool ->
# model): a coding CLI (Claude Code, Codex, Hermes), or brin, the node's own
# loop, for an agent on a model API with no CLI (brin's _maybe_run_tool_loop;
# CLI agents are "opaque" to it and only pass through). Model APIs themselves
# are the gateway's providers, shown on AI -> Providers.
# Sources:
#   - the gateway's /api/v1/health: which provider backends it has and whether
#     each is ok / not_configured (its CLI backend is named `cli`);
#   - the CLI binaries on the host and their --version;
#   - sn_config.agents.model ("<provider>/<model>") for how many agents use it;
#   - sn_core.ai_sessions.provider for sessions in the last 7 days.
# The CLI list is the set of agent CLIs the node knows how to talk about; a
# CLI with no gateway backend is shown as installed but not wired.
# ---------------------------------------------------------------------------

import shutil
import subprocess
import time
import json
import os
import urllib.request

from routes.ai import _gateway_health, GATEWAY_HEALTH_TIMEOUT
from routes.system import _read_host_data

_RUNTIMES = (
    # slug,          label,              kind,  gateway provider, model prefix, cli binary
    ("claude-code",  "Claude Code",      "cli", "claude-cli", "claude-cli", "claude"),
    ("flint",        "Flint",            "cli", "flint-cli",  "flint-cli",  "flint"),
    ("codex",        "Codex",            "cli", None,         "codex",      "codex"),
    ("hermes",       "Hermes",           "cli", None,         "hermes",     "hermes"),
)
_VERSION_TTL = 300.0
_version_cache: dict[str, tuple[float, str | None]] = {}


def _gateway_cli_backends() -> dict[str, dict] | None:
    """Runtime readiness from the gateway dispatcher, not the legacy health list."""
    gw = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        with urllib.request.urlopen(gw + "/api/v1/backends", timeout=GATEWAY_HEALTH_TIMEOUT) as r:
            rows = json.loads(r.read() or b"{}").get("backends") or []
        return {row["id"]: row for row in rows}
    except Exception as exc:
        log.warning("gateway backend probe failed: %s", exc)
        return None


def _cli_version(binary: str) -> tuple[str | None, str | None]:
    """(path, version) of a CLI on the host, version cached for five minutes."""
    path = shutil.which(binary, path="/usr/local/bin:/usr/bin:/bin")
    if not path:
        return None, None
    hit = _version_cache.get(path)
    if hit and time.monotonic() - hit[0] < _VERSION_TTL:
        return path, hit[1]
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=8, env={"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin"})
        version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
    except Exception:
        version = None
    _version_cache[path] = (time.monotonic(), version)
    return path, version


@router.get("/live", response_model=dict, dependencies=[Depends(requires("runtimes.read"))])
def live_runtimes():
    health = _gateway_health()
    backend_rows = _gateway_cli_backends()
    legacy_providers = (health or {}).get("providers") or {}
    with cursor() as cur:
        cur.execute(
            "SELECT split_part(model, '/', 1) AS prefix, count(*) AS n, "
            "string_agg(slug, ',' ORDER BY slug) AS slugs "
            "FROM sn_config.agents WHERE archived_at IS NULL AND model LIKE '%%/%%' GROUP BY 1"
        )
        agents = {r["prefix"]: r for r in cur.fetchall()}
        cur.execute(
            "SELECT provider, count(*) AS n FROM sn_core.ai_sessions "
            "WHERE created_at > now() - interval '7 days' GROUP BY 1"
        )
        sessions = {r["provider"]: r["n"] for r in cur.fetchall()}
    out = []
    for slug, label, kind, provider, prefix, binary in _RUNTIMES:
        path, version = _cli_version(binary) if binary else (None, None)
        backend = (backend_rows or {}).get(provider) if provider else None
        legacy = legacy_providers.get("cli") if slug == "claude-code" else None
        gw_status = (backend or {}).get("status") if backend else (legacy or {}).get("status")
        if binary and not path:
            live, reason = "not installed", f"no {binary} binary on the host"
        elif backend_rows is None and slug == "flint":
            live, reason = "unknown", "the gateway backend registry does not answer"
        elif backend_rows is None and health is None and provider:
            live, reason = "unknown", "the gateway does not answer"
        elif not provider:
            live, reason = "not wired", f"{binary} is installed, but the gateway has no backend that runs it"
        elif gw_status in ("ok", "available"):
            live, reason = "available", f"gateway backend `{provider}` available"
        elif gw_status:
            live = gw_status.replace("_", " ")
            reason = (backend or {}).get("reason") or f"gateway backend `{provider}`: {gw_status}"
        else:
            live, reason = "not wired", f"the gateway reports no `{provider}` backend"
        a = agents.get(prefix)
        out.append({
            "slug": slug, "label": label, "kind": kind,
            "binary": path, "version": version,
            "gateway_provider": provider, "gateway_status": gw_status,
            "live_status": live, "live_reason": reason,
            "agents": a["n"] if a else 0, "agent_slugs": a["slugs"].split(",") if a else [],
            "sessions_7d": sessions.get(prefix, 0),
        })
    out.append(_brin_row(agents, sessions))
    return {"runtimes": out, "gateway": "unreachable" if health is None else health.get("status")}


def _brin_row(agents: dict, sessions: dict) -> dict:
    """brin runs the loop for every agent whose model prefix is not a CLI's.
    Its state is the dispatcher companion and its unit, from the collector."""
    cli_prefixes = {p for (_, _, _, _, p, _) in _RUNTIMES}
    api = {k: r for k, r in agents.items() if k not in cli_prefixes}
    try:
        host = _read_host_data("services.json")
    except HTTPException:
        host = None
    comp = next((c for c in (host or {}).get("companions", []) if c["name"] == "dispatcher"), None)
    unit = next((u for u in (host or {}).get("systemd", []) if u["name"] == "smart-node-dispatcher.service"), None)
    if host is None:
        live, reason = "unknown", "host collector not readable"
    elif not comp:
        live, reason = "not installed", "dispatcher companion is not installed"
    elif not unit or unit.get("health") != "ok":
        live, reason = "broken", "smart-node-dispatcher.service: " + ((unit or {}).get("status") or "missing")
    else:
        live, reason = "available", "smart-node-dispatcher.service " + (unit.get("status") or "")
    return {
        "slug": "brin", "label": "Brin (node's loop)", "kind": "loop",
        "binary": "/opt/smart-node/dispatcher" if comp else None,
        "version": (comp or {}).get("version"),
        "gateway_provider": None, "gateway_status": None,
        "live_status": live, "live_reason": reason,
        "agents": sum(r["n"] for r in api.values()),
        "agent_slugs": sorted(x for r in api.values() for x in r["slugs"].split(",")),
        "sessions_7d": sum(n for p, n in sessions.items() if p not in cli_prefixes),
    }


@router.get("/{slug}", response_model=Runtime, dependencies=[Depends(requires("runtimes.read"))])
def get_runtime(slug: str):
    with cursor() as cur:
        cur.execute(f"SELECT {SELECT_COLS} FROM sn_config.runtimes WHERE slug = %s", (slug,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"runtime '{slug}' not found")
    return _row(row)


def _actor(request: Request, principal: Principal | None) -> str:
    """Who to record a change under: the account the permission check
    resolved. That is also right for the installer, which reaches this over the
    socket as the node's root account and would otherwise be an unnamed peer."""
    if principal is not None:
        return principal.username
    return get_actor(request, request.headers.get("remote-user"))


@router.post("", response_model=Runtime)
def upsert_runtime(payload: RuntimeCreate, request: Request,
                   principal: Principal | None = Depends(requires("runtimes.write"))):
    actor = _actor(request, principal)
    log.info("upsert runtime slug=%s actor=%s", payload.slug, actor)

    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM sn_config.runtimes WHERE slug = %s", (payload.slug,))
        existed = cur.fetchone() is not None
        cur.execute(
            f"""
            INSERT INTO sn_config.runtimes
              (slug, label, purpose, description, kind, status, install_uri, install_path, companion_slug, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (slug) DO UPDATE SET
              label          = EXCLUDED.label,
              purpose        = CASE WHEN EXCLUDED.purpose = ''
                                    THEN sn_config.runtimes.purpose
                                    ELSE EXCLUDED.purpose END,
              description    = EXCLUDED.description,
              kind           = EXCLUDED.kind,
              status         = EXCLUDED.status,
              install_uri    = EXCLUDED.install_uri,
              install_path   = CASE WHEN EXCLUDED.install_path = ''
                                    THEN sn_config.runtimes.install_path
                                    ELSE EXCLUDED.install_path END,
              companion_slug = EXCLUDED.companion_slug,
              metadata       = EXCLUDED.metadata,
              updated_at     = now(),
              archived_at    = NULL
            RETURNING id
            """,
            (payload.slug, payload.label, payload.purpose, payload.description,
             payload.kind, payload.status, payload.install_uri, payload.install_path,
             payload.companion_slug, __import__("json").dumps(payload.metadata)),
        )
        emit(
            cur, actor=actor,
            event_type="runtime.updated" if existed else "runtime.registered",
            entity_type="runtime", entity_id=payload.slug,
            payload={"kind": payload.kind, "status": payload.status},
        )
        cur.execute(f"SELECT {SELECT_COLS} FROM sn_config.runtimes WHERE slug = %s", (payload.slug,))
        row = cur.fetchone()

    code = status.HTTP_200_OK if existed else status.HTTP_201_CREATED
    return JSONResponse(content=_row(row).model_dump(mode="json"), status_code=code)


@router.put("/{slug}", response_model=Runtime)
def update_runtime(slug: str, payload: RuntimeUpdate, request: Request,
                   principal: Principal | None = Depends(requires("runtimes.write"))):
    actor = _actor(request, principal)
    with cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE sn_config.runtimes
               SET label = %s, purpose = %s, description = %s, kind = %s, status = %s,
                   install_uri = %s, install_path = %s, companion_slug = %s,
                   metadata = %s::jsonb, updated_at = now()
             WHERE slug = %s RETURNING id
            """,
            (payload.label, payload.purpose, payload.description, payload.kind, payload.status,
             payload.install_uri, payload.install_path, payload.companion_slug,
             __import__("json").dumps(payload.metadata), slug),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"runtime '{slug}' not found")
        emit(
            cur, actor=actor, event_type="runtime.updated",
            entity_type="runtime", entity_id=slug, payload={},
        )
        cur.execute(f"SELECT {SELECT_COLS} FROM sn_config.runtimes WHERE slug = %s", (slug,))
        row = cur.fetchone()
    return _row(row)


@router.delete("/{slug}", response_model=dict)
def delete_runtime(slug: str, request: Request, hard: bool = False,
                   principal: Principal | None = Depends(requires("runtimes.write"))):
    actor = _actor(request, principal)
    with cursor(commit=True) as cur:
        if hard:
            cur.execute("DELETE FROM sn_config.runtimes WHERE slug = %s RETURNING slug", (slug,))
        else:
            cur.execute(
                "UPDATE sn_config.runtimes SET archived_at = now(), status = 'not-installed' "
                "WHERE slug = %s AND archived_at IS NULL RETURNING slug",
                (slug,),
            )
        row = cur.fetchone()
        if row is not None:
            emit(
                cur, actor=actor, event_type="runtime.deleted",
                entity_type="runtime", entity_id=slug, payload={"hard": hard},
            )
    if row is None:
        raise HTTPException(status_code=404, detail=f"runtime '{slug}' not found")
    return {"ok": True, "deleted": slug, "hard": hard}
