"""Maintenance endpoints for the Planner service.

These endpoints exist so an external scheduler (cron, the node's cron surface)
can trigger supervisory sweeps without an in-process timer -- a wedged process
cannot also silence its own supervisor.

HTTP auth:  require_api_key (same as every other write endpoint).
CLI caller: `planner maintenance sweep` (talks to the DB directly; no API key
            needed; suitable for cron).

POST /api/v1/maintenance/staleness-sweep runs pipeline.staleness_sweep() and
POST /api/v1/maintenance/recurrence-sweep runs pipeline.recurrence_sweep(); each
returns a JSON dict of what it did.  Every action
taken is already written to task_comments and task_notifications by
_apply_status_change, so the results are visible in the task log without any
additional storage here.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, Request

from planner.auth import ApiKey, require_api_key
from planner.pipeline import Pipeline

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/maintenance",
    tags=["maintenance"],
    dependencies=[Depends(require_api_key)],
)


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


@router.post("/staleness-sweep", response_model=dict)
async def staleness_sweep(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
) -> dict:
    """Run the staleness sweep once and return what it did.

    Actions taken (approval reminders, escalations, orphan unblocks) are written
    to task_comments and task_notifications by the pipeline, so they are visible
    in the task log without any additional storage.

    Returns a dict mapping action name to a list of task ids that were acted on.
    The result is also logged at INFO level so the cron runner sees it in stdout.
    """
    pipeline = Pipeline(_pool(request))
    result = await pipeline.staleness_sweep()
    total = sum(len(v) for v in result.values())
    logger.info(
        "staleness_sweep by %s: %d actions -- %s",
        api_key.actor_id,
        total,
        {k: v for k, v in result.items() if v},
    )
    return {"ok": True, "actor": api_key.actor_id, "actions": result, "total": total}


@router.post("/recurrence-sweep", response_model=dict)
async def recurrence_sweep(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
) -> dict:
    """Open the cycles of recurring tasks that came due and return what it did.

    Returns a dict mapping action name (reopened, still_open, ended,
    unapproved) to the task ids acted on.
    """
    pipeline = Pipeline(_pool(request))
    result = await pipeline.recurrence_sweep()
    total = sum(len(v) for v in result.values())
    logger.info(
        "recurrence_sweep by %s: %d actions: %s",
        api_key.actor_id,
        total,
        {k: v for k, v in result.items() if v},
    )
    return {"ok": True, "actor": api_key.actor_id, "actions": result, "total": total}
