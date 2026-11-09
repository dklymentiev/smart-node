"""REST endpoints for objectives."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from planner.auth import require_api_key
from planner.models import ObjectiveNode, ObjectiveStatus, ObjectiveType
from planner.repos.objectives import ObjectivesRepo
from planner.repos.tasks import TasksRepo
from planner.schemas import (
    ObjectiveIn,
    ObjectiveOut,
    ObjectivePatch,
    ObjectiveTreeNode,
)

router = APIRouter(
    prefix="/api/v1/objectives",
    tags=["objectives"],
    dependencies=[Depends(require_api_key)],
)


def _repo(request: Request) -> ObjectivesRepo:
    pool: asyncpg.Pool = request.app.state.pool
    return ObjectivesRepo(pool)


def _to_tree_node(node: ObjectiveNode) -> ObjectiveTreeNode:
    return ObjectiveTreeNode(
        objective=ObjectiveOut.model_validate(node.objective),
        depth=node.depth,
        children=[_to_tree_node(c) for c in node.children],
    )


@router.post("", response_model=ObjectiveOut, status_code=status.HTTP_201_CREATED)
async def create_objective(body: ObjectiveIn, request: Request) -> ObjectiveOut:
    obj = await _repo(request).create(
        project_id=body.project_id,
        title=body.title,
        type=body.type,
        parent_id=body.parent_id,
        description=body.description,
        sort_order=body.sort_order,
        status=body.status,
    )
    return ObjectiveOut.model_validate(obj)


@router.get("", response_model=list[ObjectiveOut])
async def list_objectives(
    request: Request,
    project_id: int | None = None,
    parent_id: int | None = None,
    type: ObjectiveType | None = None,
    status_filter: ObjectiveStatus | None = None,
) -> list[ObjectiveOut]:
    rows = await _repo(request).list(
        project_id=project_id,
        parent_id=parent_id,
        type=type,
        status=status_filter,
    )
    return [ObjectiveOut.model_validate(r) for r in rows]


@router.get("/{objective_id}", response_model=ObjectiveOut)
async def get_objective(objective_id: int, request: Request) -> ObjectiveOut:
    obj = await _repo(request).get(objective_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"objective {objective_id} not found")
    return ObjectiveOut.model_validate(obj)


@router.patch("/{objective_id}", response_model=ObjectiveOut)
async def update_objective(
    objective_id: int, body: ObjectivePatch, request: Request
) -> ObjectiveOut:
    fields = body.model_dump(exclude_unset=True)
    obj = await _repo(request).update(objective_id, **fields)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"objective {objective_id} not found")
    return ObjectiveOut.model_validate(obj)


@router.delete("/{objective_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_objective(objective_id: int, request: Request) -> None:
    deleted = await _repo(request).delete(objective_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"objective {objective_id} not found")


@router.get("/projects/{project_id}/tree", response_model=list[ObjectiveTreeNode])
async def project_tree(project_id: int, request: Request) -> list[ObjectiveTreeNode]:
    nodes = await _repo(request).tree(project_id=project_id)
    return [_to_tree_node(n) for n in nodes]


@router.get("/{objective_id}/progress")
async def progress(objective_id: int, request: Request) -> dict:
    """Aggregate task counts under the objective subtree."""
    pool: asyncpg.Pool = request.app.state.pool
    obj_repo = ObjectivesRepo(pool)
    if await obj_repo.get(objective_id) is None:
        raise HTTPException(status_code=404, detail=f"objective {objective_id} not found")

    ids = await obj_repo.subtree_ids(objective_id)
    rows = await pool.fetch(
        """
        SELECT stage AS status, COUNT(*) AS n
        FROM tasks
        WHERE objective_id = ANY($1::int[])
        GROUP BY stage
        """,
        ids,
    )
    by_status = {r["status"]: r["n"] for r in rows}
    total = sum(by_status.values())
    done = by_status.get("done", 0)
    pct = int(round(100 * done / total)) if total else 0
    # The same subtree in the three statuses (0010): how many are queued, under
    # way and closed, why the queued ones wait and how the closed ones ended.
    # tasks_by_status above keeps the legacy stage names for older readers.
    state_rows = await pool.fetch(
        """
        SELECT status, wait_reason, close_outcome, COUNT(*) AS n
        FROM tasks
        WHERE objective_id = ANY($1::int[])
        GROUP BY status, wait_reason, close_outcome
        """,
        ids,
    )
    by_state: dict[str, int] = {}
    queued_by_reason: dict[str, int] = {}
    closed_by_outcome: dict[str, int] = {}
    for r in state_rows:
        by_state[r["status"]] = by_state.get(r["status"], 0) + r["n"]
        if r["status"] == "queued":
            key = r["wait_reason"] or "none"
            queued_by_reason[key] = queued_by_reason.get(key, 0) + r["n"]
        elif r["status"] == "closed" and r["close_outcome"]:
            key = r["close_outcome"]
            closed_by_outcome[key] = closed_by_outcome.get(key, 0) + r["n"]
    return {
        "objective_id": objective_id,
        "subtree_objective_count": len(ids),
        "total_tasks": total,
        "tasks_by_status": by_status,
        "tasks_by_state": by_state,
        "queued_by_reason": queued_by_reason,
        "closed_by_outcome": closed_by_outcome,
        "progress_pct": pct,
    }
