"""Agent Knowledge -- linked reference sources injected into the agent prompt.

  GET    /api/knowledge/kinds                source kinds + availability (gated by integrations)
  GET    /api/agents/{slug}/knowledge        list an agent's knowledge sources
  POST   /api/agents/{slug}/knowledge        add one {kind, title, ref?, content?}
  DELETE /api/agents/{slug}/knowledge/{kid}  soft-remove

A kind is offerable only if its fetcher is implemented AND (no integration required
OR that integration is status='available'). Phase 1: only 'text' is implemented.
Adding/removing recomposes the agent's CLAUDE.md so the change lands in the prompt.

Connecting a document also writes a DURABLE snapshot into the
agent's own mesh workspace (tags via:ui + source:<kind>), and removing it deletes
that mesh doc -- so the Knowledge surface is a slice of mesh and delete is symmetric.
The mesh doc id is kept in agent_knowledge.metadata.mesh_guid (a thin pointer).
"""

import base64
import logging
import os
import subprocess
import tempfile

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import Principal, get_principal
from db import cursor
from permissions import requires
from routes.agents import (MESH_API_URL, _compose_and_write_claude, _mesh_add,
                           _mesh_delete)

log = logging.getLogger("core-api.knowledge")

kinds_router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
agent_knowledge_router = APIRouter(prefix="/api/agents", tags=["knowledge"])

# Each kind needs a fetcher (implemented) and may require an integration capability.
KIND_CATALOG = [
    {"kind": "text",        "label": "Pasted text / fact", "requires": None,           "implemented": True},
    {"kind": "doci",        "label": "Document",            "requires": "doci",         "implemented": False},
    {"kind": "gdrive_file", "label": "Google Drive file",   "requires": "google-drive", "implemented": True},
    {"kind": "file",        "label": "Uploaded file",       "requires": None,           "implemented": True},
]

# Host converter: PDF/DOCX/XLSX/PPTX/HTML -> markdown.
MARKITDOWN = "/srv/tools/host-utils/venv/bin/markitdown"
_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB decoded


def _kinds_with_availability(cur):
    cur.execute(
        "SELECT slug, status FROM sn_config.integrations "
        "WHERE archived_at IS NULL AND 'knowledge' = ANY(capabilities)"
    )
    status = {r["slug"]: r["status"] for r in cur.fetchall()}
    out = []
    for k in KIND_CATALOG:
        req = k["requires"]
        integ_ok = (req is None) or (status.get(req) == "available")
        available = bool(k["implemented"]) and integ_ok
        if not k["implemented"]:
            reason = "coming soon"
        elif not integ_ok:
            reason = f"needs the {req} integration"
        else:
            reason = ""
        out.append({"kind": k["kind"], "label": k["label"], "requires": req,
                    "available": available, "reason": reason})
    return out


@kinds_router.get("/kinds",
                  dependencies=[Depends(requires("knowledge.read"))])
def list_kinds(principal: Principal = Depends(get_principal)):
    with cursor() as cur:
        return {"kinds": _kinds_with_availability(cur)}


def _get_agent(cur, slug: str):
    cur.execute(
        "SELECT id, tenant_id, mesh_workspace FROM sn_config.agents "
        "WHERE slug = %s AND archived_at IS NULL",
        (slug,),
    )
    a = cur.fetchone()
    if a is None:
        raise HTTPException(404, f"agent not found: {slug}")
    return a


def _extract_file_markdown(filename, data):
    """Convert an uploaded file's bytes to markdown via the host markitdown.
    Raises HTTPException on any failure. Returns markdown text."""
    if not os.path.exists(MARKITDOWN):
        raise HTTPException(503, "file conversion tool (markitdown) not installed on this node")
    suffix = os.path.splitext(filename or "")[1] or ".bin"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        proc = subprocess.run([MARKITDOWN, tmp.name], capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            msg = (proc.stderr or "").strip().splitlines()
            raise HTTPException(502, "could not convert file: " + (msg[-1][:200] if msg else "markitdown failed"))
        return proc.stdout
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# The content to snapshot into mesh, per kind. text = inline; gdrive_file =
# exported live from Drive at connect; file = uploaded bytes (base64) -> markitdown.
# doci = later (needs the node Doci api key). Raises on a fetch failure so the
# add aborts cleanly (no half-state row without its memory copy).
def _snapshot_content(payload):
    if payload.kind == "text":
        return payload.content
    if payload.kind == "gdrive_file":
        from routes.agents import _gdoc_export
        return _gdoc_export(payload.ref or "")
    if payload.kind == "file":
        if not payload.file_b64:
            raise HTTPException(400, "file knowledge requires base64 bytes in 'file_b64'")
        try:
            data = base64.b64decode(payload.file_b64)
        except Exception:
            raise HTTPException(400, "file_b64 is not valid base64")
        if len(data) > _MAX_FILE_BYTES:
            raise HTTPException(413, f"file too large (max {_MAX_FILE_BYTES // (1024 * 1024)} MB)")
        return _extract_file_markdown(payload.filename or "upload.bin", data)
    return None


class KnowledgeCreate(BaseModel):
    kind: str
    title: str = ""
    ref: str = ""
    content: str | None = None
    filename: str = ""          # kind=file: original name (drives type detection + title)
    file_b64: str | None = None  # kind=file: the file bytes, base64-encoded


@agent_knowledge_router.get("/{slug}/knowledge",
                            dependencies=[Depends(requires("knowledge.read"))])
def list_knowledge(slug: str, principal: Principal = Depends(get_principal)):
    with cursor() as cur:
        a = _get_agent(cur, slug)
        cur.execute(
            "SELECT id, kind, ref, title, content, enabled, status, sort_order, "
            "       metadata->>'mesh_guid' AS mesh_guid "
            "FROM sn_config.agent_knowledge "
            "WHERE agent_id = %s AND archived_at IS NULL ORDER BY sort_order, id",
            (a["id"],),
        )
        rows = cur.fetchall()
    return {"agent": slug, "knowledge": rows}


@agent_knowledge_router.post("/{slug}/knowledge", status_code=201,
                             dependencies=[Depends(requires("knowledge.write"))])
def add_knowledge(slug: str, payload: KnowledgeCreate,
                  principal: Principal = Depends(get_principal)):
    if payload.kind not in {k["kind"] for k in KIND_CATALOG}:
        raise HTTPException(400, f"unknown kind: {payload.kind}")
    with cursor(commit=True) as cur:
        avail = {k["kind"]: k for k in _kinds_with_availability(cur)}
        if not avail[payload.kind]["available"]:
            raise HTTPException(400, f"kind '{payload.kind}' not available: {avail[payload.kind]['reason']}")
        if payload.kind == "text" and not (payload.content or "").strip():
            raise HTTPException(400, "text knowledge requires content")
        if payload.kind == "gdrive_file" and not (payload.ref or "").strip():
            raise HTTPException(400, "gdrive_file knowledge requires the document URL in 'ref'")
        a = _get_agent(cur, slug)

        # Extract the content to snapshot BEFORE inserting, so a fetch failure
        # aborts the whole add (no half-state row without its memory copy).
        try:
            content = _snapshot_content(payload)
        except HTTPException:
            raise
        except Exception as e:
            log.warning("knowledge fetch failed for %s (%s): %s", slug, payload.kind, e)
            raise HTTPException(502, f"could not fetch document content: {e}")
        if payload.kind != "text" and not (content or "").strip():
            raise HTTPException(502, "could not fetch document content (source returned nothing)")

        eff_ref = payload.ref or (payload.filename if payload.kind == "file" else "")
        title = payload.title.strip() or (eff_ref or payload.kind)
        # Link/file kinds are pointers in agent_knowledge (content lives in mesh);
        # text keeps its inline content for the legacy '# Knowledge' prompt block.
        row_content = payload.content if payload.kind == "text" else None
        cur.execute(
            "INSERT INTO sn_config.agent_knowledge "
            "(tenant_id, agent_id, kind, ref, content, title, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (a["tenant_id"] or 1, a["id"], payload.kind, eff_ref, row_content,
             title, "panel", "panel"),
        )
        new_id = cur.fetchone()["id"]

        # DSR write a durable snapshot into the agent's own mesh workspace.
        # Fail-soft on the mesh write itself (the content is already validated) --
        # but NOT silent. The snapshot is what `_agent_documents` later composes
        # into the prompt, so an entry without one is stored and undelivered: the
        # operator has added a fact the agent cannot read. previously this
        # answered a bare 201 and the panel drew the chip, which is how a whole
        # demo's specialists came to look like they had "lost their knowledge"
        # when in truth every write had been landing in a database and nowhere
        # else. The row still goes in -- it is real, and a later re-run can
        # snapshot it -- and the response says which of the two happened.
        ws = a.get("mesh_workspace")
        delivered = False
        undelivered_reason = None
        if not ws:
            undelivered_reason = (
                "the agent has no mesh workspace, so nothing was snapshotted "
                "into its documents"
            )
        elif not (content or "").strip():
            undelivered_reason = "the source returned no content to snapshot"
        else:
            tags = ["via:ui", f"source:{payload.kind}", "type:knowledge"]
            # An uploaded file is pinned: its text goes into the prompt, as the
            # page promises ("always keeps in mind"). Unpinned, the agent got only
            # its title and "search your memory", or "ask the owner" without the
            # memory grant (2026-10-30). The pin budget in compose still caps it.
            if payload.kind == "file":
                tags.append("pin")
            try:
                guid = _mesh_add(ws, content, tags=tags, filename=title)
                if guid:
                    cur.execute(
                        "UPDATE sn_config.agent_knowledge "
                        "SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
                        "    '{mesh_guid}', to_jsonb(%s::text)) "
                        "WHERE id = %s",
                        (guid, new_id),
                    )
                    delivered = True
                else:
                    undelivered_reason = "the memory service accepted the write but returned no document id"
            except Exception as e:
                undelivered_reason = f"the memory service could not be reached: {e}"
            if not delivered:
                log.warning(
                    "knowledge #%s for %s is STORED BUT NOT DELIVERED -- %s "
                    "(MESH_API_URL=%s)",
                    new_id, slug, undelivered_reason, MESH_API_URL,
                )

        _compose_and_write_claude(cur, slug)
    return {"id": new_id, "delivered": delivered,
            "detail": None if delivered else undelivered_reason}


@agent_knowledge_router.delete("/{slug}/knowledge/{kid}", status_code=204,
                               dependencies=[Depends(requires("knowledge.write"))])
def delete_knowledge(slug: str, kid: int, principal: Principal = Depends(get_principal)):
    with cursor(commit=True) as cur:
        a = _get_agent(cur, slug)
        cur.execute(
            "SELECT metadata->>'mesh_guid' AS mesh_guid FROM sn_config.agent_knowledge "
            "WHERE id = %s AND agent_id = %s AND archived_at IS NULL",
            (kid, a["id"]),
        )
        krow = cur.fetchone()
        if krow is None:
            raise HTTPException(404, "knowledge item not found")
        cur.execute(
            "UPDATE sn_config.agent_knowledge SET archived_at = now() "
            "WHERE id = %s AND agent_id = %s AND archived_at IS NULL",
            (kid, a["id"]),
        )

        # DSR delete the durable snapshot from mesh too (symmetry). Fail-soft.
        mesh_guid = krow.get("mesh_guid")
        ws = a.get("mesh_workspace")
        if mesh_guid and ws:
            try:
                _mesh_delete(ws, mesh_guid)
            except Exception as e:
                log.warning("mesh snapshot delete failed for %s knowledge #%s: %s", slug, kid, e)

        _compose_and_write_claude(cur, slug)
    return None
