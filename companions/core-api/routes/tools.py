"""CRUD endpoints for the Tools registry (sn_core.tools).

After B2-Integrations phase 1, each tool carries an `integration_slug`
column that points at sn_config.integrations(slug). The `source:` tag is
no longer authoritative -- it is derived from integration_slug on read
and stripped from incoming payloads on write.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from psycopg2.extras import Json

from auth import get_actor
from db import cursor
from permissions import requires
from events_emit import emit
from schemas.tool import Tool, ToolCreate, ToolUpdate

log = logging.getLogger("core-api.tools")

router = APIRouter()


SELECT_COLS = (
    "id, name, kind, title, description, install_uri, status, tags, "
    "integration_slug, usage_md, doing, "
    # Echo what makes a row callable: a response that returns null for a field
    # just set reads as "ignored" and sent people to SQL.
    "input_schema, http_method, http_path, "
    "created_at, updated_at, created_by, updated_by"
)


def _strip_source_tags(tags: list[str]) -> list[str]:
    """Remove any incoming source:component:* tag -- column is authoritative."""
    return [t for t in tags if not t.startswith("source:component:")]


def _enrich_with_source_tag(row: dict) -> dict:
    """Append source:component:<integration_slug> to tags on read."""
    slug = row.get("integration_slug")
    if not slug:
        return row
    tag = f"source:component:{slug}"
    tags = list(row.get("tags") or [])
    if tag not in tags:
        tags = [tag, *tags]
    row = {**row, "tags": tags}
    return row


def _row_to_tool(row) -> Tool:
    return Tool(**_enrich_with_source_tag(row))


@router.get("", response_model=dict, dependencies=[Depends(requires("tools.read"))])
def list_tools(kind: str | None = None, integration_slug: str | None = None):
    where = []
    params: list = []
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if integration_slug:
        where.append("integration_slug = %s")
        params.append(integration_slug)
    sql = f"SELECT {SELECT_COLS} FROM sn_core.tools"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY integration_slug, kind, name"
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return {"tools": [_list_item(r) for r in rows]}


def _list_item(row) -> dict:
    """One list row. A row that fails the model (on example-node: 22 names with
    capitals written by MCP autodiscovery on 2026-09-02, e.g. operator-getTabs)
    used to fail the whole list with a 500, so the Tools page showed nothing.
    It is returned unvalidated and flagged instead; the name rule still holds
    for writes."""
    try:
        return _row_to_tool(row).model_dump(mode="json")
    except ValidationError as ex:
        log.warning("tool %s (id %s) fails the Tool model: %s",
                    row.get("name"), row.get("id"), ex.errors()[0].get("msg"))
        item = Tool.model_construct(**_enrich_with_source_tag(row)).model_dump(mode="json")
        item["name_invalid"] = True
        return item


@router.get("/labels", response_model=dict, dependencies=[Depends(requires("tools.read"))])
def tool_labels():
    """What a chat may say about each tool, for its live line: {name: {kind,
    title, doing}}. A runtime calls an MCP server's tool mcp_<server>_<tool>; a
    tool with no row of its own is read through its server's row (kind mcp)."""
    with cursor() as cur:
        cur.execute("SELECT name, kind, title, doing FROM sn_core.tools")
        rows = cur.fetchall()
    return {"labels": {r["name"]: {"kind": r["kind"], "title": r["title"], "doing": r["doing"]} for r in rows}}


@router.get("/{name}", response_model=Tool, dependencies=[Depends(requires("tools.read"))])
def get_tool(name: str):
    with cursor() as cur:
        cur.execute(f"SELECT {SELECT_COLS} FROM sn_core.tools WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"tool '{name}' not found")
    return _row_to_tool(row)


@router.post("", response_model=Tool, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(requires("tools.write"))])
def create_tool(payload: ToolCreate, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    tags = _strip_source_tags(payload.tags)
    with cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM sn_core.tools WHERE name = %s", (payload.name,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail=f"tool '{payload.name}' already exists")
        if payload.integration_slug:
            cur.execute(
                "SELECT 1 FROM sn_config.integrations WHERE slug = %s",
                (payload.integration_slug,),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown integration_slug '{payload.integration_slug}'",
                )
        cur.execute(
            f"""
            INSERT INTO sn_core.tools
              (name, kind, title, description, install_uri, status, tags,
               integration_slug, usage_md, doing, input_schema, http_method, http_path,
               created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {SELECT_COLS}
            """,
            (
                payload.name,
                payload.kind,
                payload.title,
                payload.description,
                payload.install_uri,
                payload.status,
                tags,
                payload.integration_slug,
                payload.usage_md,
                payload.doing or None,
                # the column is NOT NULL, default '{}': a tool registered without a
                # schema was refused with a 500 (found 2026-10-30)
                Json(payload.input_schema if payload.input_schema is not None else {}),
                # NULL, never '': a CHECK constraint rejects the empty string and the
                # 500 that follows looks like a server fault rather than a bad field.
                (payload.http_method or None),
                (payload.http_path or None),
                actor,
                actor,
            ),
        )
        row = cur.fetchone()
        emit(cur, actor=actor, event_type="tool.registered",
             entity_type="tool", entity_id=payload.name,
             payload={"kind": payload.kind, "integration_slug": payload.integration_slug})
    return _row_to_tool(row)


@router.put("/{name}", response_model=Tool, dependencies=[Depends(requires("tools.write"))])
def update_tool(name: str, payload: ToolUpdate, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    tags = _strip_source_tags(payload.tags)
    with cursor(commit=True) as cur:
        if payload.integration_slug:
            cur.execute(
                "SELECT 1 FROM sn_config.integrations WHERE slug = %s",
                (payload.integration_slug,),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown integration_slug '{payload.integration_slug}'",
                )
        cur.execute(
            f"""
            UPDATE sn_core.tools
               SET kind = %s,
                   title = %s,
                   description = %s,
                   install_uri = %s,
                   status = %s,
                   tags = %s,
                   integration_slug = %s,
                   usage_md = COALESCE(%s, usage_md),
                   doing = COALESCE(%s, doing),
                   input_schema = COALESCE(%s, input_schema),
                   http_method = COALESCE(%s, http_method),
                   http_path = COALESCE(%s, http_path),
                   updated_at = now(),
                   updated_by = %s
             WHERE name = %s
         RETURNING {SELECT_COLS}
            """,
            (
                payload.kind,
                payload.title,
                payload.description,
                payload.install_uri,
                payload.status,
                tags,
                payload.integration_slug,
                payload.usage_md,
                payload.doing or None,
                Json(payload.input_schema) if payload.input_schema is not None else None,
                (payload.http_method or None),
                (payload.http_path or None),
                actor,
                name,
            ),
        )
        row = cur.fetchone()
        if row is not None:
            emit(cur, actor=actor, event_type="tool.updated",
                 entity_type="tool", entity_id=name, payload={})
    if row is None:
        raise HTTPException(status_code=404, detail=f"tool '{name}' not found")
    return _row_to_tool(row)


@router.delete("/{name}", response_model=dict, dependencies=[Depends(requires("tools.write"))])
def delete_tool(name: str, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.tools WHERE name = %s RETURNING name", (name,))
        row = cur.fetchone()
        if row is not None:
            emit(cur, actor=actor, event_type="tool.deleted",
                 entity_type="tool", entity_id=name, payload={})
    if row is None:
        raise HTTPException(status_code=404, detail=f"tool '{name}' not found")
    return {"ok": True, "deleted": name}
