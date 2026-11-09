"""Unit tests for the between-blocks owner-wake suppression.

The bug this file guards: a workflow that parks the owner between its blocks
as a safety net was sending the owner one wake per block boundary, because
``fire_assignee_notify`` fires on every assignee change. On 2026-10-12 that
was 68 e-mails and 59 Telegrams to the owner between 07:30 and 09:15, each
one saying "waiting for you to review and approve" while nothing was in fact
waiting on him.

The fix: ``planner.notify._owner_wake_suppressed_by_flow`` returns True when
all three hold:
  1. the task carries a ``workflow_id`` (it is flow-managed);
  2. the task is NOT in one of ``OWNER_DECISION_STATUSES`` (so the owner is
     not actually being asked anything);
  3. the new assignee is an owner.

``fire_assignee_notify`` consults it. Non-owner wakes are untouched; the
owner is still woken when the task reaches ``pending_review``,
``awaiting_approval`` or ``blocked``, and when the task is not flow-managed.
"""

from __future__ import annotations

import asyncio

import pytest

from planner import notify as notify_module
from planner.notify import (
    OWNER_DECISION_STATUSES,
    _owner_wake_suppressed_by_flow,
    fire_assignee_notify,
)


OWNERS = frozenset({"admin", "owner", "dev"})


# ---------------------------------------------------------------------------
# _owner_wake_suppressed_by_flow
# ---------------------------------------------------------------------------

def test_decision_statuses_cover_the_three_owner_asks() -> None:
    # The three states that mean the owner is actually being asked something.
    assert OWNER_DECISION_STATUSES == {
        "pending_review", "awaiting_approval", "blocked",
    }


def test_in_progress_ownerflow_is_suppressed() -> None:
    # Between-blocks parking write on a flow-managed in_progress task.
    assert _owner_wake_suppressed_by_flow(
        assignee="admin",
        workflow_id=65,
        task_status="in_progress",
        owner_actors=OWNERS,
    ) is True


def test_pending_review_ownerflow_is_not_suppressed() -> None:
    # The owner's own review queue: wake fires.
    assert _owner_wake_suppressed_by_flow(
        assignee="admin",
        workflow_id=65,
        task_status="pending_review",
        owner_actors=OWNERS,
    ) is False


def test_awaiting_approval_ownerflow_is_not_suppressed() -> None:
    # Owner must still be asked at the approval gate.
    assert _owner_wake_suppressed_by_flow(
        assignee="admin",
        workflow_id=65,
        task_status="awaiting_approval",
        owner_actors=OWNERS,
    ) is False


def test_blocked_ownerflow_is_not_suppressed() -> None:
    # Blocked is a halted flow that needs the owner, as per the owner's rule.
    assert _owner_wake_suppressed_by_flow(
        assignee="admin",
        workflow_id=65,
        task_status="blocked",
        owner_actors=OWNERS,
    ) is False


def test_in_progress_non_flow_is_not_suppressed() -> None:
    # A direct ask from a person outside any flow still wakes the owner.
    assert _owner_wake_suppressed_by_flow(
        assignee="admin",
        workflow_id=None,
        task_status="in_progress",
        owner_actors=OWNERS,
    ) is False


def test_in_progress_ownerflow_specialist_is_not_suppressed() -> None:
    # Only owner wakes are gated. Specialists keep being woken between blocks.
    assert _owner_wake_suppressed_by_flow(
        assignee="writer",
        workflow_id=65,
        task_status="in_progress",
        owner_actors=OWNERS,
    ) is False


def test_empty_assignee_is_not_suppressed() -> None:
    # An unassigned write is not a wake at all.
    assert _owner_wake_suppressed_by_flow(
        assignee=None,
        workflow_id=65,
        task_status="in_progress",
        owner_actors=OWNERS,
    ) is False


# ---------------------------------------------------------------------------
# fire_assignee_notify as the storm-prevention gate
# ---------------------------------------------------------------------------

def _spy_post(monkeypatch) -> list[dict]:
    """Install a spy on ``_post_notify`` and return the captured payloads."""
    captured: list[dict] = []

    async def fake_post(payload):
        captured.append(dict(payload))
        return 200

    monkeypatch.setattr(notify_module, "_post_notify", fake_post)
    monkeypatch.setenv("PLANNER_NOTIFY_URL", "http://x/notify")
    monkeypatch.setenv("SN_OPERATOR_ACTOR", "admin")
    monkeypatch.setenv("SN_SERVICE_AUTH_SECRET", "test-secret")
    return captured


def test_between_blocks_pingpong_wakes_owner_at_most_once(monkeypatch) -> None:
    """RED on the pre-fix build: three admin ping-pongs produce three owner
    wakes. GREEN with the guard: the owner is woken no more than once (zero
    on this trace, since none of the hops carries a decision status)."""
    captured = _spy_post(monkeypatch)

    async def run() -> None:
        # Trace lifted from a real release-announcement run on 2026-10-12:
        # each block closes DONE and parks admin, then the next block
        # launches and reassigns the specialist. Status is in_progress all
        # the way through the chain.
        for specialist in ("writer", "checker", "reviewer", "planner", "writer"):
            fire_assignee_notify(
                assignee="admin",
                task_id=5555,
                title="release-announcement smart-node 1.0.37",
                summary="in_progress",
                workflow_id=65,
                task_status="in_progress",
            )
            fire_assignee_notify(
                assignee=specialist,
                task_id=5555,
                title="release-announcement smart-node 1.0.37",
                summary="in_progress",
                workflow_id=65,
                task_status="in_progress",
            )
        await asyncio.sleep(0)

    asyncio.run(run())

    owner_wakes = [p for p in captured if p["assignee"] == "admin"]
    specialist_wakes = [p for p in captured if p["assignee"] != "admin"]
    # The specialists are still woken on every block; only the admin parking
    # move is gated.
    assert len(specialist_wakes) == 5, (
        f"specialists must still be woken on every block; got {specialist_wakes!r}"
    )
    assert len(owner_wakes) <= 1, (
        f"owner must be woken at most once across a flow's intermediate "
        f"blocks; got {len(owner_wakes)} wakes: {owner_wakes!r}"
    )


def test_owner_still_woken_at_approval_gate(monkeypatch) -> None:
    """The guard must not swallow the real ask: when the task carries a
    decision status (awaiting_approval), the owner wake fires."""
    captured = _spy_post(monkeypatch)

    async def run() -> None:
        fire_assignee_notify(
            assignee="admin",
            task_id=5555,
            title="release-announcement smart-node 1.0.37",
            summary="awaiting_approval",
            workflow_id=65,
            task_status="awaiting_approval",
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(captured) == 1, (
        f"owner must be woken on awaiting_approval; got {captured!r}"
    )
    assert captured[0]["assignee"] == "admin"


def test_owner_still_woken_on_blocked_flow(monkeypatch) -> None:
    """A halted flow lands on the owner. He must hear about it."""
    captured = _spy_post(monkeypatch)

    async def run() -> None:
        fire_assignee_notify(
            assignee="admin",
            task_id=5555,
            title="release-announcement smart-node 1.0.37",
            summary="blocked",
            workflow_id=65,
            task_status="blocked",
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(captured) == 1
    assert captured[0]["assignee"] == "admin"


def test_owner_still_woken_on_pending_review(monkeypatch) -> None:
    captured = _spy_post(monkeypatch)

    async def run() -> None:
        fire_assignee_notify(
            assignee="admin",
            task_id=5555,
            title="release-announcement smart-node 1.0.37",
            summary="pending_review",
            workflow_id=65,
            task_status="pending_review",
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(captured) == 1


def test_non_flow_owner_wake_is_not_gated(monkeypatch) -> None:
    """A direct ask with no workflow_id must wake the owner, every time."""
    captured = _spy_post(monkeypatch)

    async def run() -> None:
        fire_assignee_notify(
            assignee="admin",
            task_id=4242,
            title="please look at this",
            summary="in_progress",
            workflow_id=None,
            task_status="in_progress",
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(captured) == 1


def test_specialist_wake_is_never_gated(monkeypatch) -> None:
    """The guard only touches owner wakes. Everyone else keeps being woken."""
    captured = _spy_post(monkeypatch)

    async def run() -> None:
        fire_assignee_notify(
            assignee="writer",
            task_id=5555,
            title="release-announcement",
            summary="in_progress",
            workflow_id=65,
            task_status="in_progress",
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(captured) == 1
    assert captured[0]["assignee"] == "writer"


def test_legacy_call_without_workflow_still_fires(monkeypatch) -> None:
    """Call sites that have not been threaded yet keep the old behaviour: no
    workflow_id means the guard does not apply."""
    captured = _spy_post(monkeypatch)

    async def run() -> None:
        fire_assignee_notify(
            assignee="admin",
            task_id=4242,
            title="legacy",
            summary="in_progress",
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(captured) == 1
