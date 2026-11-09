"""The attachment scan stage.

Every attachment passes through verdict() before anything serves it, so the
place where rules run exists from the first file. PHASE 1, the owner's decision
of 2026-09-10: "for now let it pass every attachment of trusted, allowed
senders, just so that we have a method that lets us scan them later; the key
problem today is that they are not saved." The rule list (what to block, and
how strictly by how far a sender is trusted) is enforced here.

Pure functions only: no I/O, no model, no network. The caller stores the
verdict and its reason on the row and emits the event.
"""

APPROVED = "approved"
RECEIVED = "received"

# A storage name is data too: Claude Read classifies a local file by its suffix.
# Keep this mapping explicit and conservative rather than trusting a caller's
# filename (which is display-only) or the host-dependent mimetypes registry.
_STORAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/css": ".css",
    "application/json": ".json",
    "application/ld+json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/javascript": ".js",
    "application/yaml": ".yaml",
    "application/x-yaml": ".yaml",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def storage_suffix(content_type: str) -> str:
    """A safe suffix derived only from the normalized content type."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    return _STORAGE_SUFFIXES.get(ctype, ".bin")

# The sources this stage knows. A desk upload is the owner's own file in the
# Studio chat; anything else is a channel name (email, telegram, ...).
DESK_OWNER = "desk-owner"


def verdict(source: str, trusted: bool) -> tuple:
    """(status, reason) for one attachment.

    source  DESK_OWNER for the owner's upload in the Studio chat, else the
            channel the file arrived on.
    trusted the contact gate's answer for the sender (deny by default);
            ignored for DESK_OWNER, which is the owner by construction.

    Phase 1 decides nothing from the content. What it does decide is recorded,
    so a later rule can tell a file it approved from one nobody looked at.
    """
    if source == DESK_OWNER:
        return APPROVED, "owner-upload"
    if trusted:
        return APPROVED, "trusted-sender"
    return RECEIVED, "unscanned"


# Image types a browser may render inline (thumbnails in the Studio thread).
# Decided from the BYTES, never from the name or the declared type: a file
# called photo.png that is really HTML must not be served as an image. SVG is
# deliberately absent, because it can carry script.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_type(head: bytes):
    """The image type the first bytes prove, or None."""
    for magic, ctype in _IMAGE_MAGIC:
        if head.startswith(magic):
            return ctype
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_filename(name: str, limit: int = 180) -> str:
    """A display name only: the last path segment, no control characters,
    bounded. Never used to build a path on disk (the stored name is a uuid plus
    a suffix derived from content type)."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    return base[:limit] or "file"
