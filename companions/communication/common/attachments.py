"""Attachment storage for inbound channel messages.

bytes on the filesystem at
    /srv/data/attachments/YYYY/MM/{uuid}.bin
metadata in sn_core.message_attachments.

Call prepare_attachment() to produce a metadata dict from raw bytes.
Call flush_attachment_bytes() to write bytes to disk BEFORE the DB
transaction (tmp-then-rename for crash safety).
The DB write is handled by db.record_inbound_with_attachments().

No agent reads the filesystem directly. Bytes are served by the
retrieval API (core-api /api/attachments/) after an access check.
"""
import hashlib
import os
import uuid
from datetime import datetime, timezone

from common.config import get

# Both caps are node config; fall back to the configured attachment-size defaults.
MAX_SIZE_BYTES = int(get("ATTACHMENT_MAX_SIZE_BYTES", str(25 * 1024 * 1024)))  # 25 MB
MAX_PER_MESSAGE = int(get("ATTACHMENT_MAX_PER_MESSAGE", "10"))

# Base directory; provisioned by the communication installer.
ATTACHMENT_DIR = get("ATTACHMENT_DIR", "/srv/data/attachments")


def storage_rel_path(attachment_id: str, received_at=None) -> str:
    """Relative path within ATTACHMENT_DIR: YYYY/MM/{uuid}.bin

    Dated directories make the 90-day expiration sweep straightforward:
    a sweep job lists directories by month and deletes those older than
    the retention threshold without having to scan the full table first.
    """
    ts = received_at or datetime.now(timezone.utc)
    return "%04d/%02d/%s.bin" % (ts.year, ts.month, attachment_id)


def flush_attachment_bytes(att: dict) -> None:
    """Write the attachment's raw bytes to disk.

    Must be called BEFORE the DB transaction that inserts the metadata
    row. Write order: bytes first, then metadata. If the process dies
    between the two, the orphaned file is cleaned by the expiration
    sweep; the DB row is never written, so no access is possible to
    the orphan.

    Uses a tmp-file-then-rename pattern so a partial write cannot
    produce a truncated file at the final path.
    """
    rel_path = att["storage_path"]
    data = att["_data"]
    dest = os.path.join(ATTACHMENT_DIR, rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prepare_attachment(filename, content_type, data: bytes,
                       received_at=None) -> dict:
    """Produce a metadata dict for one attachment from raw MIME bytes.

    Writes nothing -- the caller writes bytes (flush_attachment_bytes)
    and then passes this dict (minus _data) to record_inbound_with_attachments.

    Keys in the returned dict:
        id            str   UUID for this attachment row
        filename      str   declared filename (sanitised; never trusted for paths)
        content_type  str   MIME type as declared
        size_bytes    int   actual byte count
        content_hash  str   SHA-256 hex
        storage_path  str   relative path within ATTACHMENT_DIR
        received_at   datetime or None
        _data         bytes raw bytes (stripped before DB insert)
    """
    att_id = str(uuid.uuid4())
    safe_name = (filename or "").strip() or ("attachment-%s" % att_id[:8])
    ctype = (content_type or "application/octet-stream").strip()
    content_hash = hashlib.sha256(data).hexdigest()
    rel_path = storage_rel_path(att_id, received_at)
    return {
        "id": att_id,
        "filename": safe_name,
        "content_type": ctype,
        "size_bytes": len(data),
        "content_hash": content_hash,
        "storage_path": rel_path,
        "received_at": received_at,
        "_data": data,
    }


def format_size(size_bytes: int) -> str:
    """Human-readable file size for the body summary line."""
    if size_bytes >= 1024 * 1024:
        return "%.1f MB" % (size_bytes / (1024 * 1024))
    if size_bytes >= 1024:
        return "%d KB" % (size_bytes // 1024)
    return "%d B" % size_bytes


def attachment_summary_line(att: dict) -> str:
    """The one-line summary inserted into the message body.

    It is the composer marker the dispatcher rewrites into a retrieval
    instruction for the receiving agent (companions/brin/core/
    attachment_delivery.py _MARKER), so the shape is the contract: filename,
    bare mime type, byte count, attachment id (the uuid recorded in
    sn_core.message_attachments). MIME parameters (charset, name) are dropped,
    because the marker admits a bare type only. The old human summary
    "[attachment: invoice.pdf (32 KB, application/pdf)]" matched no marker, so
    an agent was told a file existed and never how to open it.

    Format:
        [attachment: invoice.pdf, application/pdf, 32768 bytes, id 7f3b2c1a-...]
    """
    mime = (att["content_type"] or "application/octet-stream").split(";")[0].strip()
    return "[attachment: %s, %s, %d bytes, id %s]" % (
        att["filename"],
        mime,
        att["size_bytes"],
        att["id"],
    )


def attachment_handle(att: dict) -> dict:
    """The metadata handle stored in message metadata.attachments."""
    return {
        "id": att["id"],
        "filename": att["filename"],
        "content_type": att["content_type"],
        "size_bytes": att["size_bytes"],
    }
