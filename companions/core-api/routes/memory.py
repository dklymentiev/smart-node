"""The node's memory (mesh), read by its owner: the panel's Memory page.

  GET  /api/memory/workspaces          every workspace: documents, last write, its agent
  GET  /api/memory/types               the document types (type:* tags) with counts
  GET  /api/memory/documents           the newest documents of one workspace or all of them
  POST /api/memory/search              search by meaning over the chosen workspaces (all by default)
  GET  /api/memory/documents/{guid}    one document in full

The owner asked to see what is in the agents' memory, with search and filters
and an agent's own memory one click away (2026-10-15). Each agent writes to a
workspace of its own (sn_config.agents.mesh_workspace, its slug by default);
"shared" and a few others are not an agent's. memory.read is the owner's alone
(super-admin): it reads every agent's memory. Nothing here writes.

mesh reports a document listed across all workspaces as workspace "*", so the
"all" listing asks each workspace and merges by date, and search names the
workspaces it searches: every row then says whose it is.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import cursor
from permissions import requires

router = APIRouter(prefix="/api/memory", tags=["memory"])

MESH_API_URL = os.environ.get("MESH_API_URL", "https://mesh.local").rstrip("/")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_WS = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_GUID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_TYPE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_MAX_WINDOW = 100   # mesh lists at most 100 per call


def _mesh(method: str, path: str, workspace: str, body: dict | None = None, timeout: float = 10):
    """One call to mesh as the given workspace ("*" for the cross-workspace reads)."""
    req = urllib.request.Request(
        MESH_API_URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Workspace": workspace, "Accept": "application/json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(404, "no such document in that workspace")
        raise HTTPException(502, f"memory answered {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPException(502, f"memory is not reachable: {e}")


def _workspace(ws: str) -> str:
    if not _WS.match(ws or ""):
        raise HTTPException(400, f"not a workspace name: {ws!r}")
    return ws


def _agents() -> dict[str, dict]:
    """workspace -> {slug, title} for every agent of the node."""
    with cursor() as cur:
        cur.execute("SELECT slug, title, COALESCE(NULLIF(mesh_workspace, ''), slug) AS ws "
                    "FROM sn_config.agents ORDER BY slug")
        rows = cur.fetchall()
    return {r["ws"]: {"slug": r["slug"], "title": r["title"] or r["slug"]} for r in rows}


def _counts() -> dict[str, dict]:
    rows = _mesh("GET", "/admin/workspaces", "*") or []
    return {r["workspace"]: r for r in rows if isinstance(r, dict) and r.get("workspace")}


# A first heading that every document of its kind shares says nothing in a list:
# a handoff opens "# Session handoff" and names its subject in the next heading.
_GENERIC = {"session handoff", "handoff"}
# The bookkeeping lines a handoff opens with: who, which conversation, which
# session. A preview starts after them, with what the document says.
_BOOKKEEPING = re.compile(r"^(agent|conversation|prior session|session|date|channel|caller)\s*:", re.I)


def _strip_kind(title: str) -> str:
    """'Handoff: the release' -> 'the release': the type says it already."""
    return title.split(":", 1)[1].strip() if title.lower().startswith("handoff:") and title[8:].strip() else title


def _preview(content: str) -> str:
    lines = [x.strip() for x in content.splitlines() if x.strip()]
    body = [x for x in lines if not x.startswith("#") and not _BOOKKEEPING.match(x.lstrip("-* "))]
    return re.sub(r"\s+", " ", " ".join(body or lines))[:280]


def _title(d: dict) -> str:
    name = (d.get("filename") or "").strip()
    if name:
        return name[:160]
    lines = [x.strip() for x in (d.get("content") or "").splitlines() if x.strip()]
    if not lines:
        return "(empty)"
    first = lines[0].lstrip("#").strip()
    if first.lower() in _GENERIC:
        for x in lines[1:]:
            if x.startswith("#"):
                sub = _strip_kind(x.lstrip("#").strip())
                if sub:
                    return sub[:160]
    return _strip_kind(first)[:160]


def _type(tags) -> str | None:
    for t in tags or []:
        if isinstance(t, str) and t.startswith("type:"):
            return t[5:]
    return None


def _row(d: dict, ws: str, full: bool = False) -> dict:
    content = d.get("content") or ""
    out = {
        "guid": d.get("guid"), "workspace": d.get("workspace") if d.get("workspace") not in (None, "*") else ws,
        "title": _title(d), "type": _type(d.get("tags")), "tags": d.get("tags") or [],
        "source": d.get("source"), "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
    }
    if full:
        out["content"] = content
    else:
        out["preview"] = _preview(content)
    if d.get("similarity_score") is not None:
        out["score"] = d["similarity_score"]
    return out


@router.get("/workspaces", dependencies=[Depends(requires("memory.read"))])
def workspaces():
    """Every workspace that holds documents, and every agent's even when empty."""
    counts = _counts()
    agents = _agents()
    out = []
    for ws in sorted(set(counts) | set(agents)):
        c = counts.get(ws) or {}
        out.append({"workspace": ws, "documents": int(c.get("documents") or 0),
                    "last_activity": c.get("last_activity"), "agent": agents.get(ws)})
    out.sort(key=lambda r: (-r["documents"], r["workspace"]))
    return {"workspaces": out, "total": sum(r["documents"] for r in out)}


@router.get("/types", dependencies=[Depends(requires("memory.read"))])
def types():
    data = _mesh("GET", "/tags?prefix=type:&limit=100", "*") or {}
    return {"types": [{"type": t["tag"][5:], "count": t.get("count", 0)}
                      for t in data.get("tags") or [] if str(t.get("tag", "")).startswith("type:")]}


@router.get("/documents", dependencies=[Depends(requires("memory.read"))])
def documents(workspace: str = Query("all"), type: str | None = Query(None),
              limit: int = Query(30, ge=1, le=50), offset: int = Query(0, ge=0, le=_MAX_WINDOW)):
    """The newest documents, of one workspace or of all of them merged by date."""
    if type is not None and not _TYPE.match(type):
        raise HTTPException(400, f"not a document type: {type!r}")
    window = min(offset + limit, _MAX_WINDOW)
    q = f"/?all=1&limit={window}" + (f"&tag=type:{type}" if type else "")
    if workspace == "all":
        names = [ws for ws, c in _counts().items() if int(c.get("documents") or 0) > 0]
    else:
        names = [_workspace(workspace)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        lists = list(pool.map(lambda ws: [_row(d, ws) for d in (_mesh("GET", q, ws) or [])], names))
    rows = sorted((r for rows in lists for r in rows), key=lambda r: r["created_at"] or "", reverse=True)
    page = rows[offset:offset + limit]
    return {"documents": page, "offset": offset, "limit": limit,
            "more": len(rows) > offset + limit and offset + limit < _MAX_WINDOW}


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    workspaces: list[str] | None = None
    type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = Field(20, ge=1, le=50)


@router.post("/search", dependencies=[Depends(requires("memory.read"))])
def search(body: SearchIn):
    """By meaning, over the named workspaces or every workspace with documents."""
    if body.type is not None and not _TYPE.match(body.type):
        raise HTTPException(400, f"not a document type: {body.type!r}")
    names = [_workspace(w) for w in body.workspaces] if body.workspaces else \
        [ws for ws, c in _counts().items() if int(c.get("documents") or 0) > 0]
    if not names:
        return {"results": [], "query": body.query}
    req = {"query": body.query, "limit": body.limit, "workspaces": {ws: 1.0 for ws in names}}
    if body.type:
        req["tags"] = [f"type:{body.type}"]
    if body.date_from:
        req["date_from"] = body.date_from.isoformat()
    if body.date_to:
        req["date_to"] = body.date_to.isoformat()
    data = _mesh("POST", "/search", names[0], req, timeout=30) or {}
    return {"results": [_row(d, d.get("workspace") or names[0]) for d in data.get("results") or []],
            "query": body.query}


@router.get("/documents/{guid}", dependencies=[Depends(requires("memory.read"))])
def document(guid: str, workspace: str = Query(...)):
    if not _GUID.match(guid):
        raise HTTPException(400, "not a document id")
    return _row(_mesh("GET", "/" + guid, _workspace(workspace)) or {}, workspace, full=True)
