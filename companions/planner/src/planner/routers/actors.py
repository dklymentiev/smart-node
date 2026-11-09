"""REST endpoints for actors."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from planner.auth import require_api_key
from planner.repos.actors import ActorsRepo
from planner.schemas import ActorIn, ActorOut, ActorPatch
from planner.models import ActorType

router = APIRouter(
    prefix="/api/v1/actors",
    tags=["actors"],
    dependencies=[Depends(require_api_key)],
)


def _repo(request: Request) -> ActorsRepo:
    pool: asyncpg.Pool = request.app.state.pool
    return ActorsRepo(pool)


@router.post("", response_model=ActorOut, status_code=status.HTTP_201_CREATED)
async def create_actor(body: ActorIn, request: Request) -> ActorOut:
    actor = await _repo(request).create(body.id, body.display_name, body.type)
    return ActorOut.model_validate(actor)


@router.get("", response_model=list[ActorOut])
async def list_actors(
    request: Request,
    type: ActorType | None = None,
    active: bool | None = None,
) -> list[ActorOut]:
    rows = await _repo(request).list(type=type, active=active)
    return [ActorOut.model_validate(r) for r in rows]


@router.get("/{actor_id}", response_model=ActorOut)
async def get_actor(actor_id: str, request: Request) -> ActorOut:
    actor = await _repo(request).get(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"actor {actor_id!r} not found")
    return ActorOut.model_validate(actor)


@router.patch("/{actor_id}", response_model=ActorOut)
async def update_actor(actor_id: str, body: ActorPatch, request: Request) -> ActorOut:
    fields = body.model_dump(exclude_unset=True)
    actor = await _repo(request).update(actor_id, **fields)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"actor {actor_id!r} not found")
    return ActorOut.model_validate(actor)


@router.delete("/{actor_id}", response_model=ActorOut)
async def deactivate_actor(actor_id: str, request: Request) -> ActorOut:
    """Soft-delete: actors are deactivated, never removed."""
    actor = await _repo(request).deactivate(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"actor {actor_id!r} not found")
    return ActorOut.model_validate(actor)
