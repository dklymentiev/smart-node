"""CRUD endpoints for the Integrations registry (sn_config.integrations).

This is the authoritative source of "what sources/apps an agent can see".
Companion installers POST here at install-time; the admin /integrations/
page CRUDs here directly.

POST is upsert-by-slug:
    * unknown slug      -> 201 Created
    * existing slug     -> 200 OK with updated fields
This lets companion installers run idempotently without prior existence
checks.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from auth import get_actor
from db import cursor
from events_emit import emit
from permissions import requires
from schemas.integration import Integration, IntegrationCreate, IntegrationUpdate
from schemas.tool import Tool
from routes.system import _read_host_data

log = logging.getLogger("core-api.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


SELECT_COLS = (
    "i.id, i.slug, i.label, i.purpose, i.description, i.kind, i.status, i.endpoint, "
    "i.install_uri, i.install_path, i.companion_slug, i.metadata, "
    "i.capabilities, "
    "i.created_at, i.updated_at, i.archived_at, "
    "(SELECT count(*) FROM sn_core.tools t WHERE t.integration_slug = i.slug) AS tools_count"
)

TOOLS_SELECT_COLS = (
    "id, name, kind, title, description, install_uri, status, tags, "
    "integration_slug, created_at, updated_at, created_by, updated_by"
)


def _row_to_integration(row) -> Integration:
    return Integration(**row)


@router.get("", response_model=dict,
            dependencies=[Depends(requires("integrations.read"))])
def list_integrations(
    status_filter: str | None = None,
    companion_slug: str | None = None,
    include_archived: bool = False,
):
    """List integrations.

    Query params:
        status         filter by status (available / not-installed / broken)
        companion_slug filter by linked companion
        include_archived  set true to include archived rows (default false)
    """
    where = []
    params: list = []
    if status_filter:
        where.append("i.status = %s")
        params.append(status_filter)
    if companion_slug:
        where.append("i.companion_slug = %s")
        params.append(companion_slug)
    if not include_archived:
        where.append("i.archived_at IS NULL")
    sql = f"SELECT {SELECT_COLS} FROM sn_config.integrations i"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.slug"
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        # Helper tools = the catalog's own tools (shell, cli, builtin, api); MCP
        # methods are counted as the MCP server they come from, not as tools.
        cur.execute(
            "SELECT integration_slug, count(*) AS n FROM sn_core.tools "
            "WHERE kind <> 'mcp' AND integration_slug IS NOT NULL GROUP BY 1"
        )
        helpers = {r["integration_slug"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT m.slug, m.transport, m.enabled, a.slug AS integration "
            "FROM sn_config.mcp_servers m "
            "LEFT JOIN sn_config.integrations a ON a.id = m.application_id"
        )
        servers = cur.fetchall()
    try:
        host = _read_host_data("services.json")
    except HTTPException:
        host = None
    out = []
    for r in rows:
        item = Integration(**r).model_dump(mode="json")
        item["helper_tools_count"] = helpers.get(r["slug"], 0)
        # application_id is empty for every server today, so the slug match is
        # what links most of them.
        srv = next((s for s in servers if s["integration"] == r["slug"]), None) \
            or next((s for s in servers if s["slug"] == r["slug"]), None)
        item["mcp_server"] = (
            {"slug": srv["slug"], "transport": srv["transport"], "enabled": srv["enabled"]}
            if srv else None
        )
        item["live_status"], item["live_reason"] = _live_status(r, host)
        out.append(item)
    return {"integrations": out}


def _live_status(row, host) -> tuple[str, str]:
    """Whether the integration is actually up, from the host collector.

    The stored `status` column is whatever someone last wrote; nothing checks
    it. This checks what can be checked: an integration backed by a companion is
    `available` when that companion is installed and every unit or container
    running from its install tree is healthy, else `broken` with the reason.
    With no companion there is nothing on the host to check: `registered`.
    """
    if host is None:
        return "unknown", "host collector not readable"
    companions = {c["name"]: c for c in host.get("companions", [])}
    comp = row.get("companion_slug") or (row["slug"] if row["slug"] in companions else None)
    if not comp:
        return "registered", "no companion on this node to check"
    c = companions.get(comp)
    if c is None:
        return "broken", f"companion {comp} is not installed"
    if c.get("state") != "installed":
        return "broken", f"companion {comp}: {c.get('state')}"
    base = f"/opt/smart-node/{comp}"
    units = [u for u in host.get("systemd", []) + host.get("containers", [])
             if (u.get("path") or "") == base or (u.get("path") or "").startswith(base + "/")]
    bad = [u for u in units if u.get("health") != "ok"]
    if bad:
        return "broken", "; ".join(f"{u['name']}: {u.get('status') or u.get('state')}" for u in bad)
    return "available", f"{comp} {c.get('version') or ''} installed".replace("  ", " ") + (
        f", {len(units)} running" if units else "")


@router.get("/{slug}", response_model=Integration,
            dependencies=[Depends(requires("integrations.read"))])
def get_integration(slug: str):
    with cursor() as cur:
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.integrations i WHERE i.slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"integration '{slug}' not found")
    return _row_to_integration(row)


@router.get("/{slug}/tools", response_model=dict,
            dependencies=[Depends(requires("integrations.read"))])
def get_integration_tools(slug: str):
    """Return all tools that belong to this integration."""
    with cursor() as cur:
        cur.execute("SELECT 1 FROM sn_config.integrations WHERE slug = %s", (slug,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"integration '{slug}' not found")
        cur.execute(
            f"SELECT {TOOLS_SELECT_COLS} FROM sn_core.tools "
            f"WHERE integration_slug = %s ORDER BY kind, name",
            (slug,),
        )
        rows = cur.fetchall()
    return {"tools": [Tool(**r).model_dump(mode="json") for r in rows]}


@router.post("", response_model=Integration,
             dependencies=[Depends(requires("integrations.write"))])
def upsert_integration(payload: IntegrationCreate, request: Request):
    """Upsert an integration by slug.

    Returns 201 on create, 200 on update. Optional `tools` array on the
    payload upserts each tool with integration_slug set to this integration.
    """
    actor = get_actor(request, request.headers.get("remote-user"))
    log.info("upsert integration slug=%s actor=%s", payload.slug, actor)

    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM sn_config.integrations WHERE slug = %s",
            (payload.slug,),
        )
        existed = cur.fetchone() is not None

        cur.execute(
            f"""
            INSERT INTO sn_config.integrations
              (slug, label, purpose, description, kind, status, install_uri, install_path, endpoint, companion_slug, metadata, capabilities)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (slug) DO UPDATE SET
              label          = EXCLUDED.label,
              -- Preserve existing purpose if the caller (e.g. an old companion
              -- installer pre-purpose) didn't send one. PUT from the UI is
              -- full-replace and bypasses this.
              purpose        = CASE WHEN EXCLUDED.purpose = ''
                                    THEN sn_config.integrations.purpose
                                    ELSE EXCLUDED.purpose END,
              description    = EXCLUDED.description,
              kind           = EXCLUDED.kind,
              status         = EXCLUDED.status,
              install_uri    = EXCLUDED.install_uri,
              -- Same defensive policy for install_path -- pre-install_path
              -- installers won't blank it out.
              install_path   = CASE WHEN EXCLUDED.install_path = ''
                                    THEN sn_config.integrations.install_path
                                    ELSE EXCLUDED.install_path END,
              -- Same rule: an installer that sends no endpoint must not blank
              -- the one already published, or a routine re-run would quietly
              -- make every api tool of this integration uncallable.
              endpoint       = CASE WHEN EXCLUDED.endpoint = ''
                                    THEN sn_config.integrations.endpoint
                                    ELSE EXCLUDED.endpoint END,
              companion_slug = EXCLUDED.companion_slug,
              metadata       = EXCLUDED.metadata,
              -- Preserve existing capabilities if caller sends an empty list
              -- (old installers pre-capabilities won't wipe agent features).
              capabilities   = CASE WHEN EXCLUDED.capabilities = '{{}}'
                                    THEN sn_config.integrations.capabilities
                                    ELSE EXCLUDED.capabilities END,
              updated_at     = now(),
              archived_at    = NULL
            RETURNING id
            """,
            (
                payload.slug,
                payload.label,
                payload.purpose,
                payload.description,
                payload.kind,
                payload.status,
                payload.install_uri,
                payload.install_path,
                payload.endpoint,
                payload.companion_slug,
                __import__("json").dumps(payload.metadata),
                payload.capabilities,
            ),
        )

        # Optional inline tools array
        if payload.tools:
            for t in payload.tools:
                name = t.get("name")
                kind = t.get("kind")
                title = t.get("title") or name
                if not (name and kind):
                    log.warning("skipping inline tool with missing name/kind: %r", t)
                    continue
                # input_schema: the CALLABLE description of the tool -- what a
                # runtime needs to offer it to a model as a function with real
                # arguments. It was never accepted here, which is why every row
                # in the catalogue carried an empty one and `api` tools reached
                # the model as parameterless: brin could offer `planner` but not
                # say a task has a title. MCP rows escaped it only because the
                # server is asked for its own schemas at runtime.
                schema = t.get("input_schema") or {}
                if not isinstance(schema, dict):
                    log.warning("tool %r: input_schema is not an object, ignoring", name)
                    schema = {}
                cur.execute(
                    """
                    INSERT INTO sn_core.tools
                      (name, kind, title, description, install_uri, status, tags,
                       usage_md, input_schema, http_method, http_path,
                       integration_slug, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                      kind             = EXCLUDED.kind,
                      title            = EXCLUDED.title,
                      description      = EXCLUDED.description,
                      install_uri      = EXCLUDED.install_uri,
                      status           = EXCLUDED.status,
                      tags             = EXCLUDED.tags,
                      usage_md         = COALESCE(NULLIF(EXCLUDED.usage_md, ''), sn_core.tools.usage_md),
                      -- same rule as usage_md: a re-register that supplies no
                      -- schema must not erase the one already published, or a
                      -- routine companion re-run would silently disarm a tool.
                      input_schema     = CASE WHEN EXCLUDED.input_schema = '{}'::jsonb
                                              THEN sn_core.tools.input_schema
                                              ELSE EXCLUDED.input_schema END,
                      -- Which call the tool IS. Same preserve-on-empty rule.
                      http_method      = COALESCE(NULLIF(EXCLUDED.http_method, ''), sn_core.tools.http_method),
                      http_path        = COALESCE(NULLIF(EXCLUDED.http_path, ''), sn_core.tools.http_path),
                      integration_slug = EXCLUDED.integration_slug,
                      updated_at       = now(),
                      updated_by       = EXCLUDED.updated_by
                    """,
                    (
                        name,
                        kind,
                        title,
                        t.get("description", ""),
                        t.get("install_uri", ""),
                        t.get("status", "available"),
                        t.get("tags", []),
                        t.get("usage_md", ""),
                        __import__("json").dumps(schema),
                        # NULL, never "": the column's CHECK allows NULL or a
                        # real verb, so an empty string from a tool that has no
                        # HTTP call of its own would fail the whole registration.
                        ((t.get("http_method") or "").strip().upper() or None),
                        ((t.get("http_path") or "").strip() or None),
                        payload.slug,
                        actor,
                        actor,
                    ),
                )

        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.integrations i WHERE i.slug = %s",
            (payload.slug,),
        )
        row = cur.fetchone()
        emit(cur, actor=actor,
             event_type="integration.updated" if existed else "integration.registered",
             entity_type="integration", entity_id=payload.slug,
             payload={"kind": payload.kind})

    integration = _row_to_integration(row)
    code = status.HTTP_200_OK if existed else status.HTTP_201_CREATED
    return JSONResponse(content=integration.model_dump(mode="json"), status_code=code)


@router.put("/{slug}", response_model=Integration,
            dependencies=[Depends(requires("integrations.write"))])
def update_integration(slug: str, payload: IntegrationUpdate, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    with cursor(commit=True) as cur:
        cur.execute(
            f"""
            UPDATE sn_config.integrations
               SET label          = %s,
                   purpose        = %s,
                   description    = %s,
                   kind           = %s,
                   status         = %s,
                   install_uri    = %s,
                   install_path   = %s,
                   companion_slug = %s,
                   metadata       = %s::jsonb,
                   updated_at     = now()
             WHERE slug = %s
         RETURNING id
            """,
            (
                payload.label,
                payload.purpose,
                payload.description,
                payload.kind,
                payload.status,
                payload.install_uri,
                payload.install_path,
                payload.companion_slug,
                __import__("json").dumps(payload.metadata),
                slug,
            ),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"integration '{slug}' not found")
        emit(cur, actor=actor, event_type="integration.updated",
             entity_type="integration", entity_id=slug, payload={})
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.integrations i WHERE i.slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    return _row_to_integration(row)


@router.delete("/{slug}", response_model=dict,
               dependencies=[Depends(requires("integrations.write"))])
def delete_integration(slug: str, request: Request, hard: bool = False):
    """Soft-delete by default (sets archived_at); pass ?hard=true to remove
    the row entirely. Tools' integration_slug becomes NULL on hard delete
    via FK ON DELETE SET NULL; on soft delete the FK stays intact but the
    integration is filtered out of the default list.
    """
    actor = get_actor(request, request.headers.get("remote-user"))
    if slug == "system":
        raise HTTPException(status_code=400, detail="cannot delete the 'system' integration")
    with cursor(commit=True) as cur:
        if hard:
            cur.execute(
                "DELETE FROM sn_config.integrations WHERE slug = %s RETURNING slug",
                (slug,),
            )
        else:
            cur.execute(
                "UPDATE sn_config.integrations SET archived_at = now(), status = 'not-installed' "
                "WHERE slug = %s AND archived_at IS NULL RETURNING slug",
                (slug,),
            )
        row = cur.fetchone()
        if row is not None:
            emit(cur, actor=actor,
                 event_type="integration.deleted" if hard else "integration.archived",
                 entity_type="integration", entity_id=slug,
                 payload={"hard": hard})
    if row is None:
        raise HTTPException(status_code=404, detail=f"integration '{slug}' not found")
    return {"ok": True, "deleted": slug, "hard": hard}
