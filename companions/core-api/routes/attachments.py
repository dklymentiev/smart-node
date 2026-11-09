"""Attachment retrieval --

Access rule: an agent that may read the parent message may read its
attachments. An agent that may not read the parent message gets a 403.
A file on a planner task follows the task: whoever may read the task.

No agent reads /srv/data/attachments/ directly; all bytes come through
this endpoint after the access check.

Text extraction is lazy: first access with mode=text triggers extraction
and caches the result in the metadata column. Supported types: text/*
(returned as-is). Other types return a 422 with an explanation.

Routes:
    GET /api/attachments/{id}              metadata only
    GET /api/attachments/{id}/content      raw bytes (application/octet-stream)
    GET /api/attachments/{id}/text         extracted text (text/plain)
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from auth import Principal, get_principal
from db import cursor
from permissions import requires

log = logging.getLogger("core-api.attachments")

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

# Base directory for attachment bytes. Should match ATTACHMENT_DIR in the
# communication companion's environment (communication.sh writes it to .env).
_ATTACHMENT_DIR = os.environ.get("ATTACHMENT_DIR", "/srv/data/attachments")


def _get_attachment(cur, attachment_id: str, principal: Principal):
    """Fetch the attachment row and enforce message-level access.

    Returns the row dict. Raises 404 or 403 as appropriate.
    """
    cur.execute(
        "SELECT a.id, a.message_id, a.tenant_id, a.channel_type, "
        "       a.filename, a.content_type, a.size_bytes, a.content_hash, "
        "       a.storage_path, a.status, a.received_at, a.created_at, a.task_id "
        "FROM sn_core.message_attachments a "
        "WHERE a.id = %s",
        (attachment_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "attachment not found")

    # A file attached in the Studio chat hangs from a desk, not a message
    #: its trust is the desk's -- the owner, the desk's own
    # agent, and the whole-node seats. Checked before the message rule, which
    # would otherwise look up a NULL message_id and refuse everyone.
    # A file on a planner task (routes/task_attachments.py) is readable by
    # whoever may read the task. Checked first, like the desk rule below,
    # since its message_id is NULL too.
    if row["task_id"] is not None:
        from routes.task_attachments import may_read_task
        if not may_read_task(principal, row["task_id"]):
            raise HTTPException(403, "access denied: the attachment belongs to a task you may not read")
    elif row["message_id"] is None:
        cur.execute(
            "SELECT d.tenant_id, d.owner_user_id, d.metadata->>'agent' AS agent "
            "FROM sn_core.message_attachments a JOIN sn_core.desks d ON d.id = a.desk_id "
            "WHERE a.id = %s",
            (attachment_id,),
        )
        desk = cur.fetchone()
        if desk is None:
            raise HTTPException(404, "attachment not found")
        allowed = (principal.role in ("super-admin", "operator")
                   or (desk["tenant_id"] == principal.tenant_id
                       and (desk["owner_user_id"] == principal.user_id
                            or (desk["agent"] and desk["agent"] == principal.username))))
        if not allowed:
            raise HTTPException(403, "access denied: the attachment belongs to another desk")
    # Enforce message-level access: can this principal read the parent message?
    # Cross-tenant principals (super-admin) may read any message.
    elif not principal.cross_tenant:
        cur.execute(
            "SELECT 1 FROM sn_core.messages WHERE id = %s AND tenant_id = %s",
            (row["message_id"], principal.tenant_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(403, "access denied: message belongs to another tenant")

    if row["status"] == "quarantined":
        raise HTTPException(403, "attachment is quarantined")
    if row["status"] == "expired":
        raise HTTPException(410, "attachment has expired and its bytes have been deleted")
    # The agent retrieval helper uses this generic door because its stable
    # handle contains the attachment id, not the desk id. Keep the same gate as
    # the desk-specific route: an unscanned `received` file (or any future
    # unknown status) must never become readable merely by knowing its UUID.
    if row["status"] not in ("approved", "processed"):
        raise HTTPException(403, f"attachment is {row['status']}, not approved")

    return row


@router.get("/{attachment_id}", response_model=dict,
            dependencies=[Depends(requires("attachments.read"))])
def get_attachment_metadata(
    attachment_id: str,
    principal: Principal = Depends(get_principal),
):
    """Return attachment metadata. Does not serve bytes."""
    with cursor() as cur:
        row = _get_attachment(cur, attachment_id, principal)
    return {
        "id": str(row["id"]),
        "message_id": row["message_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "content_hash": row["content_hash"],
        "status": row["status"],
        "received_at": row["received_at"].isoformat() if row["received_at"] else None,
    }


@router.get("/{attachment_id}/content",
            dependencies=[Depends(requires("attachments.read"))])
def get_attachment_bytes(
    attachment_id: str,
    principal: Principal = Depends(get_principal),
):
    """Serve the raw attachment bytes.

    Returns the file as application/octet-stream (or the stored content_type
    for types browsers render safely: text/plain, image/*). The Content-
    Disposition header is 'attachment' so browsers download rather than render
    arbitrary content, which protects against HTML/SVG-based XSS.
    """
    with cursor() as cur:
        row = _get_attachment(cur, attachment_id, principal)

    if not row["storage_path"]:
        raise HTTPException(500, "attachment has no storage path recorded")

    path = os.path.join(_ATTACHMENT_DIR, row["storage_path"])
    if not os.path.isfile(path):
        raise HTTPException(500, "attachment bytes not found on disk -- may have been moved")

    # Force download disposition to prevent arbitrary content execution in the
    # browser (no agent reads the filesystem directly).
    fname = row["filename"] or ("attachment-" + str(row["id"])[:8])
    ctype = row["content_type"] or "application/octet-stream"
    return FileResponse(
        path=path,
        media_type=ctype,
        filename=fname,
        headers={"Content-Disposition": "attachment; filename=%r" % fname},
    )


@router.get("/{attachment_id}/text", response_class=PlainTextResponse,
            dependencies=[Depends(requires("attachments.read"))])
def get_attachment_text(
    attachment_id: str,
    principal: Principal = Depends(get_principal),
):
    """Return the attachment content as plain text.

    Extraction is lazy: the first call reads
    the bytes from disk and returns them. Only text/* types are supported
    in v1; other types return 422. The caller must handle encoding issues
    (the response charset is UTF-8 with replacement for undecodable bytes).
    """
    with cursor() as cur:
        row = _get_attachment(cur, attachment_id, principal)

    ctype = (row["content_type"] or "").lower()
    if not ctype.startswith("text/"):
        raise HTTPException(
            422,
            "text extraction is not supported for %s in v1. "
            "Use /content to retrieve the raw bytes." % (row["content_type"] or "unknown type"),
        )

    if not row["storage_path"]:
        raise HTTPException(500, "attachment has no storage path recorded")

    path = os.path.join(_ATTACHMENT_DIR, row["storage_path"])
    if not os.path.isfile(path):
        raise HTTPException(500, "attachment bytes not found on disk")

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        log.error("failed to read attachment %s: %s", attachment_id, exc)
        raise HTTPException(500, "could not read attachment bytes")

    # Decode to text; replace undecodable bytes so a malformed file never
    # produces a 500. The charset from content_type is preferred; fall back
    # to UTF-8.
    charset = "utf-8"
    if "charset=" in ctype:
        try:
            charset = ctype.split("charset=")[-1].split(";")[0].strip()
        except Exception:
            pass
    text = raw.decode(charset, errors="replace")
    return PlainTextResponse(content=text)
