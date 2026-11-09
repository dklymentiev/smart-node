"""
Request Logger for AI Gateway
Logs all requests/responses with IP, timestamps, tokens, and duration
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Set
import threading
import logging

logger = logging.getLogger(__name__)

from config import LOGS_DIR as _CONFIGURED_LOGS_DIR, LOG_CONTENT
LOGS_DIR = _CONFIGURED_LOGS_DIR
SESSIONS_DIR = LOGS_DIR / "sessions"
REQUESTS_LOG = LOGS_DIR / "requests.log"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_LOG_BACKUPS = 5


class RequestLogger:
    """Thread-safe request logger with per-session files and WebSocket broadcast"""

    def __init__(self):
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._ws_clients: Set = set()
        self._ws_lock = threading.Lock()
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        # Allow read access for dashboard container (www-data)
        os.chmod(LOGS_DIR, 0o755)
        os.chmod(SESSIONS_DIR, 0o755)

    def add_ws_client(self, queue: asyncio.Queue):
        with self._ws_lock:
            self._ws_clients.add(queue)

    def remove_ws_client(self, queue: asyncio.Queue):
        with self._ws_lock:
            self._ws_clients.discard(queue)

    def _broadcast_ws(self, data: dict):
        """Push log entry to all connected WebSocket clients"""
        with self._ws_lock:
            dead = []
            for q in self._ws_clients:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    # Drop oldest entry and retry - don't kill the client
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(data)
                    except Exception:
                        pass
                except Exception:
                    dead.append(q)
            for q in dead:
                self._ws_clients.discard(q)

    def _get_lock(self, session_id: str) -> threading.Lock:
        """Get or create a lock for a session"""
        with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def _rotate_if_needed(self, filepath: Path):
        """Rotate log file if it exceeds MAX_LOG_SIZE."""
        try:
            if filepath.exists() and filepath.stat().st_size > MAX_LOG_SIZE:
                # Shift backups: .5 -> delete, .4 -> .5, ... .1 -> .2, current -> .1
                for i in range(MAX_LOG_BACKUPS, 1, -1):
                    src = filepath.with_suffix(f".log.{i-1}")
                    dst = filepath.with_suffix(f".log.{i}")
                    if src.exists():
                        src.rename(dst)
                backup = filepath.with_suffix(f".log.1")
                filepath.rename(backup)
                os.chmod(backup, 0o644)
        except Exception as e:
            logger.error(f"Log rotation error: {e}")

    def _write_to_file(self, filepath: Path, data: dict):
        """Write JSON line to file with rotation for requests.log"""
        try:
            if filepath == REQUESTS_LOG:
                self._rotate_if_needed(filepath)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
            # Ensure new files get restricted permissions
            if filepath == REQUESTS_LOG:
                os.chmod(filepath, 0o644)
        except Exception as e:
            logger.error(f"Failed to write log: {e}")

    def _get_session_file(self, session_id: str) -> Path:
        """Get path for session log file"""
        return SESSIONS_DIR / f"{session_id}.jsonl"

    def log_request(
        self,
        session_id: str,
        ip: str,
        event_type: str,
        request_data: Optional[Dict[str, Any]] = None,
        response_data: Optional[Dict[str, Any]] = None,
        tokens: Optional[Dict[str, int]] = None,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
        model: Optional[str] = None,
        service: Optional[str] = None,
        cache_hit: Optional[bool] = None,
    ):
        """
        Log a request/response to session file

        Args:
            session_id: Session UUID
            ip: Client IP address
            event_type: create|chat|stream|close|error
            request_data: Request content (for chat: the message)
            response_data: Response content (for chat: the reply)
            tokens: {"input": N, "output": N}
            duration_ms: Request duration in milliseconds
            error: Error message if any
            model: Model name (haiku, sonnet, opus)
        """
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "ip": ip,
            "type": event_type,
        }

        if model:
            entry["model"] = model
        if service:
            entry["service"] = service

        if request_data and LOG_CONTENT:
            # Truncate long requests for readability
            if isinstance(request_data, dict) and "content" in request_data:
                content = request_data["content"]
                if isinstance(content, str) and len(content) > 2000:
                    entry["request"] = content[:2000] + f"... [truncated, total: {len(content)} chars]"
                else:
                    entry["request"] = content
            else:
                entry["request"] = request_data

        if response_data and LOG_CONTENT:
            # Truncate long responses
            if isinstance(response_data, dict) and "content" in response_data:
                content = response_data["content"]
                if isinstance(content, str) and len(content) > 2000:
                    entry["response"] = content[:2000] + f"... [truncated, total: {len(content)} chars]"
                else:
                    entry["response"] = content
            elif isinstance(response_data, str):
                if len(response_data) > 2000:
                    entry["response"] = response_data[:2000] + f"... [truncated, total: {len(response_data)} chars]"
                else:
                    entry["response"] = response_data
            else:
                entry["response"] = response_data

        if tokens:
            entry["tokens"] = tokens

        if duration_ms is not None:
            entry["duration_ms"] = round(duration_ms, 2)

        if error:
            entry["error"] = error

        # Write to session file
        session_file = self._get_session_file(session_id)
        lock = self._get_lock(session_id)

        with lock:
            self._write_to_file(session_file, entry)

        # Also write to general requests log for quick overview
        general_entry = {
            "ts": entry["ts"],
            "session_id": session_id,
            "ip": ip,
            "type": event_type,
        }
        if model:
            general_entry["model"] = model
        if service:
            general_entry["service"] = service
        if tokens:
            general_entry["tokens"] = tokens
        if duration_ms is not None:
            general_entry["duration_ms"] = round(duration_ms, 2)
        if error:
            general_entry["error"] = error
        if error and request_data:
            general_entry["request"] = request_data
        if cache_hit is not None:
            general_entry["cache_hit"] = cache_hit

        self._write_to_file(REQUESTS_LOG, general_entry)

        # Broadcast to WebSocket clients
        self._broadcast_ws(general_entry)

    def log_create_session(
        self,
        session_id: str,
        ip: str,
        model: str,
        system_prompt: Optional[str] = None,
        duration_ms: Optional[float] = None,
        service: Optional[str] = None,
    ):
        """Log session creation"""
        request_data = {"model": model}
        if system_prompt:
            request_data["system_prompt"] = system_prompt[:500] + "..." if len(system_prompt or "") > 500 else system_prompt

        self.log_request(
            session_id=session_id,
            ip=ip,
            event_type="create",
            request_data=request_data,
            model=model,
            duration_ms=duration_ms,
            service=service,
        )

    def log_chat(
        self,
        session_id: str,
        ip: str,
        message: str,
        response: str,
        tokens: Optional[Dict[str, int]] = None,
        duration_ms: Optional[float] = None,
        model: Optional[str] = None,
        service: Optional[str] = None,
        cache_hit: Optional[bool] = None,
    ):
        """Log chat message and response"""
        self.log_request(
            session_id=session_id,
            ip=ip,
            event_type="chat",
            request_data={"content": message},
            response_data={"content": response},
            tokens=tokens,
            duration_ms=duration_ms,
            model=model,
            service=service,
            cache_hit=cache_hit,
        )

    def log_close_session(
        self,
        session_id: str,
        ip: str,
        total_messages: int,
        duration_seconds: float,
    ):
        """Log session close"""
        self.log_request(
            session_id=session_id,
            ip=ip,
            event_type="close",
            response_data={
                "total_messages": total_messages,
                "session_duration_sec": round(duration_seconds, 2),
            },
        )

    def log_error(
        self,
        session_id: str,
        ip: str,
        error: str,
        event_type: str = "error",
    ):
        """Log an error"""
        self.log_request(
            session_id=session_id,
            ip=ip,
            event_type=event_type,
            error=error,
        )


# Global logger instance
request_logger = RequestLogger()
