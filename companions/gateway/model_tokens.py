"""Tokens for an agent's Claude Code process: minted by the runtime at spawn,
checked by the door on every model call.

The door (main.py, `/anthropic/*`) and the runtime (runtime_main.py, the owner
of the agent processes) are two processes, so a token cannot live in either
one's memory. It lives in `sn_core.model_tokens`, as a SHA-256 digest only,
the way `sn_config.actor_credentials` keeps a credential: the token itself
exists in the runtime for the moment of the spawn and in the agent's process
environment, nowhere else.

What that buys: a restart of the door leaves every running agent's token
valid, so an agent in the middle of a turn loses one call and retries. A
restart of the runtime revokes every token, because no process it minted
them for survives it; an orphan that does is refused at the door and dies.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from typing import Dict, Optional

import db

logger = logging.getLogger("gateway.model_tokens")

PREFIX = "sn-"
# A revoked token can open the door for at most this long after revocation;
# an unknown bearer is looked up at most this often.
CACHE_SECONDS = 30.0
MISS_SECONDS = 10.0
PRUNE_AFTER_DAYS = 7

_cache: Dict[str, tuple] = {}     # digest -> (record or None, monotonic time)
_lock = threading.Lock()


def digest_of(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(session_id: Optional[str], agent: Optional[str], os_user: Optional[str],
          model: Optional[str], provider: Optional[str] = None) -> str:
    """A fresh token for one process. A session holds one live token:
    re-issuing (a respawn) revokes the previous one. Raises when the store
    cannot be written: a process without a stored token would fail every
    model call, so the spawn fails here, loudly.

    `provider` is the cli_providers slug the process runs under; the door
    routes the process's model calls by it (anthropic_relay). None means
    claude-cli, the node's own."""
    token = PREFIX + secrets.token_urlsafe(32)
    if session_id:
        db.query("UPDATE sn_core.model_tokens SET revoked_at = now(), revoke_reason = 'reissued' "
                 "WHERE session_id = %s AND revoked_at IS NULL", (session_id,))
    db.query("INSERT INTO sn_core.model_tokens (digest, session_id, agent, os_user, model, provider) "
             "VALUES (%s, %s, %s, %s, %s, %s)",
             (digest_of(token), session_id, agent, os_user, model, provider))
    return token


def revoke_session(session_id: Optional[str], reason: str = "process ended") -> None:
    """The process is gone; its token must not open the door again. Never
    raises: this runs on every reclaim path."""
    if not session_id:
        return
    try:
        db.query("UPDATE sn_core.model_tokens SET revoked_at = now(), revoke_reason = %s "
                 "WHERE session_id = %s AND revoked_at IS NULL", (reason, session_id))
    except Exception as e:  # noqa: BLE001
        logger.error("model token of session %s not revoked: %s", session_id, e)


def revoke_all(reason: str) -> int:
    """Every live token. For the runtime's start and stop: nothing it minted
    for is running any more."""
    rows = db.query("UPDATE sn_core.model_tokens SET revoked_at = now(), revoke_reason = %s "
                    "WHERE revoked_at IS NULL RETURNING id", (reason,), fetch=True)
    return len(rows or [])


def prune(days: int = PRUNE_AFTER_DAYS) -> None:
    db.query("DELETE FROM sn_core.model_tokens WHERE revoked_at < now() - make_interval(days => %s)",
             (days,))


def live_count() -> int:
    rows = db.query("SELECT count(*) FROM sn_core.model_tokens WHERE revoked_at IS NULL", fetch=True)
    return int(rows[0][0]) if rows else 0


def lookup(token: str) -> Optional[dict]:
    """The live token's record (session_id, agent, os_user, model, provider), or None.
    Synchronous; the door calls it in a thread. Cached briefly so a turn's
    stream of calls costs one read."""
    if not token or not token.startswith(PREFIX):
        return None
    d = digest_of(token)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(d)
    if hit is not None:
        info, at = hit
        if now - at < (CACHE_SECONDS if info else MISS_SECONDS):
            return info
    rows = db.query("SELECT session_id, agent, os_user, model, provider FROM sn_core.model_tokens "
                    "WHERE digest = %s AND revoked_at IS NULL", (d,), fetch=True, as_dict=True)
    info = dict(rows[0]) if rows else None
    with _lock:
        if len(_cache) > 4096:
            _cache.clear()
        _cache[d] = (info, now)
    return info


def forget() -> None:
    """Drop the cache (tests, and after a revoke_all)."""
    with _lock:
        _cache.clear()
