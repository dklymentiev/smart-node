"""MCP servers registry CRUD (sn_config.mcp_servers) + manual tools catalog.

POST is upsert-by-slug (idempotent for companion installers / discovery).
Tools discovered or manually published via POST /servers/{slug}/tools land
in sn_core.tools with kind='mcp' and mcp_server_id pointing back here.

For v0 Foundation, discovery is best-effort and ships in a follow-up commit;
this module exposes the manual catalog surface.
"""

import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from auth import get_actor
from db import cursor
from events_emit import emit
from permissions import requires
from schemas.mcp import MCPServer, MCPServerCreate, MCPServerUpdate, MCPToolsBulk

log = logging.getLogger("core-api.mcp")

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


SELECT_COLS = (
    "id, application_id, slug, name, transport, "
    "config_jsonb AS config, autoinstall, enabled, "
    "last_discovered_at, auth_secret_ref, status, created_at"
)


def _upsert_umbrella_tool(cur, server_id: int, slug: str, name: str, actor: str) -> None:
    """One grantable catalogue row per MCP server, beside its flat tool rows."""
    cur.execute(
        "INSERT INTO sn_core.tools "
        "(name, kind, title, description, mcp_server_id, status, created_by, updated_by) "
        "VALUES (%s, 'mcp', %s, 'Grant to use this MCP server', %s, 'available', %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET "
        "mcp_server_id=EXCLUDED.mcp_server_id, updated_at=now(), "
        "updated_by=EXCLUDED.updated_by "
        "WHERE sn_core.tools.kind='mcp' RETURNING id",
        (slug, name, server_id, actor, actor),
    )
    if cur.fetchone() is None:
        raise HTTPException(409, f"tool name '{slug}' is already used by another kind")


def _row_to_server(row) -> MCPServer:
    # config_jsonb -> config alias already applied by SELECT_COLS
    return MCPServer(**row)


@router.get("/servers", response_model=dict,
            dependencies=[Depends(requires("mcp.read"))])
def list_servers(enabled_only: bool = False):
    sql = f"SELECT {SELECT_COLS} FROM sn_config.mcp_servers"
    if enabled_only:
        sql += " WHERE enabled = true"
    sql += " ORDER BY slug"
    with cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = list(pool.map(_live_check, rows))
    for row, (live, reason) in zip(rows, checks):
        row["live_status"], row["live_reason"] = live, reason
    return {"servers": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Live check. The stored `status` only changes when someone posts a
# discovery to /servers/{slug}/tools, and nothing on the node does, so every
# server read "unknown" forever. This answers the cheaper question honestly:
# is the server there at all. http: the endpoint answers anything within 2 s.
# stdio: the command (and its script, for an interpreter) exists and is
# executable; the agent's CLI starts it, nothing runs between calls. Not an MCP
# handshake: no initialize, no tools/list.
# ---------------------------------------------------------------------------

_LIVE_TTL = 30.0
_live_cache: dict[str, tuple[float, tuple[str, str]]] = {}
_INTERPRETERS = ("python", "python3", "node", "bash", "sh", "perl")


def _live_check(row) -> tuple[str, str]:
    key = f"{row['slug']}:{json.dumps(row.get('config') or {}, sort_keys=True)}"
    hit = _live_cache.get(key)
    if hit and time.monotonic() - hit[0] < _LIVE_TTL:
        return hit[1]
    cfg = row.get("config") or {}
    try:
        if row.get("transport") == "stdio":
            result = _check_stdio(cfg)
        else:
            result = _check_http(cfg)
    except Exception as ex:  # a check must never break the list
        result = ("unknown", f"check failed: {ex}")
    _live_cache[key] = (time.monotonic(), result)
    return result


def _check_http(cfg: dict) -> tuple[str, str]:
    url = cfg.get("url")
    if not url:
        return "broken", "no url in config"
    return _probe(url, mcp_path=True)


def _probe(url: str, mcp_path: bool) -> tuple[str, str]:
    """Any HTTP answer means the server is up. On an MCP endpoint a 404 means
    the path is wrong (nothing an agent can use); on a base URL it is fine."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return "available", f"{url} answers {r.status}"
    except urllib.error.HTTPError as ex:
        if mcp_path and ex.code == 404:
            return "broken", f"{url} answers 404: no MCP endpoint at this path"
        # Any other 4xx/5xx is still a live server: MCP endpoints refuse a bare GET.
        return "available", f"{url} answers {ex.code}"
    except urllib.error.URLError as ex:
        if isinstance(ex.reason, ssl.SSLError):
            return "available", f"{url} answers TLS ({ex.reason.__class__.__name__})"
        return "broken", f"{url}: {ex.reason}"
    except (TimeoutError, socket.timeout):
        return "broken", f"{url}: no answer in 2 s"


def _check_stdio(cfg: dict) -> tuple[str, str]:
    cmd = cfg.get("command")
    if not cmd:
        return "broken", "no command in config"
    args = [a for a in (cfg.get("args") or []) if isinstance(a, str)]
    paths = [cmd]
    if os.path.basename(cmd).split(".")[0].rstrip("0123456789.") in _INTERPRETERS and args:
        first = next((a for a in args if not a.startswith("-")), None)
        if args[0] != "-m" and first:
            cwd = cfg.get("cwd") or ""
            paths.append(first if os.path.isabs(first) or not cwd else os.path.join(cwd, first))
    for p in paths:
        if not os.path.isabs(p):
            continue  # a bare name is resolved on the agent's PATH, not checkable here
        try:
            st = os.stat(p)
        except PermissionError:
            return "unknown", f"core-api cannot see {p}"
        except FileNotFoundError:
            return "broken", f"{p} does not exist"
        if p == cmd and not st.st_mode & 0o111:
            return "broken", f"{p} is not executable"
    # A stdio server that is only a bridge to something elsewhere (the operator
    # bridge to the owner's Chrome: env OPERATOR_HTTP_URL) is only as alive as
    # that something, so an http(s) URL in its env is probed too.
    for k, v in (cfg.get("env") or {}).items():
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            live, why = _probe(v, mcp_path=False)
            if live != "available":
                return live, f"command present, but {k}: {why}"
            return "available", f"command present; {k} {why}"
    return "available", "command present: " + " ".join(paths)


@router.get("/servers/{slug}", response_model=MCPServer,
            dependencies=[Depends(requires("mcp.read"))])
def get_server(slug: str):
    with cursor() as cur:
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.mcp_servers WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"mcp server '{slug}' not found")
    return _row_to_server(row)


@router.post("/servers", response_model=MCPServer,
             dependencies=[Depends(requires("mcp.write"))])
def upsert_server(payload: MCPServerCreate, request: Request):
    """Upsert MCP server by slug. 201 on create, 200 on update."""
    actor = get_actor(request, request.headers.get("remote-user"))
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM sn_config.mcp_servers WHERE slug = %s",
            (payload.slug,),
        )
        existed = cur.fetchone() is not None
        cur.execute(
            """
            INSERT INTO sn_config.mcp_servers
              (slug, name, transport, config_jsonb, auth_secret_ref, enabled, autoinstall, default_on, status)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, 'unknown')
            ON CONFLICT (slug) DO UPDATE SET
              name             = EXCLUDED.name,
              transport        = EXCLUDED.transport,
              config_jsonb     = EXCLUDED.config_jsonb,
              auth_secret_ref  = EXCLUDED.auth_secret_ref,
              enabled          = EXCLUDED.enabled,
              autoinstall      = EXCLUDED.autoinstall,
              default_on       = EXCLUDED.default_on
            """,
            (
                payload.slug, payload.name, payload.transport,
                json.dumps(payload.config), payload.auth_secret_ref,
                payload.enabled, payload.autoinstall, payload.default_on,
            ),
        )
        cur.execute("SELECT id FROM sn_config.mcp_servers WHERE slug = %s", (payload.slug,))
        _upsert_umbrella_tool(cur, cur.fetchone()["id"], payload.slug, payload.name, actor)
        emit(
            cur, actor=actor,
            event_type="mcp.updated" if existed else "mcp.registered",
            entity_type="mcp_server", entity_id=payload.slug,
            payload={"transport": payload.transport},
        )
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.mcp_servers WHERE slug = %s",
            (payload.slug,),
        )
        row = cur.fetchone()

    server = _row_to_server(row)
    code = status.HTTP_200_OK if existed else status.HTTP_201_CREATED
    return JSONResponse(content=server.model_dump(mode="json"), status_code=code)


@router.put("/servers/{slug}", response_model=MCPServer,
            dependencies=[Depends(requires("mcp.write"))])
def update_server(slug: str, payload: MCPServerUpdate, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    sets: list[str] = []
    params: list = []
    if payload.name is not None:
        sets.append("name=%s"); params.append(payload.name)
    if payload.transport is not None:
        sets.append("transport=%s"); params.append(payload.transport)
    if payload.config is not None:
        sets.append("config_jsonb=%s::jsonb"); params.append(json.dumps(payload.config))
    if payload.auth_secret_ref is not None:
        sets.append("auth_secret_ref=%s"); params.append(payload.auth_secret_ref)
    if payload.enabled is not None:
        sets.append("enabled=%s"); params.append(payload.enabled)
    if not sets:
        raise HTTPException(400, "no fields to update")
    params.append(slug)
    with cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE sn_config.mcp_servers SET {', '.join(sets)} WHERE slug=%s RETURNING id",
            params,
        )
        if cur.fetchone() is None:
            raise HTTPException(404, f"mcp server '{slug}' not found")
        emit(
            cur, actor=actor, event_type="mcp.updated",
            entity_type="mcp_server", entity_id=slug, payload={},
        )
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.mcp_servers WHERE slug=%s",
            (slug,),
        )
        row = cur.fetchone()
    return _row_to_server(row)


@router.delete("/servers/{slug}", response_model=dict,
               dependencies=[Depends(requires("mcp.write"))])
def delete_server(slug: str, request: Request):
    """Hard delete (no soft-delete column on mcp_servers). FK on sn_core.tools.mcp_server_id is ON DELETE SET NULL -- tools survive."""
    actor = get_actor(request, request.headers.get("remote-user"))
    with cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM sn_config.mcp_servers WHERE slug=%s RETURNING slug",
            (slug,),
        )
        row = cur.fetchone()
        if row is not None:
            emit(
                cur, actor=actor, event_type="mcp.deleted",
                entity_type="mcp_server", entity_id=slug, payload={},
            )
    if row is None:
        raise HTTPException(404, f"mcp server '{slug}' not found")
    return {"ok": True, "deleted": slug}


@router.get("/servers/{slug}/tools", response_model=dict,
            dependencies=[Depends(requires("mcp.read"))])
def list_server_tools(slug: str):
    """List tools registered as belonging to this MCP server."""
    with cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.name, t.title, t.description, t.status,
                   t.input_schema, t.last_seen
            FROM sn_core.tools t
            JOIN sn_config.mcp_servers s ON s.id = t.mcp_server_id
            WHERE s.slug = %s
            ORDER BY t.name
            """,
            (slug,),
        )
        rows = cur.fetchall()
    return {"tools": rows, "count": len(rows)}


@router.post("/servers/{slug}/tools", response_model=dict,
             dependencies=[Depends(requires("mcp.write"))])
def bulk_upsert_tools(slug: str, payload: MCPToolsBulk, request: Request):
    """Bulk upsert tools for an MCP server (manual catalog or discovery output).

    Tool name convention: '{slug}-{tool_name}' to avoid collisions across servers.
    Marks the server status=available + bumps last_discovered_at on success.
    """
    actor = get_actor(request, request.headers.get("remote-user"))
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM sn_config.mcp_servers WHERE slug=%s",
            (slug,),
        )
        srv = cur.fetchone()
        if not srv:
            raise HTTPException(404, f"mcp server '{slug}' not found")
        server_id = srv["id"]
        _upsert_umbrella_tool(cur, server_id, slug, slug, actor)

        upserted: list[str] = []
        for t in payload.tools:
            tool_name = f"{slug}-{t.name}"
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, install_uri, status, tags,
                   input_schema, mcp_server_id, last_seen, created_by, updated_by)
                VALUES (%s, 'mcp', %s, %s, '', 'available', '{}',
                        %s::jsonb, %s, now(), %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                  title         = EXCLUDED.title,
                  description   = EXCLUDED.description,
                  input_schema  = EXCLUDED.input_schema,
                  mcp_server_id = EXCLUDED.mcp_server_id,
                  updated_at    = now(),
                  updated_by    = EXCLUDED.updated_by,
                  last_seen     = now()
                """,
                (
                    tool_name, t.title or t.name, t.description or "",
                    json.dumps(t.input_schema), server_id, actor, actor,
                ),
            )
            upserted.append(tool_name)

        cur.execute(
            "UPDATE sn_config.mcp_servers "
            "SET last_discovered_at = now(), status = 'available' "
            "WHERE id = %s",
            (server_id,),
        )
        emit(
            cur, actor=actor, event_type="mcp.tools_published",
            entity_type="mcp_server", entity_id=slug,
            payload={"count": len(upserted), "names": upserted},
        )
    return {"ok": True, "upserted": upserted, "count": len(upserted)}
