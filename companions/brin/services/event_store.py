"""EventStore -- SQLite-backed persistent event storage."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from models.event import EventRecord

logger = logging.getLogger("brin.event_store")

_DEFAULT_DB = os.environ.get(
    "EVENT_DB_PATH",
    str(Path(__file__).parent.parent / "_storage" / "events.db"),
)

# Default TTL per event type (hours)
DEFAULT_TTL: dict[str, int] = {
    "process.completed": 24,
    "process.failed": 168,
    "rein.task_done": 72,
    "rein.task_failed": 72,
    "rein.block_done": 1,
    "memory.ready": 1,
    "system.alert": 168,
}

_FALLBACK_TTL = 24


class EventStore:
    """Synchronous SQLite CRUD for events. Wrapped via run_in_executor by EventBus."""

    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info("EventStore initialized (db=%s)", self.db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0,
                    ttl_hours INTEGER NOT NULL DEFAULT 24
                );
                CREATE INDEX IF NOT EXISTS idx_events_unread
                    ON events(read, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_type
                    ON events(event_type, created_at);
            """)
        finally:
            conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> EventRecord:
        payload = {}
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        return EventRecord(
            id=row["id"],
            event_type=row["event_type"],
            source=row["source"] or "",
            payload=payload,
            created_at=row["created_at"] or "",
            read=bool(row["read"]),
        )

    def insert(self, event_type: str, payload: dict = None,
               source: str = "", ttl_hours: int = None) -> int:
        """Insert event, return its ID."""
        if ttl_hours is None:
            ttl_hours = DEFAULT_TTL.get(event_type, _FALLBACK_TTL)
        now = datetime.utcnow().isoformat()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        conn = self._conn()
        try:
            cur = conn.execute(
                "INSERT INTO events (event_type, source, payload, created_at, ttl_hours) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_type, source, payload_json, now, ttl_hours),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_unread(self, limit: int = 100) -> list[EventRecord]:
        """Get unread events ordered by creation time."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE read=0 ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    def mark_read(self, event_id: int) -> None:
        """Mark a single event as read."""
        conn = self._conn()
        try:
            conn.execute("UPDATE events SET read=1 WHERE id=?", (event_id,))
            conn.commit()
        finally:
            conn.close()

    def mark_all_read(self, event_ids: list[int]) -> None:
        """Mark multiple events as read."""
        if not event_ids:
            return
        conn = self._conn()
        try:
            placeholders = ",".join("?" for _ in event_ids)
            conn.execute(
                f"UPDATE events SET read=1 WHERE id IN ({placeholders})",
                event_ids,
            )
            conn.commit()
        finally:
            conn.close()

    def list_recent(self, event_type: Optional[str] = None,
                    limit: int = 50) -> list[EventRecord]:
        """List recent events, optionally filtered by type."""
        conn = self._conn()
        try:
            if event_type:
                rows = conn.execute(
                    "SELECT * FROM events WHERE event_type=? ORDER BY created_at DESC LIMIT ?",
                    (event_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    def count_unread(self) -> int:
        """Count unread events."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM events WHERE read=0").fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def cleanup_expired(self) -> int:
        """Delete events past their TTL. Returns number deleted."""
        conn = self._conn()
        try:
            # SQLite datetime arithmetic:
            # created_at + ttl_hours < now  =>  expired
            now = datetime.utcnow().isoformat()
            cur = conn.execute(
                "DELETE FROM events WHERE datetime(created_at, '+' || ttl_hours || ' hours') < ?",
                (now,),
            )
            conn.commit()
            deleted = cur.rowcount
            if deleted:
                logger.info("Cleaned up %d expired events", deleted)
            return deleted
        finally:
            conn.close()
