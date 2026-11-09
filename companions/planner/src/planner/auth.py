"""Request authentication.

Mode is selected at startup via ``PLANNER_AUTH_MODE`` (DD-008):

- ``dev`` (default): every request is bound to a seeded ``dev`` actor.
  No header check, no login screen. Use only behind ``localhost``.
- ``proxy``: read the trusted reverse-proxy ``Remote-User`` header,
  materialise the actor on first sight. Configure your edge to be the
  only ingress -- the header is trusted blindly.
- ``apikey``: validate ``X-API-Key`` against the ``api_keys`` table.
  Plaintext keys live only at the moment of creation; the database
  stores SHA-256 hashes. A key is valid when its hash matches a row,
  ``revoked_at`` is NULL, and the linked actor is ``active = true``.

In every mode the resolved actor lands in ``request.state.actor_id``,
and the dependency returns an ``ApiKey``-shaped record (synthetic
``id=0`` in ``dev`` / ``proxy`` modes -- treat it as identity only).

DELEGATION (``apikey`` mode only)
---------------------------------
A shared front door -- a component that has already authenticated its own
callers and forwards their work here -- needs to present two things at once:
a credential proving it may come in, and the name of the person or agent it
is acting for. Neither mode could express that. ``apikey`` authors every call
as the key's owner, collapsing every caller behind the door into one name.
``proxy`` keeps the real name but believes the header from anybody able to
reach the socket.

So the two questions are separated. The KEY answers "may this caller in at
all". The proxy-user header answers "on whose behalf", and it is read ONLY
for a key whose ``can_delegate`` is true -- a key the operator has decided
authenticates its own callers. For every other key the header is ignored
entirely, not merely deprioritised, and the key's own actor authors the work.

A delegating key with no header authors as itself, which is the right answer
for the door's own housekeeping calls. A delegating key with an unusable
header is refused rather than quietly recorded as the door: attribution that
silently falls back to the credential holder is how a forged name acquires
the authority of a real one.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

import asyncpg
from fastapi import HTTPException, Request, status

API_KEY_HEADER = "X-API-Key"
API_KEY_PREFIX = "pln_"
API_KEY_BYTES = 32  # 256 bits

AuthMode = Literal["dev", "proxy", "apikey"]
_VALID_MODES: tuple[AuthMode, ...] = ("dev", "proxy", "apikey")
ENV_AUTH_MODE = "PLANNER_AUTH_MODE"
ENV_PROXY_HEADER = "PLANNER_PROXY_USER_HEADER"
DEFAULT_PROXY_HEADER = "Remote-User"
DEV_ACTOR_ID = "dev"
_ACTOR_ID_RE = re.compile(r"^(?:[a-z0-9][a-z0-9._-]{0,63}|component:[a-z][a-z0-9._-]{0,53})$")


def get_auth_mode() -> AuthMode:
    """Return the configured auth mode, validated. Raises on invalid input."""
    raw = os.environ.get(ENV_AUTH_MODE, "dev").strip().lower()
    if raw not in _VALID_MODES:
        raise RuntimeError(
            f"{ENV_AUTH_MODE}={raw!r} -- must be one of: {', '.join(_VALID_MODES)}"
        )
    return raw  # type: ignore[return-value]


def _proxy_header_name() -> str:
    return os.environ.get(ENV_PROXY_HEADER, DEFAULT_PROXY_HEADER)


def _normalize_proxy_user(raw: str) -> str | None:
    """Coerce a ``Remote-User`` value into a valid actor id, or None.

    Lowercases, replaces invalid chars with ``.``, trims to 64 chars,
    enforces the same shape the actors table accepts. Empty / unsalvageable
    input returns None so the caller can 401.
    """
    if not raw:
        return None
    if raw.strip().lower().startswith("component:"):
        component = raw.strip().lower()
        return component if _ACTOR_ID_RE.fullmatch(component) else None
    cleaned = re.sub(r"[^a-z0-9._-]+", ".", raw.strip().lower()).strip(".")
    if not cleaned:
        return None
    cleaned = cleaned[:64]
    return cleaned if _ACTOR_ID_RE.match(cleaned) else None


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_plaintext_key() -> str:
    """Return a freshly generated key with the project prefix."""
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_BYTES)


@dataclass(slots=True, frozen=True)
class ApiKey:
    id: int
    label: str
    actor_id: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    # Defaulted so a record built by an older caller, a test, or one of the
    # synthetic modes is the WEAKER of the two: a missing field must never be
    # read as permission to speak for somebody else.
    can_delegate: bool = False


class ApiKeyError(ValueError):
    """Raised when an API key cannot be created or used."""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ApiKeysRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self, *, label: str, actor_id: str, can_delegate: bool = False
    ) -> tuple[ApiKey, str]:
        """Create a key. Returns (row, plaintext) -- plaintext is shown once.

        ``can_delegate`` is off unless the caller asks for it: the power to
        author work under somebody else's name is granted deliberately or not
        at all.
        """
        if not label.strip():
            raise ApiKeyError("label must not be blank")
        plaintext = generate_plaintext_key()
        key_hash = _hash_key(plaintext)
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO api_keys (key_hash, label, actor_id, can_delegate)
                VALUES ($1, $2, $3, $4)
                RETURNING id, label, actor_id, created_at, last_used_at,
                          revoked_at, can_delegate
                """,
                key_hash,
                label,
                actor_id,
                can_delegate,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ApiKeyError("hash collision -- generate a new key") from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise ApiKeyError(f"unknown actor_id: {actor_id!r}") from exc
        assert row is not None
        return ApiKey(**dict(row)), plaintext

    async def list(self, *, include_revoked: bool = False) -> list[ApiKey]:
        sql = (
            "SELECT id, label, actor_id, created_at, last_used_at, revoked_at, "
            "can_delegate FROM api_keys"
        )
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY id"
        rows = await self._pool.fetch(sql)
        return [ApiKey(**dict(r)) for r in rows]

    async def revoke(self, key_id: int) -> bool:
        result = await self._pool.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
            key_id,
        )
        return result.endswith(" 1")

    async def lookup_by_plaintext(self, plaintext: str) -> ApiKey | None:
        """Return the api-key row for a plaintext token, or None."""
        if not plaintext:
            return None
        row = await self._pool.fetchrow(
            """
            SELECT k.id, k.label, k.actor_id, k.created_at, k.last_used_at,
                   k.revoked_at, k.can_delegate
            FROM api_keys k
            JOIN actors a ON a.id = k.actor_id
            WHERE k.key_hash = $1
              AND k.revoked_at IS NULL
              AND a.active = true
            """,
            _hash_key(plaintext),
        )
        return ApiKey(**dict(row)) if row else None

    async def touch(self, key_id: int) -> None:
        """Update last_used_at to now. Best-effort; failures are silent."""
        try:
            await self._pool.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE id = $1",
                key_id,
            )
        except Exception:
            pass

    async def has_any(self) -> bool:
        n = await self._pool.fetchval("SELECT COUNT(*) FROM api_keys")
        return n > 0


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


async def ensure_root_key(pool: asyncpg.Pool) -> str | None:
    """If no api_keys exist, create one bound to the ``system`` actor.

    Returns the plaintext key (to be printed exactly once) or None if keys
    already exist.
    """
    repo = ApiKeysRepo(pool)
    if await repo.has_any():
        return None
    _, plaintext = await repo.create(label="root", actor_id="system")
    return plaintext


async def ensure_dev_actor(pool: asyncpg.Pool) -> None:
    """Insert the ``dev`` actor if missing. Idempotent. Used by ``dev`` mode."""
    await pool.execute(
        """
        INSERT INTO actors (id, display_name, type)
        VALUES ($1, 'Local Dev', 'human')
        ON CONFLICT (id) DO NOTHING
        """,
        DEV_ACTOR_ID,
    )


async def _ensure_proxy_actor(pool: asyncpg.Pool, actor_id: str) -> None:
    """Materialise the actor for a fresh Remote-User. Idempotent."""
    actor_type = "system" if actor_id.startswith("component:") else "human"
    await pool.execute(
        """
        INSERT INTO actors (id, display_name, type)
        VALUES ($1, $1, $2)
        ON CONFLICT (id) DO NOTHING
        """,
        actor_id,
        actor_type,
    )


def _synthetic_apikey(actor_id: str, label: str) -> ApiKey:
    """Build an ``ApiKey`` record for modes without a real ``api_keys`` row.

    ``id=0`` is never assigned to a real row, so synthetic records cannot
    collide. The label describes the auth source (``dev-auto`` /
    ``proxy:<user>``).
    """
    return ApiKey(
        id=0,
        label=label,
        actor_id=actor_id,
        created_at=datetime.now(timezone.utc),
        last_used_at=None,
        revoked_at=None,
    )


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def require_api_key(request: Request) -> ApiKey:
    """Resolve the request's actor according to ``PLANNER_AUTH_MODE``.

    Returns an ``ApiKey``-shaped record (synthetic in ``dev`` / ``proxy``).
    Raises 401 only in ``proxy`` (missing/invalid header) or ``apikey``
    (missing/invalid key). ``dev`` mode never fails.
    """
    mode = get_auth_mode()
    pool: asyncpg.Pool = request.app.state.pool

    if mode == "dev":
        api_key = _synthetic_apikey(DEV_ACTOR_ID, "dev-auto")
        request.state.actor_id = api_key.actor_id
        request.state.api_key_id = api_key.id
        return api_key

    if mode == "proxy":
        header = _proxy_header_name()
        raw = request.headers.get(header, "")
        actor_id = _normalize_proxy_user(raw)
        if actor_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"missing or invalid {header} header",
            )
        await _ensure_proxy_actor(pool, actor_id)
        api_key = _synthetic_apikey(actor_id, f"proxy:{actor_id}")
        request.state.actor_id = api_key.actor_id
        request.state.api_key_id = api_key.id
        return api_key

    # mode == "apikey"
    plaintext = request.headers.get(API_KEY_HEADER)
    if not plaintext:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {API_KEY_HEADER} header",
        )
    repo = ApiKeysRepo(pool)
    api_key = await repo.lookup_by_plaintext(plaintext)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or revoked API key",
        )

    # Delegation. Read the header ONLY for a key the operator marked as a front
    # door; for every other key it does not exist. `api_key.id` survives the
    # replacement, so the touch below still records the real key.
    if api_key.can_delegate:
        header = _proxy_header_name()
        raw = request.headers.get(header, "")
        if raw.strip():
            delegated = _normalize_proxy_user(raw)
            if delegated is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"delegating key sent an unusable {header} value",
                )
            await _ensure_proxy_actor(pool, delegated)
            api_key = replace(api_key, actor_id=delegated)

    request.state.actor_id = api_key.actor_id
    request.state.api_key_id = api_key.id
    # Best-effort touch; do not block the request on failure.
    await repo.touch(api_key.id)
    return api_key
