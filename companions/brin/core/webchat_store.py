"""Webchat State Store: persists webchat chat history server-side.

Local-first approach: localStorage is primary, server is backup.
On fresh browser with empty localStorage, state is fetched from server.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("brin.webchat_store")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "_storage", "webchat.db")


class WebchatStore:
    """Stores webchat UI state (chats, active chat) per user."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS webchat_state (
                    user_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
        logger.info("WebchatStore initialized: %s", self.db_path)

    def save_state(self, user_id: str, state_json: str) -> str:
        """UPSERT state for user. Returns updated_at timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO webchat_state (user_id, state, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "state = excluded.state, updated_at = excluded.updated_at",
                (user_id, state_json, now),
            )
            conn.commit()
        return now

    def load_state(self, user_id: str) -> Optional[tuple[str, str]]:
        """Load state for user. Returns (state_json, updated_at) or None."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT state, updated_at FROM webchat_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row if row else None
