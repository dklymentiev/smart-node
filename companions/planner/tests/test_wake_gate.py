"""Unit tests for the wake-suppression gate and reviewer-change notify path.

The gate (``planner.pipeline.should_suppress_wake``) decides whether a
reassignment or status transition should fall through to the agent without an
owner's approval, or wait for one. Before the predicate this file guards it
was an unlabelled inline check that caught two legitimate paths it was never
meant to catch: the runner proxying for the owner, and a triaging colleague
reassigning another agent. The explicit predicate names the four conditions
and the status clause that was missing.

The reviewer-notify path (``planner.notify.fire_reviewer_notify``) is a
symmetrical twin of ``fire_assignee_notify``; the test only pins its fire
semantics so a future refactor cannot silently remove the wake again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


# conftest.py pins ``planner`` to this companion's src/ so the next line
# resolves to the file being changed in this MR.
from planner.pipeline import (  # noqa: E402
    PRE_APPROVAL_STATUSES,
    should_suppress_wake,
)
from planner import notify as notify_module  # noqa: E402


OWNERS = frozenset({"admin", "dev"})


# ---------------------------------------------------------------------------
# should_suppress_wake
# ---------------------------------------------------------------------------

def test_pre_approval_statuses_cover_the_two_gated_states() -> None:
    # Guards against someone adding/removing a status without thinking about
    # the wake gate: open and awaiting_approval are the ones that gate.
    assert PRE_APPROVAL_STATUSES == {"awaiting_approval", "open"}


def test_in_progress_reassignment_wakes_even_when_both_are_agents() -> None:
    # Case F1: runner POSTs a task with assignee=X status=in_progress. The
    # task is already in the pipeline, so this is a handoff, not a filing.
    assert (
        should_suppress_wake(
            actor="runner",
            requester="runner",
            approved_at=None,
            task_status="in_progress",
            owner_actors=OWNERS,
        )
        is False
    )


def test_pending_review_reviewer_change_wakes() -> None:
    # Case G1: PATCH reviewer=<other> on a pending_review task; the gate must
    # not suppress even though neither actor nor requester is an owner.
    assert (
        should_suppress_wake(
            actor="agent-a",
            requester="agent-a",
            approved_at=None,
            task_status="pending_review",
            owner_actors=OWNERS,
        )
        is False
    )


def test_assessing_handoff_wakes() -> None:
    # Case F2 relative: a triaging colleague reassigns another agent on an
    # open task that then went through assessing. Approved_at may still be
    # null in some triage paths; status rules out suppression.
    assert (
        should_suppress_wake(
            actor="agent-a",
            requester="agent-a",
            approved_at=None,
            task_status="assessing",
            owner_actors=OWNERS,
        )
        is False
    )


def test_awaiting_approval_agent_handoff_is_suppressed() -> None:
    # Case F3: runner POSTs an awaiting_approval task; owner has not approved
    # yet so the agent must not be woken (handoff defence stays).
    assert (
        should_suppress_wake(
            actor="runner",
            requester="runner",
            approved_at=None,
            task_status="awaiting_approval",
            owner_actors=OWNERS,
        )
        is True
    )


def test_open_task_agent_filing_is_suppressed() -> None:
    # An agent-filed open task with no owner interaction yet; the gate holds.
    assert (
        should_suppress_wake(
            actor="agent-a",
            requester="agent-a",
            approved_at=None,
            task_status="open",
            owner_actors=OWNERS,
        )
        is True
    )


def test_owner_actor_always_wakes() -> None:
    assert (
        should_suppress_wake(
            actor="admin",
            requester="agent-a",
            approved_at=None,
            task_status="open",
            owner_actors=OWNERS,
        )
        is False
    )


def test_owner_requester_always_wakes() -> None:
    assert (
        should_suppress_wake(
            actor="agent-a",
            requester="admin",
            approved_at=None,
            task_status="open",
            owner_actors=OWNERS,
        )
        is False
    )


def test_already_approved_task_wakes() -> None:
    # Once approved_at is set, the task has been admitted to the pipeline and
    # the gate must not suppress even if we are still technically in open.
    assert (
        should_suppress_wake(
            actor="agent-a",
            requester="agent-a",
            approved_at="2026-10-01T00:00:00Z",
            task_status="open",
            owner_actors=OWNERS,
        )
        is False
    )


def test_null_actor_does_not_suppress() -> None:
    # A system-level write with no actor is not an agent-to-agent handoff.
    assert (
        should_suppress_wake(
            actor=None,
            requester="agent-a",
            approved_at=None,
            task_status="open",
            owner_actors=OWNERS,
        )
        is False
    )


# ---------------------------------------------------------------------------
# fire_reviewer_notify
# ---------------------------------------------------------------------------

def test_fire_reviewer_notify_posts_with_reviewer_as_assignee(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post(payload):
        captured.update(payload)
        return 200

    monkeypatch.setattr(notify_module, "_post_notify", fake_post)
    monkeypatch.setenv("PLANNER_NOTIFY_URL", "http://x/notify")

    async def runner():
        notify_module.fire_reviewer_notify(
            reviewer="agent-b", task_id=4242,
            title="wake mechanics", summary="pending_review",
        )
        # Let the detached task post.
        await asyncio.sleep(0)

    asyncio.run(runner())
    assert captured["assignee"] == "agent-b"
    assert captured["task_id"] == 4242
    assert captured["event"] == "reviewer_assigned"
    assert captured["title"] == "wake mechanics"
    assert captured["summary"] == "pending_review"


def test_fire_reviewer_notify_noops_on_empty_reviewer(monkeypatch) -> None:
    called = {"n": 0}

    async def fake_post(payload):
        called["n"] += 1
        return 200

    monkeypatch.setattr(notify_module, "_post_notify", fake_post)
    notify_module.fire_reviewer_notify(
        reviewer=None, task_id=1, title="x", summary="y",
    )
    notify_module.fire_reviewer_notify(
        reviewer="", task_id=1, title="x", summary="y",
    )
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# stale-threshold default
# ---------------------------------------------------------------------------

def test_stale_threshold_default_is_two_hours() -> None:
    # Guard against the default drifting back to 8h: the current contract is
    # 2h on the owner's instruction. A deployment can still override via the
    # env. The constant is read at import time, so a changed env in the
    # running process does not retroactively change it; this test reads the
    # SOURCE.
    src = Path(notify_module.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("PLANNER_STALE_THRESHOLD_HOURS", "2")' in src, (
        "default stale threshold must stay at 2 hours per the current "
        "contract; found a different literal in planner/notify.py"
    )
