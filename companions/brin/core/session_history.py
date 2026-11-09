"""Session History: writes conversation turns to JSON files on disk.

Each agent session gets a JSON file in its history_dir:
  /spaces/<agent>/2026-02-28_143022.json

Format:
{
  "session_id": "2026-02-28_143022",
  "agent": "branding-agent",
  "channel": "web",
  "user_id": "default",
  "started_at": "2026-02-28T14:30:22Z",
  "messages": [
    {"role": "user", "content": "...", "ts": "..."},
    {"role": "assistant", "content": "...", "ts": "..."}
  ]
}
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("brin.session_history")

# data/ is runtime state, rooted under the install tree (the _storage/
# pattern). BRIN_DATA_DIR lets the companion / host override the root.
_DATA_DIR = Path(os.environ.get("BRIN_DATA_DIR", str(Path(__file__).parent.parent / "data")))
DEFAULT_SPACES_DIR = str(_DATA_DIR / "spaces")


class SessionHistory:
    """Manages conversation history as JSON files per agent session."""

    def __init__(self, base_dir: str = DEFAULT_SPACES_DIR):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Active sessions: (user_id, agent_name) -> session file path
        self._active: dict[tuple[str, str], Path] = {}

    def start_session(
        self,
        agent_name: str,
        history_dir: str,
        user_id: str = "default",
        channel: str = "web",
    ) -> str:
        """Start a new session. Returns session_id."""
        now = datetime.now(timezone.utc)
        session_id = now.strftime("%Y-%m-%d_%H%M%S")

        target_dir = Path(history_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        session_data = {
            "session_id": session_id,
            "agent": agent_name,
            "channel": channel,
            "user_id": user_id,
            "started_at": now.isoformat(),
            "messages": [],
        }

        path = target_dir / f"{session_id}.json"
        path.write_text(
            json.dumps(session_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        key = (user_id, agent_name)
        self._active[key] = path

        logger.info(
            "Session started: %s agent=%s user=%s -> %s",
            session_id, agent_name, user_id, path,
        )
        return session_id

    def get_or_start_session(
        self,
        agent_name: str,
        history_dir: str,
        user_id: str = "default",
        channel: str = "web",
    ) -> str:
        """Get active session or start a new one. Returns session_id."""
        key = (user_id, agent_name)
        if key in self._active:
            path = self._active[key]
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    return data["session_id"]
                except (json.JSONDecodeError, KeyError):
                    pass

        return self.start_session(agent_name, history_dir, user_id, channel)

    def append(
        self,
        agent_name: str,
        user_id: str,
        role: str,
        content: str,
        client_turn_id: str = None,
    ) -> bool:
        """Append a message to active session. Returns False if no active session."""
        key = (user_id, agent_name)
        path = self._active.get(key)
        if not path or not path.exists():
            return False

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return False

        message = {
            "role": role,
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if client_turn_id:
            message["client_turn_id"] = client_turn_id
        data["messages"].append(message)

        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True

    def discard_client_turn(self, agent_name: str, user_id: str, client_turn_id: str) -> bool:
        """Remove an inbound desk message cancelled before provider delivery."""
        path = self._active.get((user_id, agent_name))
        if not path or not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return False
        old = data.get("messages") or []
        data["messages"] = [m for m in old if m.get("client_turn_id") != client_turn_id]
        if len(data["messages"]) == len(old):
            return False
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def end_session(self, agent_name: str, user_id: str = "default") -> Optional[dict]:
        """End active session. Returns session data or None."""
        key = (user_id, agent_name)
        path = self._active.pop(key, None)
        if not path or not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["ended_at"] = datetime.now(timezone.utc).isoformat()
            data["message_count"] = len(data["messages"])
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                "Session ended: %s agent=%s msgs=%d",
                data["session_id"], agent_name, data["message_count"],
            )
            return data
        except (json.JSONDecodeError, FileNotFoundError):
            return None

    def get_session(self, session_path: str) -> Optional[dict]:
        """Load a session by file path."""
        path = Path(session_path)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def list_sessions(
        self,
        history_dir: str,
        limit: int = 20,
    ) -> list[dict]:
        """List recent sessions for an agent (newest first)."""
        target = Path(history_dir)
        if not target.exists():
            return []

        files = sorted(target.glob("*.json"), reverse=True)[:limit]
        sessions = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append({
                    "session_id": data.get("session_id", f.stem),
                    "started_at": data.get("started_at", ""),
                    "ended_at": data.get("ended_at"),
                    "message_count": len(data.get("messages", [])),
                    "channel": data.get("channel", ""),
                    "path": str(f),
                })
            except json.JSONDecodeError:
                continue
        return sessions
