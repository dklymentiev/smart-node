"""Pydantic schemas for the REST API.

Wire format only -- repos use plain dataclasses (planner.models). Schemas
narrow validation, name fields the way clients see them, and shape responses.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from planner import statuses as _st
from planner.models import (
    ActorType,
    ObjectiveStatus,
    ObjectiveType,
    ProjectStatus,
    RelationType,
    TaskBlockedReason,
    TaskComplexity,
    TaskPriority,
    TaskState,
)

_FROM_ATTR = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


class ActorIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)
    type: ActorType


class ActorPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    active: bool | None = None


class ActorOut(BaseModel):
    model_config = _FROM_ATTR

    id: str
    display_name: str
    type: ActorType
    active: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class ProjectIn(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=128)
    description: str | None = None
    sort_order: int = 0


class ProjectPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    status: ProjectStatus | None = None
    sort_order: int | None = None


class ProjectOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    slug: str
    title: str
    description: str | None
    status: ProjectStatus
    sort_order: int
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------


class ObjectiveIn(BaseModel):
    project_id: int
    title: str = Field(..., min_length=1, max_length=256)
    type: ObjectiveType
    parent_id: int | None = None
    description: str | None = None
    sort_order: int = 0
    status: ObjectiveStatus = "active"


class ObjectivePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    type: ObjectiveType | None = None
    status: ObjectiveStatus | None = None
    sort_order: int | None = None
    description: str | None = None
    parent_id: int | None = None
    clear_parent: bool = False


class ObjectiveOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    parent_id: int | None
    project_id: int
    title: str
    type: ObjectiveType
    status: ObjectiveStatus
    sort_order: int
    description: str | None
    created_at: datetime
    updated_at: datetime


class ObjectiveTreeNode(BaseModel):
    objective: ObjectiveOut
    depth: int
    children: list["ObjectiveTreeNode"] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    description: str | None = None
    project_id: int | None = None
    objective_id: int | None = None
    assignee: str | None = None
    requester: str | None = None
    reviewer: str | None = None
    priority: TaskPriority = "medium"
    # A legacy stage name (open, awaiting_approval, ...) or one of the three
    # states (queued, in_progress, closed); with a state, wait_reason /
    # close_outcome say the rest. See planner.statuses.
    status: str = "open"
    wait_reason: str | None = None
    close_outcome: str | None = None
    due_date: date | None = None
    complexity_hint: TaskComplexity | None = None
    scope: str | None = None
    eta: str | None = None
    workflow_id: int | None = None
    # A recurring task: '1 day', '2 weeks', '1 month' (or daily / weekly /
    # monthly). next_run is when the first cycle opens; default one interval
    # from now.
    recur_interval: str | None = None
    next_run: datetime | None = None


class TaskPatch(BaseModel):
    """Generic task patch -- any subset of writable fields.

    When ``status`` is supplied the change is routed through the pipeline state
    machine (audit log, notifications, cascade unblock). All other fields
    update the task row directly without creating a log entry.

    ``comment`` is only meaningful alongside a ``status`` change. It is
    required for ``blocked``, ``escalated``, and ``rejected`` transitions so
    that the reason for stopping work is always on record.
    """

    model_config = ConfigDict(extra="forbid")

    # Accepted by the wire schema only so the route can reject direct writes
    # with the stable, explicit 400 contract. Owner decision endpoints are the
    # only code allowed to stamp this field.
    approved_at: datetime | None = None

    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = None
    project_id: int | None = None
    objective_id: int | None = None
    assignee: str | None = None
    requester: str | None = None
    reviewer: str | None = None
    priority: TaskPriority | None = None
    # Legacy stage name or one of queued / in_progress / closed.
    status: str | None = None
    wait_reason: str | None = None
    close_outcome: str | None = None
    comment: str | None = Field(
        default=None,
        description="Status-change comment. Required for a close and for a stop whose reason asks for one.",
    )
    due_date: date | None = None
    complexity_hint: TaskComplexity | None = None
    scope: str | None = None
    eta: str | None = None
    blocked_by: int | None = None
    blocked_reason: TaskBlockedReason | None = None
    external_detail: str | None = None
    workflow_id: int | None = None
    # 'off' (or null) ends the recurrence and clears next_run.
    recur_interval: str | None = None
    next_run: datetime | None = None


class TaskOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    title: str
    description: str | None
    project_id: int | None
    objective_id: int | None
    assignee: str | None
    requester: str | None
    reviewer: str | None
    priority: TaskPriority
    #: PLANNER_STATUS_COMPAT=legacy (default): the stage name; new: the state.
    status: str
    stage: str
    state: TaskState
    wait_reason: str | None = None
    close_outcome: str | None = None
    created_at: datetime
    updated_at: datetime
    assessed_at: datetime | None
    approved_at: datetime | None
    execution_started_at: datetime | None
    completed_at: datetime | None
    due_date: date | None
    complexity_hint: TaskComplexity | None
    scope: str | None
    eta: str | None
    blocked_by: int | None
    blocked_reason: TaskBlockedReason | None
    external_detail: str | None
    previous_status: str | None
    verification_attempts: int
    workflow_id: int | None = None
    recur_interval: str | None = None
    next_run: datetime | None = None
    proposed_assignee: str | None = None

    @model_validator(mode="after")
    def _status_on_the_wire(self) -> TaskOut:
        if _st.compat_mode() == "new":
            self.status = self.state
        return self


# ---------------------------------------------------------------------------
# Ancillary
# ---------------------------------------------------------------------------


class CommentIn(BaseModel):
    content: str = Field(..., min_length=1)
    comment_type: str | None = None


class CommentOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    task_id: int
    author: str
    content: str
    comment_type: str | None
    created_at: datetime


class RelationIn(BaseModel):
    related_id: int
    relation: RelationType


class RelationOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    task_id: int
    related_id: int
    relation: RelationType
    created_at: datetime


class CommitIn(BaseModel):
    repo: str | None = None
    commit_hash: str | None = None
    message: str | None = None
    author: str | None = None
    content_body: str | None = None


class CommitOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    task_id: int
    repo: str | None
    commit_hash: str | None
    message: str | None
    author: str | None
    content_body: str | None
    committed_at: datetime


class LinkIn(BaseModel):
    kind: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    label: str | None = None


class LinkOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    task_id: int
    kind: str
    url: str
    label: str | None
    created_at: datetime


class LogEntryOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    task_id: int
    action: str
    old_value: str | None
    new_value: str | None
    actor: str
    created_at: datetime


class TagIn(BaseModel):
    tag: str = Field(..., min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    actor_id: str = Field(..., min_length=1)


class ApiKeyOut(BaseModel):
    model_config = _FROM_ATTR

    id: int
    label: str
    actor_id: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(BaseModel):
    """Response on POST /api-keys -- includes plaintext, returned once."""

    key: ApiKeyOut
    plaintext: str = Field(
        ...,
        description="Shown exactly once. Store it now -- it cannot be recovered.",
    )


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


class Problem(BaseModel):
    """RFC 7807-shaped error body."""

    status: int
    title: str
    detail: str | None = None


SortDir = Literal["asc", "desc"]
