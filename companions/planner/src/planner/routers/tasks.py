"""REST endpoints for tasks.

Status changes route through the pipeline state machine (audit, notify,
cascade). Field-only patches go directly to the repo. The distinction is
whether ``status`` is present in the PATCH body.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from planner import decisions
from planner import statuses as _st
from planner.auth import require_api_key
from planner.models import TaskPriority
from planner.pipeline import (
    Pipeline,
    PipelineError,
    StatusConflictError,
    TaskNotFoundError,
    _EXTRA_FIELDS_ALLOWED,
)
from planner.repos.tasks import TasksRepo, _owner_ids
from planner.schemas import TaskIn, TaskOut, TaskPatch

# Whether a move needs a comment is the dictionaries' call (planner.statuses
# .comment_required): a close carries a report, a stop says why when its
# reason asks for one. Checked here for an early, clear 400.

router = APIRouter(
    prefix="/api/v1/tasks",
    tags=["tasks"],
    dependencies=[Depends(require_api_key)],
)


def _repo(request: Request) -> TasksRepo:
    pool: asyncpg.Pool = request.app.state.pool
    return TasksRepo(pool)


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskIn, request: Request) -> TaskOut:
    fields = body.model_dump(exclude_unset=True)
    # The creator must never be empty: default requester to the authenticated
    # actor (auth resolves it into request.state.actor_id in every mode) when
    # the caller did not name one. Keeps "who created this" always populated.
    actor = getattr(request.state, "actor_id", None)
    if not fields.get("requester") and actor:
        fields["requester"] = actor
    try:
        task = await _repo(request).create(actor_id=actor, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TaskOut.model_validate(task)


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    request: Request,
    project_id: int | None = None,
    objective_id: int | None = None,
    assignee: str | None = None,
    status_filter: str | None = Query(
        default=None,
        description="a legacy stage name (open, pending_review, done ...) or "
                    "one of queued / in_progress / closed"),
    wait_reason: str | None = None,
    close_outcome: str | None = None,
    priority: TaskPriority | None = None,
    exclude_tags: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[TaskOut]:
    if status_filter is not None and status_filter not in _st.ALL_INPUT_NAMES:
        raise HTTPException(status_code=400, detail=f"unknown status {status_filter!r}")
    rows = await _repo(request).list(
        project_id=project_id,
        objective_id=objective_id,
        assignee=assignee,
        status=status_filter,
        wait_reason=wait_reason,
        close_outcome=close_outcome,
        priority=priority,
        exclude_tags=exclude_tags,
        limit=limit,
        offset=offset,
    )
    return [TaskOut.model_validate(r) for r in rows]


@router.get("/search", response_model=list[TaskOut])
async def search_tasks(
    request: Request,
    q: str = Query(..., min_length=1,
                   description="words, in any order; every word must occur in the "
                               "task's title, description or comments (case-insensitive)"),
    project_id: int | None = None,
    objective_id: int | None = None,
    author: str | None = Query(default=None, max_length=128,
                               description="only tasks this name wrote a comment on or filed"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TaskOut]:
    """Find prior/similar tasks by words, across titles, descriptions AND comments.
    Registered before /{task_id} on purpose: FastAPI matches routes in order and
    a bare /{task_id} would otherwise swallow /search and 422 on int coercion."""
    rows = await _repo(request).search(
        q=q, project_id=project_id, objective_id=objective_id, author=author, limit=limit,
    )
    return [TaskOut.model_validate(r) for r in rows]


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, request: Request) -> TaskOut:
    task = await _repo(request).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return TaskOut.model_validate(task)


class HandoffIn(BaseModel):
    from_actor: str = Field(..., min_length=1, max_length=128)
    to_actor: str = Field(..., min_length=1, max_length=128)
    notify: bool = True


@router.post("/{task_id}/handoff", response_model=TaskOut)
async def handoff_task(task_id: int, body: HandoffIn, request: Request) -> TaskOut:
    """Atomic assignee transfer; a concurrent reassignment returns 409."""
    if not body.notify and getattr(request.state, "actor_id", None) != "component:hitl-bridge":
        raise HTTPException(status_code=403, detail="deferred handoff wake is HITL-only")
    repo = _repo(request)
    task = await repo.handoff(task_id, from_actor=body.from_actor,
                              to_actor=body.to_actor, notify=body.notify)
    if task is None:
        if await repo.get(task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        raise HTTPException(status_code=409, detail="task assignee changed before handoff")
    return TaskOut.model_validate(task)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, body: TaskPatch, request: Request) -> TaskOut:
    fields = body.model_dump(exclude_unset=True)

    # Pull comment and status out before dispatching -- they are not raw columns.
    comment: str | None = fields.pop("comment", None)
    new_status: str | None = fields.pop("status", None)
    pool: asyncpg.Pool = request.app.state.pool

    if new_status is None and ("wait_reason" in fields or "close_outcome" in fields):
        raise HTTPException(
            status_code=400,
            detail="wait_reason / close_outcome travel with a status change (queued / closed)",
        )

    if new_status is not None:
        if new_status not in _st.ALL_INPUT_NAMES:
            raise HTTPException(status_code=400, detail=f"unknown status {new_status!r}")
        if "approved_at" in fields:
            raise HTTPException(
                status_code=400,
                detail="approved_at can only be set by the owner decision endpoint",
            )
        # Status change: pipeline path -- writes audit log, enqueues notification,
        # optionally records a comment, and triggers cascade unblock.
        try:
            needs_comment = await _st.comment_required(
                pool, new_status, fields.get("wait_reason"), fields.get("close_outcome"))
        except (ValueError, asyncpg.PostgresError) as exc:
            raise HTTPException(status_code=400, detail=str(exc).splitlines()[0])
        if needs_comment and not comment:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"a comment is required when moving a task to {new_status!r}; "
                    f"state why it is being stopped or what was delivered"
                ),
            )
        # An outcome a person must stand behind (not_possible, cancelled ...) is
        # not an agent's to set: effects.agent_may_set on the outcome row.
        if fields.get("close_outcome"):
            eff = await _st.effects(pool, "close_outcomes", fields["close_outcome"])
            if not bool(eff.get("agent_may_set", False)):
                actor_id = getattr(request.state, "actor_id", None)
                kind = await pool.fetchval(
                    "SELECT type FROM actors WHERE id = $1", actor_id) if actor_id else None
                if kind == "agent":
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"close_outcome {fields['close_outcome']!r} is a person's call; "
                            f"stop the task (status queued, wait_reason person) and say why"
                        ),
                    )

        # Remaining fields must be pipeline-compatible (extra_fields whitelist).
        # Non-pipeline field updates (title, description, priority, ...) must be
        # submitted in a separate PATCH so each write has one clear audit author.
        non_pipeline = sorted(k for k in fields if k not in _EXTRA_FIELDS_ALLOWED)
        if non_pipeline:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cannot update {non_pipeline} in the same request as a status "
                    f"change; send field-only updates in a separate PATCH"
                ),
            )

        actor: str = getattr(request.state, "actor_id", "system")
        try:
            task = await Pipeline(pool).status_change(
                task_id,
                new_status,
                actor=actor,
                comment=comment,
                extra_fields=fields or None,
            )
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return TaskOut.model_validate(task)

    # Field-only patch: repo path. The repo already fires assignee notify on a
    # real assignee change, so no extra work needed here.
    if "approved_at" in fields:
        raise HTTPException(status_code=400, detail="approved_at can only be set by the owner decision endpoint")
    task = await _repo(request).update(
        task_id, actor_id=getattr(request.state, "actor_id", None), **fields
    )
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return TaskOut.model_validate(task)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
# What a person answers when a task waits for them. Before these existed the
# Studio assembled it by hand: a comment, then PATCH {status, approved_at}.
# TaskPatch forbids approved_at, so Approve failed with 422 after its comment was
# already written, on every node. The pipeline already knew how to approve; it
# had no door. Each decision is written in the one shape a machine reads back
# (planner.decisions) and applied in one transaction (Pipeline.decide).


class DecisionIn(BaseModel):
    """The note that comes with a decision. modify and reject require one."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=8000)
    assignee: str | None = Field(default=None, max_length=128)


async def _decide(request: Request, task_id: int, decision: str, body: DecisionIn) -> TaskOut:
    if decision in (decisions.MODIFY, decisions.REJECT) and not (body.note or "").strip():
        what = "what to change" if decision == decisions.MODIFY else "why"
        raise HTTPException(status_code=400, detail=f"{decision} needs a note saying {what}")
    actor: str | None = getattr(request.state, "actor_id", None)
    if not actor:
        raise HTTPException(status_code=401, detail="decision requires an authenticated owner")
    pool: asyncpg.Pool = request.app.state.pool
    if actor not in _owner_ids():
        raise HTTPException(status_code=403, detail="only the node owner may approve, modify, or reject tasks")
    try:
        task = await Pipeline(pool).decide(
            task_id,
            decision,
            actor=actor,
            comment=decisions.compose(decision, body.note),
            assignee=body.assignee,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except StatusConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TaskOut.model_validate(task)


@router.post("/{task_id}/approve", response_model=TaskOut)
async def approve_task(task_id: int, body: DecisionIn, request: Request) -> TaskOut:
    """Approve a task in awaiting_approval: in_progress, approved_at stamped."""
    return await _decide(request, task_id, decisions.APPROVE, body)


@router.post("/{task_id}/modify", response_model=TaskOut)
async def modify_task(task_id: int, body: DecisionIn, request: Request) -> TaskOut:
    """Ask for changes on a task in awaiting_approval: in_progress, approved_at NOT
    stamped, the note says what to change."""
    return await _decide(request, task_id, decisions.MODIFY, body)


@router.post("/{task_id}/reject", response_model=TaskOut)
async def reject_task(task_id: int, body: DecisionIn, request: Request) -> TaskOut:
    """Reject a task that has not started; the note says why."""
    return await _decide(request, task_id, decisions.REJECT, body)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: int, request: Request) -> None:
    deleted = await _repo(request).delete(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
