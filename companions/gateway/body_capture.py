"""Raw request and answer bodies of the model calls that pass the door.

A diagnostic with a clock, not a log: when SN_MODEL_CAPTURE_ENABLED is on, every metered call
is written to its own private directory for SN_MODEL_CAPTURE_WINDOW_SECONDS (a day by default)
counted from the first call, and the directory is swept of anything older. The bodies are the
prompts agents send and the answers they get, so the directory is 0700, the files 0600, and
no credential is written: only the headers named in _SAFE_HEADERS are kept.

The door has one attempt per call, so a call is one directory: request.body, response.body
(the answer as it arrived, an event stream stays one) and meta.json (who, which model, status,
outcome, duration). Capture never fails the call it describes.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gateway.body_capture")

ENABLED = os.getenv("SN_MODEL_CAPTURE_ENABLED", "false").lower() in ("1", "true", "yes")
WINDOW_SECONDS = max(0, int(os.getenv("SN_MODEL_CAPTURE_WINDOW_SECONDS", "86400")))
DIR = Path(os.getenv("SN_MODEL_CAPTURE_DIR", "/var/log/smart-node/gateway/model-capture"))
SWEEP_EVERY_SECONDS = 3600
MARKER = ".capture-until"
_SAFE_HEADERS = {"anthropic-version", "anthropic-beta", "content-type",
                 "user-agent", "x-claude-code-session-id"}

_LOCK = threading.Lock()
_deadline: Optional[float] = None
_swept_at = 0.0


def _write(path: Path, content: bytes, flags: int = os.O_TRUNC) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | flags, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)


def _sweep(cutoff: float) -> None:
    for child in DIR.iterdir():
        if child.name == MARKER:
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        except OSError as e:
            logger.warning("old body capture not removed: %s", e)


def enabled() -> bool:
    """On, inside the window. The window starts at the first call and is kept in a marker
    file, so a restart of the gateway does not open a new one."""
    global _deadline, _swept_at
    if not ENABLED or not WINDOW_SECONDS:
        return False
    with _LOCK:
        if _deadline is None:
            try:
                DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(DIR, 0o700)
                marker = DIR / MARKER
                try:
                    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    _deadline = time.time() + WINDOW_SECONDS
                    with os.fdopen(fd, "w", encoding="ascii") as f:
                        f.write(str(_deadline))
                except FileExistsError:
                    _deadline = float(marker.read_text(encoding="ascii"))
            except (OSError, ValueError) as e:
                logger.error("body capture window unavailable: %s", e)
                return False
        now = time.monotonic()
        if now - _swept_at >= SWEEP_EVERY_SECONDS:
            _swept_at = now
            try:
                _sweep(time.time() - WINDOW_SECONDS)
            except OSError as e:
                logger.warning("body capture sweep failed: %s", e)
        return time.time() < _deadline


class Capture:
    def __init__(self, rec: dict, method: str, url: str, headers: dict, body: bytes):
        self.path = DIR / rec["id"]
        self.path.mkdir(mode=0o700)
        self.meta = {
            "rid": rec["id"], "session_id": rec.get("session_id"), "agent": rec.get("agent"),
            "os_user": rec.get("os_user"), "model": rec.get("model"),
            "provider": rec.get("provider"), "started_at": time.time(),
            "method": method, "url": url,
            "request_headers": {k.lower(): v for k, v in headers.items()
                                if k.lower() in _SAFE_HEADERS},
        }
        _write(self.path / "request.body", body)
        self._save()

    def _save(self) -> None:
        _write(self.path / "meta.json",
               json.dumps(self.meta, ensure_ascii=False, indent=2).encode("utf-8"))

    def answer(self, status: int, headers: dict) -> None:
        self.meta.update(status=status, response_headers={
            k.lower(): v for k, v in headers.items() if k.lower() in _SAFE_HEADERS})
        self._save()

    def fallback(self, url: str, body: bytes, status: int, answer: bytes) -> None:
        """The first upstream refused (status); the call went on to another with this body.
        What the first one said is kept apart, so response.body stays the answer that was used."""
        self.meta.update(limited_status=status, fallback_url=url)
        _write(self.path / "limited.response.body", answer)
        _write(self.path / "fallback.request.body", body)
        self._save()

    def chunk(self, data: bytes) -> None:
        _write(self.path / "response.body", data, os.O_APPEND)

    def finish(self, outcome: Optional[str], status: Optional[int], duration_ms: Optional[int]) -> None:
        self.meta.update(outcome=outcome, status=status, duration_ms=duration_ms,
                         finished_at=time.time())
        self._save()


def begin(rec: dict, method: str, url: str, headers: dict, body: bytes) -> Optional[Capture]:
    """A capture for this call, or None when capture is off or could not start."""
    try:
        return Capture(rec, method, url, headers, body or b"") if enabled() else None
    except Exception as e:  # noqa: BLE001 -- capture must never fail a model call
        logger.error("body capture could not start (%s): %s", rec.get("id"), e)
        return None


def note(capture: Optional[Capture], what: str, *args) -> None:
    """Call one of the capture's methods, swallowing anything it raises."""
    if capture is None:
        return
    try:
        getattr(capture, what)(*args)
    except Exception as e:  # noqa: BLE001
        logger.error("body capture %s failed: %s", what, e)
