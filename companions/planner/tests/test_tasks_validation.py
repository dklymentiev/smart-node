"""Pure validation tests for TasksRepo input."""

from __future__ import annotations

import pytest

from planner.repos.tasks import (
    TaskError,
    _check_blocked_reason,
    _check_complexity,
    _check_priority,
    _check_status,
)


def test_priority_valid() -> None:
    for p in ("low", "medium", "high", "critical"):
        _check_priority(p)


@pytest.mark.parametrize("p", ["", "urgent", "MEDIUM", "p1"])
def test_priority_invalid(p: str) -> None:
    with pytest.raises(TaskError):
        _check_priority(p)  # type: ignore[arg-type]


def test_status_valid() -> None:
    for s in (
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
    ):
        _check_status(s)


@pytest.mark.parametrize("s", ["", "Done", "wip", "finished", "in-progress"])
def test_status_invalid(s: str) -> None:
    with pytest.raises(TaskError):
        _check_status(s)  # type: ignore[arg-type]


def test_complexity_none_ok() -> None:
    _check_complexity(None)


def test_complexity_valid() -> None:
    for c in ("simple", "moderate", "complex", "unknown"):
        _check_complexity(c)


@pytest.mark.parametrize("c", ["easy", "Hard", "trivial", ""])
def test_complexity_invalid(c: str) -> None:
    with pytest.raises(TaskError):
        _check_complexity(c)  # type: ignore[arg-type]


def test_blocked_reason_none_ok() -> None:
    _check_blocked_reason(None)


def test_blocked_reason_valid() -> None:
    for r in ("task_dependency", "external_approval", "scheduled_time"):
        _check_blocked_reason(r)


@pytest.mark.parametrize("r", ["other", "blocker", ""])
def test_blocked_reason_invalid(r: str) -> None:
    with pytest.raises(TaskError):
        _check_blocked_reason(r)  # type: ignore[arg-type]
