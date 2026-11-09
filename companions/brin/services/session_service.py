"""Shared session service -- JSON file storage for cross-channel handoff."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from models.session import CreateSessionRequest, CreateSessionResponse, SharedSession

logger = logging.getLogger(__name__)

# data/ is runtime state, rooted under the install tree (the _storage/
# pattern). BRIN_DATA_DIR lets the companion / host override the root.
_DATA_DIR = Path(os.environ.get("BRIN_DATA_DIR", str(Path(__file__).parent.parent / "data")))
SESSIONS_DIR = _DATA_DIR / "sessions"
TTL_DAYS = 7
WEBCHAT_BASE_URL = os.environ.get("WEBCHAT_BASE_URL", "https://chat.local")


class SessionService:
    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def create(self, req: CreateSessionRequest) -> CreateSessionResponse:
        """Create a shared session and return its URL."""
        session_id = uuid4().hex[:12]
        now = datetime.now(timezone.utc)

        session = SharedSession(
            id=session_id,
            channel=req.channel,
            messages=req.messages,
            specialist=req.specialist,
            model=req.model,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=TTL_DAYS)).isoformat(),
            metadata=req.metadata,
        )

        path = SESSIONS_DIR / f"{session_id}.json"
        path.write_text(json.dumps(session.model_dump(), ensure_ascii=False, indent=2))

        url = f"{WEBCHAT_BASE_URL}/s/{session_id}"
        logger.info(f"Session created: {session_id} ({len(req.messages)} msgs, channel={req.channel})")

        self._cleanup_expired()

        return CreateSessionResponse(session_id=session_id, url=url)

    def get(self, session_id: str) -> SharedSession | None:
        """Load session by ID, return None if not found or expired."""
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            session = SharedSession(**data)
        except Exception as e:
            logger.error(f"Failed to load session {session_id}: {e}")
            return None

        # Check expiry
        expires = datetime.fromisoformat(session.expires_at)
        if datetime.now(timezone.utc) > expires:
            path.unlink(missing_ok=True)
            logger.info(f"Session {session_id} expired, deleted")
            return None

        return session

    def _cleanup_expired(self) -> None:
        """Delete session files older than TTL_DAYS."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)
        deleted = 0
        for f in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                expires = datetime.fromisoformat(data.get("expires_at", ""))
                if datetime.now(timezone.utc) > expires:
                    f.unlink()
                    deleted += 1
            except Exception:
                # If we can't parse, check mtime
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    f.unlink()
                    deleted += 1
        if deleted:
            logger.info(f"Cleaned up {deleted} expired sessions")
