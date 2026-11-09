"""Plain dataclasses mirroring database rows.

Repos return these. Pydantic models for API I/O live elsewhere (FastAPI layer).
Keeping the layers separate avoids coupling persistence to HTTP serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ActorType = Literal["agent", "human", "system"]
ProjectStatus = Literal["active", "archived"]
ObjectiveType = Literal["strategy", "roadmap", "milestone", "goal", "initiative", "campaign"]
ObjectiveStatus = Literal["active", "completed", "paused", "cancelled", "draft"]

TaskPriority = Literal["low", "medium", "high", "critical"]
#: The three statuses a task has (migration 0010).
TaskState = Literal["queued", "in_progress", "closed"]
#: The legacy one-word stage names: a projection of (status, wait_reason,
#: close_outcome) through the dictionaries, still spoken on the wire while
#: PLANNER_STATUS_COMPAT=legacy. assessing and incident are accepted on
#: input and collapse to in_progress. See planner.statuses.
TaskStatus = Literal[
    "open",
    "assessing",
    "awaiting_approval",
    "in_progress",
    "pending_review",
    "done",
    "blocked",
    "escalated",
    "rejected",
    "incident",
]
TaskComplexity = Literal["simple", "moderate", "complex", "unknown"]
TaskBlockedReason = Literal["task_dependency", "external_approval", "scheduled_time"]


@dataclass(slots=True)
class Actor:
    id: str
    display_name: str
    type: ActorType
    active: bool
    created_at: datetime


@dataclass(slots=True)
class Project:
    id: int
    slug: str
    title: str
    description: str | None
    status: ProjectStatus
    sort_order: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class Objective:
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


@dataclass(slots=True)
class ObjectiveNode:
    """An objective plus its children (for tree responses)."""

    objective: Objective
    depth: int
    children: list["ObjectiveNode"]


RelationType = Literal["parent", "child", "hotfix", "postmortem"]


@dataclass(slots=True)
class TaskComment:
    id: int
    task_id: int
    author: str
    content: str
    comment_type: str | None
    created_at: datetime


@dataclass(slots=True)
class TaskRelation:
    id: int
    task_id: int
    related_id: int
    relation: RelationType
    created_at: datetime


@dataclass(slots=True)
class TaskCommit:
    id: int
    task_id: int
    repo: str | None
    commit_hash: str | None
    message: str | None
    author: str | None
    content_body: str | None
    committed_at: datetime


@dataclass(slots=True)
class TaskLogEntry:
    id: int
    task_id: int
    action: str
    old_value: str | None
    new_value: str | None
    actor: str
    created_at: datetime


@dataclass(slots=True)
class TaskLink:
    id: int
    task_id: int
    kind: str
    url: str
    label: str | None
    created_at: datetime


@dataclass(slots=True)
class TaskNotification:
    id: int
    task_id: int
    event_type: str
    notify_target: str | None  # None when no reviewer/assignee was available
    payload: dict | None
    created_at: datetime
    fired_at: datetime | None


@dataclass(slots=True)
class Task:
    id: int
    title: str
    description: str | None
    project_id: int | None
    objective_id: int | None
    assignee: str | None
    requester: str | None
    reviewer: str | None  # agent/human who reviews in pending_review; set by 0006
    priority: TaskPriority
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    assessed_at: datetime | None
    approved_at: datetime | None
    execution_started_at: datetime | None
    completed_at: datetime | None
    due_date: object | None
    complexity_hint: TaskComplexity | None
    scope: str | None
    eta: str | None
    blocked_by: int | None
    blocked_reason: TaskBlockedReason | None
    external_detail: str | None
    previous_status: TaskStatus | None
    verification_attempts: int
    workflow_id: int | None
    # The real state (migration 0010). ``status`` above is the legacy stage.
    state: TaskState = "queued"
    wait_reason: str | None = None
    close_outcome: str | None = None
    # Recurrence (migration 0011): the interval as Postgres prints it
    # ('1 day', '14 days', '1 mon') and when the next cycle opens.
    recur_interval: str | None = None
    next_run: datetime | None = None
    # The doer the filer named on a task that waits for approval (migration 0012).
    proposed_assignee: str | None = None

    @property
    def stage(self) -> str:
        """The legacy one-word name, whatever the wire shows as ``status``."""
        return self.status
