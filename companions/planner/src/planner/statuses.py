"""Three statuses, two dictionaries, and the legacy names the wire still speaks.

The task row (migration 0010) carries:

    status         queued | in_progress | closed
    wait_reason    why a queued task is not being worked on (a row of
                   ``wait_reasons``; NULL = waiting for someone to pick it up)
    close_outcome  how a closed task ended (a row of ``close_outcomes``)
    stage          the legacy one-word name of that situation, derived by a
                   database trigger through the dictionaries

Everything the planner used to call a status (``awaiting_approval``,
``pending_review``, ``blocked``, ``escalated``, ``done``, ``rejected`` ...) is a
*stage*: a projection of the three real columns. Writers may still send a stage
name and readers may still read one; that is what lets every flow, the Studio
and the runner keep working on the day the model changes. The real state is the
three columns, and a node extends them by inserting dictionary rows, not by
releasing code.

``PLANNER_STATUS_COMPAT`` decides what the ``status`` field on the wire shows:
``legacy`` (default) the stage name, ``new`` the three-valued status. The other
fields (``state``, ``stage``, ``wait_reason``, ``close_outcome``) are always
present, so a consumer can move at its own pace.
"""

from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from typing import Any, Final

import asyncpg

STATES: Final[tuple[str, ...]] = ("queued", "in_progress", "closed")

#: Legacy one-word stages: accepted on input, produced on output.
LEGACY_STAGES: Final[tuple[str, ...]] = (
    "open",
    "awaiting_approval",
    "in_progress",
    "pending_review",
    "blocked",
    "escalated",
    "done",
    "rejected",
)

#: Legacy names that are not a state of their own any more. Accepted on input
#: for old writers, never produced: an agent assessing a task is working on
#: it, and an incident reopens a task into work (the fact lives in the
#: relations and comments the incident endpoint writes).
LEGACY_ALIASES: Final[dict[str, str]] = {"assessing": "in_progress", "incident": "in_progress"}

ALL_INPUT_NAMES: Final[tuple[str, ...]] = STATES + LEGACY_STAGES + tuple(LEGACY_ALIASES)

DICTIONARIES: Final[tuple[str, ...]] = ("wait_reasons", "close_outcomes")

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_WAIT_STAGES = ("open", "awaiting_approval", "pending_review", "blocked", "escalated")
_CLOSE_STAGES = ("done", "rejected")


def is_state(name: object) -> bool:
    return name in STATES


def is_stage(name: object) -> bool:
    return name in LEGACY_STAGES or name in LEGACY_ALIASES


def normalize_stage(name: str) -> str:
    """Collapse the retired legacy names onto the stage they mean."""
    return LEGACY_ALIASES.get(name, name)


#: The vocabulary the current request asked for (``X-Status-Vocabulary`` header
#: or ``?vocabulary=`` query), or None to use the node default. Set by the app
#: middleware, so a consumer moves to the new names when it is ready, one
#: request at a time, and nothing is flipped under the readers that are not.
REQUEST_VOCABULARY: ContextVar[str | None] = ContextVar("planner_status_vocabulary", default=None)
VOCABULARY_HEADER: Final[str] = "X-Status-Vocabulary"


def normalize_vocabulary(value: object) -> str | None:
    v = str(value or "").strip().lower()
    if v in ("new", "state", "states", "3", "three"):
        return "new"
    if v in ("legacy", "stage", "stages", "old"):
        return "legacy"
    return None


def compat_mode() -> str:
    """``legacy`` (the wire ``status`` is the stage) or ``new`` (the three states).

    The request's choice wins; otherwise ``PLANNER_STATUS_COMPAT`` (default
    legacy) is the node's default for every reader that did not ask.
    """
    asked = REQUEST_VOCABULARY.get()
    if asked:
        return asked
    mode = os.environ.get("PLANNER_STATUS_COMPAT", "legacy").strip().lower()
    return "new" if mode == "new" else "legacy"


# ---------------------------------------------------------------------------
# The two directions, delegated to the SQL functions of migration 0010 so that
# Python and the trigger can never disagree.
# ---------------------------------------------------------------------------


async def stage_for(conn: asyncpg.Connection | asyncpg.Pool,
                    status: str, wait_reason: str | None, close_outcome: str | None) -> str:
    return await conn.fetchval("SELECT stage_for($1, $2, $3)", status, wait_reason, close_outcome)


async def state_for(conn: asyncpg.Connection | asyncpg.Pool,
                    stage: str) -> tuple[str, str | None, str | None]:
    row = await conn.fetchrow("SELECT * FROM state_for($1)", normalize_stage(stage))
    return row["o_status"], row["o_wait"], row["o_outcome"]


async def exists(conn: asyncpg.Connection | asyncpg.Pool, dictionary: str, code: str) -> bool:
    _check_dictionary(dictionary)
    return bool(await conn.fetchval(f"SELECT 1 FROM {dictionary} WHERE code = $1", code))


async def effects(conn: asyncpg.Connection | asyncpg.Pool, dictionary: str, code: str | None) -> dict:
    """The declared side effects of a dictionary row (empty when unknown)."""
    if not code:
        return {}
    _check_dictionary(dictionary)
    raw = await conn.fetchval(f"SELECT effects FROM {dictionary} WHERE code = $1", code)
    return _as_dict(raw)


async def resolve(conn: asyncpg.Connection | asyncpg.Pool, status: str,
                  wait_reason: str | None = None, close_outcome: str | None = None,
                  ) -> tuple[str, str | None, str | None]:
    """The real state a writer means, whichever vocabulary it used."""
    if is_state(status):
        return status, wait_reason, close_outcome
    if is_stage(status):
        return await state_for(conn, status)
    raise ValueError(f"unknown status: {status!r}")


async def comment_required(conn: asyncpg.Connection | asyncpg.Pool, status: str,
                           wait_reason: str | None = None, close_outcome: str | None = None) -> bool:
    """Whether this move must carry a comment.

    A close carries a report unless the outcome's row says otherwise; a stop in
    the queue says why when the reason's row asks for it (the seeded rows do
    for owner / person / dependency / schedule / error, not for approval and
    review, which are the pipeline's own hand-offs).
    """
    st, wait, outcome = await resolve(conn, status, wait_reason, close_outcome)
    if st == "closed":
        eff = await effects(conn, "close_outcomes", outcome)
        return bool(eff.get("comment_required", True))
    if st == "queued" and wait:
        eff = await effects(conn, "wait_reasons", wait)
        return bool(eff.get("comment_required", False))
    return False


# ---------------------------------------------------------------------------
# Dictionary administration
# ---------------------------------------------------------------------------


class DictionaryError(ValueError):
    """Invalid dictionary input (maps to 400)."""


class DictionaryInUse(DictionaryError):
    """The row is referenced by tasks (maps to 409)."""


class DictionaryNotFound(DictionaryError):
    """No such row (maps to 404)."""


_COLS = ("code", "label", "description", "legacy_stage", "legacy_default",
         "effects", "enabled", "sort_order", "created_at", "updated_at", "updated_by")
_WRITABLE = ("label", "description", "legacy_stage", "legacy_default", "effects", "enabled", "sort_order")


def _check_dictionary(dictionary: str) -> None:
    if dictionary not in DICTIONARIES:
        raise DictionaryError(f"unknown dictionary: {dictionary!r}")


def _as_dict(raw: object) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _row_out(row: asyncpg.Record) -> dict[str, Any]:
    out = dict(row)
    out["effects"] = _as_dict(out.get("effects"))
    return out


class DictionariesRepo:
    """CRUD over ``wait_reasons`` and ``close_outcomes``.

    The code is the identity and never changes; a row that is in use may be
    disabled but not deleted, so history keeps meaning what it meant.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list(self, dictionary: str, *, include_disabled: bool = False) -> list[dict]:
        _check_dictionary(dictionary)
        sql = f"SELECT {', '.join(_COLS)} FROM {dictionary}"
        if not include_disabled:
            sql += " WHERE enabled"
        sql += " ORDER BY sort_order, code"
        return [_row_out(r) for r in await self._pool.fetch(sql)]

    async def get(self, dictionary: str, code: str) -> dict | None:
        _check_dictionary(dictionary)
        row = await self._pool.fetchrow(
            f"SELECT {', '.join(_COLS)} FROM {dictionary} WHERE code = $1", code)
        return _row_out(row) if row else None

    def _validate(self, dictionary: str, fields: dict, *, creating: bool) -> dict:
        out: dict[str, Any] = {}
        if creating:
            code = str(fields.get("code") or "")
            if not _CODE_RE.match(code):
                raise DictionaryError("code must be 1-32 chars of [a-z0-9_], starting with a letter")
            out["code"] = code
        for key, value in fields.items():
            if key == "code":
                continue
            if key not in _WRITABLE:
                raise DictionaryError(f"unknown field: {key!r}")
            if key == "label":
                if not str(value or "").strip():
                    raise DictionaryError("label must not be blank")
                out[key] = str(value).strip()
            elif key == "description":
                out[key] = str(value or "")
            elif key == "legacy_stage":
                allowed = _WAIT_STAGES if dictionary == "wait_reasons" else _CLOSE_STAGES
                if value not in allowed:
                    raise DictionaryError(f"legacy_stage must be one of {list(allowed)}")
                out[key] = value
            elif key in ("legacy_default", "enabled"):
                if not isinstance(value, bool):
                    raise DictionaryError(f"{key} must be true or false")
                out[key] = value
            elif key == "effects":
                if not isinstance(value, dict):
                    raise DictionaryError("effects must be a JSON object")
                out[key] = json.dumps(value)
            elif key == "sort_order":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise DictionaryError("sort_order must be an integer")
                out[key] = value
        if creating and "label" not in out:
            raise DictionaryError("label is required")
        return out

    async def create(self, dictionary: str, fields: dict, *, actor: str | None = None) -> dict:
        _check_dictionary(dictionary)
        values = self._validate(dictionary, fields, creating=True)
        values["updated_by"] = actor
        cols = list(values)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        sql = (f"INSERT INTO {dictionary} ({', '.join(cols)}) VALUES ({placeholders}) "
               f"RETURNING {', '.join(_COLS)}")
        try:
            row = await self._pool.fetchrow(sql, *[values[c] for c in cols])
        except asyncpg.UniqueViolationError as exc:
            if "legacy_default" in str(exc):
                raise DictionaryError("another row is already the default for that legacy_stage") from exc
            raise DictionaryError(f"{values['code']!r} already exists") from exc
        except asyncpg.CheckViolationError as exc:
            raise DictionaryError(str(exc).splitlines()[0]) from exc
        return _row_out(row)

    async def update(self, dictionary: str, code: str, fields: dict, *, actor: str | None = None) -> dict:
        _check_dictionary(dictionary)
        values = self._validate(dictionary, fields, creating=False)
        if not values:
            row = await self.get(dictionary, code)
            if row is None:
                raise DictionaryNotFound(f"{dictionary}: no row {code!r}")
            return row
        values["updated_by"] = actor
        sets = [f"{k} = ${i + 1}" for i, k in enumerate(values)]
        sets.append("updated_at = now()")
        args = list(values.values()) + [code]
        try:
            row = await self._pool.fetchrow(
                f"UPDATE {dictionary} SET {', '.join(sets)} WHERE code = ${len(args)} "
                f"RETURNING {', '.join(_COLS)}", *args)
        except asyncpg.UniqueViolationError as exc:
            raise DictionaryError("another row is already the default for that legacy_stage") from exc
        except asyncpg.CheckViolationError as exc:
            raise DictionaryError(str(exc).splitlines()[0]) from exc
        if row is None:
            raise DictionaryNotFound(f"{dictionary}: no row {code!r}")
        return _row_out(row)

    async def delete(self, dictionary: str, code: str) -> None:
        _check_dictionary(dictionary)
        column = "wait_reason" if dictionary == "wait_reasons" else "close_outcome"
        in_use = await self._pool.fetchval(f"SELECT count(*) FROM tasks WHERE {column} = $1", code)
        if in_use:
            raise DictionaryInUse(
                f"{code!r} is on {in_use} task(s); disable it instead of deleting it")
        result = await self._pool.execute(f"DELETE FROM {dictionary} WHERE code = $1", code)
        if not result.endswith(" 1"):
            raise DictionaryNotFound(f"{dictionary}: no row {code!r}")
