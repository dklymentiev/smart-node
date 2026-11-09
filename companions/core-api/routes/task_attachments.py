"""Files on a planner task.

    POST /api/planner/api/v1/tasks/{task_id}/attachments   store one file on the task
    GET  /api/planner/api/v1/tasks/{task_id}/attachments   the task's files, oldest first

WHY. A person answering a hitl question may attach files (a photo, a price
list). The answer lands on the task the question was about, so its files
belong there too: until now they stayed on the hitl host, outside the node,
and no agent could open them.

The upload takes the body contract of the Studio chat upload
(routes/desks.py upload_desk_attachment): the raw bytes as the body, the name
percent-encoded in X-Filename, the declared type in Content-Type. Stored the
same way (attachments/tasks/<task_id>/<uuid>.<suffix>, temp then rename), as a
sn_core.message_attachments row whose parent is the task (task_id), and served
by the existing GET /api/attachments/{id}/content to anyone who may read the
task (attachments.py asks may_read_task below).

These two paths sit under the planner proxy's prefix on purpose, where a
caller looks for a task's parts; this router is included before the proxy, so
it answers them and the proxy never forwards them. Who may write is the
proxy's own rule for a task mutation: a proved identity (kernel socket,
signed-in session, verified service), and an agent needs its planner grant.

Uploading the same file (name and bytes) to the same task again returns the
row already there: the hitl delivery job retries until it has posted.
"""

import hashlib
import json
import os
import urllib.parse
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

import attachment_scan
from auth import Principal, get_principal
from db import cursor
from events_emit import emit
from permissions import requires
from routes import planner

router = APIRouter(tags=["planner"])

_ATTACHMENT_DIR = os.environ.get("ATTACHMENT_DIR", "/srv/data/attachments")
_MAX_FILE = int(os.environ.get("SN_TASK_ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)))
_MAX_TOTAL = int(os.environ.get("SN_TASK_ATTACHMENTS_MAX_BYTES", str(500 * 1024 * 1024)))
_WHOLE_NODE_SEATS = ("super-admin", "operator")
SOURCE = "planner-task"


def may_read_task(principal: Principal | None, task_id: int) -> bool:
    """May this caller read planner task `task_id`? Blocking (db + planner).

    The whole-node seats may. Anyone else only when the planner itself serves
    them the task, asked as them; an agent also needs its planner grant, the
    proxy's gate for every planner call an agent makes."""
    if principal is None:
        return False
    if principal.role in _WHOLE_NODE_SEATS:
        return True
    _, agent_id, is_agent = planner._caller(principal.username)
    if is_agent and not planner._has_planner_grant(agent_id):
        return False
    try:
        status, _, _ = planner._forward("GET", f"api/v1/tasks/{int(task_id)}", "", b"",
                                        principal.username, None)
    except Exception:  # noqa: BLE001  planner unreachable: no proof the caller may read it
        return False
    return status == 200


def _task_exists(task_id: int) -> bool:
    status, _, _ = planner._forward("GET", f"api/v1/tasks/{int(task_id)}", "", b"", "admin", None)
    return status == 200


def _row(r) -> dict:
    return {
        "id": str(r["id"]),
        "task_id": r["task_id"],
        "filename": r["filename"],
        "content_type": r["content_type"],
        "size_bytes": r["size_bytes"],
        "content_hash": r["content_hash"],
        "status": r["status"],
        "scan_reason": r["scan_reason"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


_COLUMNS = "id, task_id, filename, content_type, size_bytes, content_hash, status, scan_reason, created_at"


def _store(task_id: int, data: bytes, name: str, ctype: str, principal: Principal):
    """Blocking: write the bytes and the row. Returns (status_code, row)."""
    digest = hashlib.sha256(data).hexdigest()
    with cursor(commit=True) as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM sn_core.message_attachments "
                    "WHERE task_id = %s AND content_hash = %s AND filename = %s", (task_id, digest, name))
        same = cur.fetchone()
        if same is not None:
            return 200, _row(same)
        cur.execute("SELECT COALESCE(sum(size_bytes), 0) AS total FROM sn_core.message_attachments "
                    "WHERE task_id = %s", (task_id,))
        total = cur.fetchone()["total"]
        if total + len(data) > _MAX_TOTAL:
            raise HTTPException(413, f"this task already holds {total} bytes of files; "
                                     f"the limit per task is {_MAX_TOTAL}")
        # The scan stage decides before anything serves the file (phase 1: a
        # proved node identity is a trusted sender).
        status, reason = attachment_scan.verdict(SOURCE, True)
        aid = str(uuid.uuid4())
        rel = os.path.join("tasks", str(int(task_id)), aid + attachment_scan.storage_suffix(ctype))
        path = os.path.join(_ATTACHMENT_DIR, rel)
        os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
        # Temp then rename: a crash mid-write leaves a .part, never a short file
        # under a name a row points at.
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        try:
            cur.execute(
                "INSERT INTO sn_core.message_attachments "
                "  (id, task_id, tenant_id, channel_type, filename, content_type, size_bytes, "
                "   content_hash, storage_path, status, scan_reason, received_at) "
                "VALUES (%s, %s, %s, 'planner', %s, %s, %s, %s, %s, %s, %s, now()) "
                f"RETURNING {_COLUMNS}",
                (aid, task_id, principal.tenant_id, name, ctype, len(data), digest, rel, status, reason),
            )
            r = cur.fetchone()
            emit(cur, actor=principal.actor_id or f"user:{principal.username}", event_type="attachment.stored",
                 entity_type="attachment", entity_id=aid,
                 payload={"task_id": task_id, "filename": name, "content_type": ctype,
                          "size_bytes": len(data), "status": status, "reason": reason,
                          "identity": principal.strength})
        except Exception:
            # The bytes reached disk before the row. A row that failed must not
            # leave a file nothing points at.
            try:
                os.remove(path)
            except OSError:
                pass
            raise
    return 201, _row(r)


@router.post("/api/planner/api/v1/tasks/{task_id}/attachments", status_code=201,
             dependencies=[Depends(requires("planner.proxy"))])
async def upload_task_attachment(task_id: int, request: Request,
                                 principal: Principal = Depends(get_principal)):
    """Store one file on planner task `task_id`; returns its attachment row.

    The body is the file's raw bytes; X-Filename its name (percent-encoded,
    display only) and Content-Type its declared type. An image is stored as the
    type its bytes prove; a file that claims to be an image and is not is kept
    as opaque bytes."""
    if principal.strength == "asserted":
        raise HTTPException(403, "storing a file on a task requires an authenticated session or a "
                                 "kernel-proved identity")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > _MAX_FILE:
        raise HTTPException(413, f"file too large: {len(data)} bytes, the limit is {_MAX_FILE}")
    # Existence first: the whole-node seats may read any task, and a file must
    # never hang from a task that is not there.
    if not await run_in_threadpool(_task_exists, task_id):
        raise HTTPException(404, f"task {task_id} not found")
    if not await run_in_threadpool(may_read_task, principal, task_id):
        raise HTTPException(403, f"task {task_id} is not readable by {principal.username}")
    name = attachment_scan.safe_filename(urllib.parse.unquote(request.headers.get("x-filename", "")))
    declared = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    proved = attachment_scan.sniff_image_type(data[:16])
    if proved:
        ctype = proved
    elif declared.startswith("image/") or not declared:
        ctype = "application/octet-stream"
    else:
        ctype = declared
    code, row = await run_in_threadpool(_store, task_id, data, name, ctype, principal)
    return Response(content=json.dumps(row), status_code=code, media_type="application/json")


@router.get("/api/planner/api/v1/tasks/{task_id}/attachments",
            dependencies=[Depends(requires("planner.proxy"))])
def list_task_attachments(task_id: int, principal: Principal = Depends(get_principal)):
    """The files on this task, oldest first. Read them through /api/attachments/{id}/content."""
    if not may_read_task(principal, task_id):
        raise HTTPException(403, f"task {task_id} is not readable by {principal.username}")
    with cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM sn_core.message_attachments WHERE task_id = %s "
                    "ORDER BY created_at", (task_id,))
        rows = cur.fetchall() or []
    return {"task_id": task_id, "attachments": [_row(r) for r in rows]}
