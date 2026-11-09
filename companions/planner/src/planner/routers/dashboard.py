"""JSON API for dashboard aggregations.

Exposes the same read-models the web UI renders, so external consumers
(e.g. an aggregating front-end) can build their own dashboard without
re-implementing the aggregation SQL.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from planner.auth import ApiKey, require_api_key
from planner.repos.dashboard import DashboardRepo

router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_api_key)],
)


def _repo(request: Request) -> DashboardRepo:
    pool: asyncpg.Pool = request.app.state.pool
    return DashboardRepo(pool)


@router.get("/task-flow")
async def task_flow(
    request: Request, days: int = Query(default=30, ge=1, le=365)
) -> dict:
    series = await _repo(request).task_flow_series(days)
    return {"days": days, "series": series}


@router.get("/projects")
async def projects(request: Request) -> list[dict]:
    return await _repo(request).project_progress()


@router.get("/objectives")
async def objectives(
    request: Request, type: str = Query(default="goal")
) -> list[dict]:
    return await _repo(request).objective_progress(type)


@router.get("/attention")
async def attention(request: Request) -> list[dict]:
    return await _repo(request).attention_tasks()


@router.get("/my-tasks")
async def my_tasks(
    request: Request, api_key: ApiKey = Depends(require_api_key)
) -> list[dict]:
    return await _repo(request).tasks_for_actor(api_key.actor_id)


@router.get("/needs-review")
async def needs_review(request: Request) -> list[dict]:
    return await _repo(request).tasks_in_review()


@router.get("/agents-working")
async def agents_working(request: Request) -> list[dict]:
    return await _repo(request).agent_tasks()


@router.get("/recent-tasks")
async def recent_tasks(
    request: Request, limit: int = Query(default=15, ge=1, le=100)
) -> list[dict]:
    return await _repo(request).recent_tasks(limit)
