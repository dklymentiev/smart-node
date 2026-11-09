"""REST endpoints for the two status dictionaries.

    GET    /api/v1/dictionaries/wait-reasons
    GET    /api/v1/dictionaries/close-outcomes
    POST   /api/v1/dictionaries/{dictionary}
    GET    /api/v1/dictionaries/{dictionary}/{code}
    PATCH  /api/v1/dictionaries/{dictionary}/{code}
    DELETE /api/v1/dictionaries/{dictionary}/{code}

Reads are open to every authenticated caller: an agent must be able to see
which reasons exist before it stops a task. Writes are the owner's: the
dictionaries are how a node shapes its own planner, and that is the owner's
call, not an agent's.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from planner.auth import require_api_key
from planner.repos.tasks import _owner_ids
from planner.statuses import (
    DICTIONARIES,
    DictionariesRepo,
    DictionaryError,
    DictionaryInUse,
    DictionaryNotFound,
)

router = APIRouter(
    prefix="/api/v1/dictionaries",
    tags=["dictionaries"],
    dependencies=[Depends(require_api_key)],
)

_PATH_TO_TABLE = {"wait-reasons": "wait_reasons", "close-outcomes": "close_outcomes"}


class DictionaryRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=32)
    label: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    legacy_stage: str | None = None
    legacy_default: bool = False
    effects: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    sort_order: int = 100


class DictionaryRowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    legacy_stage: str | None = None
    legacy_default: bool | None = None
    effects: dict[str, Any] | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class DictionaryRowOut(BaseModel):
    code: str
    label: str
    description: str
    legacy_stage: str
    legacy_default: bool
    effects: dict[str, Any]
    enabled: bool
    sort_order: int
    updated_by: str | None = None


def _table(dictionary: str) -> str:
    table = _PATH_TO_TABLE.get(dictionary) or (dictionary if dictionary in DICTIONARIES else None)
    if table is None:
        raise HTTPException(status_code=404, detail=f"no dictionary {dictionary!r}")
    return table


def _repo(request: Request) -> DictionariesRepo:
    pool: asyncpg.Pool = request.app.state.pool
    return DictionariesRepo(pool)


def _owner_actor(request: Request) -> str:
    actor: str | None = getattr(request.state, "actor_id", None)
    if not actor:
        raise HTTPException(status_code=401, detail="dictionary changes require an authenticated owner")
    if actor not in _owner_ids():
        raise HTTPException(status_code=403, detail="only the node owner may change the dictionaries")
    return actor


def _raise(exc: DictionaryError) -> None:
    if isinstance(exc, DictionaryNotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DictionaryInUse):
        raise HTTPException(status_code=409, detail=str(exc))
    raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{dictionary}", response_model=list[DictionaryRowOut])
async def list_rows(dictionary: str, request: Request, include_disabled: bool = False) -> list[DictionaryRowOut]:
    rows = await _repo(request).list(_table(dictionary), include_disabled=include_disabled)
    return [DictionaryRowOut.model_validate(r) for r in rows]


@router.post("/{dictionary}", response_model=DictionaryRowOut, status_code=status.HTTP_201_CREATED)
async def create_row(dictionary: str, body: DictionaryRowIn, request: Request) -> DictionaryRowOut:
    actor = _owner_actor(request)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("legacy_stage") is None:
        fields.pop("legacy_stage", None)
    try:
        row = await _repo(request).create(_table(dictionary), fields, actor=actor)
    except DictionaryError as exc:
        _raise(exc)
    return DictionaryRowOut.model_validate(row)


@router.get("/{dictionary}/{code}", response_model=DictionaryRowOut)
async def get_row(dictionary: str, code: str, request: Request) -> DictionaryRowOut:
    row = await _repo(request).get(_table(dictionary), code)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{dictionary}: no row {code!r}")
    return DictionaryRowOut.model_validate(row)


@router.patch("/{dictionary}/{code}", response_model=DictionaryRowOut)
async def patch_row(dictionary: str, code: str, body: DictionaryRowPatch, request: Request) -> DictionaryRowOut:
    actor = _owner_actor(request)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    try:
        row = await _repo(request).update(_table(dictionary), code, fields, actor=actor)
    except DictionaryError as exc:
        _raise(exc)
    return DictionaryRowOut.model_validate(row)


@router.delete("/{dictionary}/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_row(dictionary: str, code: str, request: Request) -> None:
    _owner_actor(request)
    try:
        await _repo(request).delete(_table(dictionary), code)
    except DictionaryError as exc:
        _raise(exc)
