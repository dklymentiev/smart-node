"""Unit tests for the planner MCP server's pure helpers.

These tests do not need core-api, the planner backend, or httpx. They exercise
the three pure functions that decide what tasks an agent sees on a wake:

- ``_priority_rank``: priority -> sortable integer (critical first).
- ``_primary_sort_key`` / ``_sort_tasks``: priority, eta, due_date, then
  updated_at desc.
- ``_is_executor_only_review``: tasks queued for review where the actor is the
  assignee but not the reviewer wait on someone else, so they must be dropped
  from the actor's working queue.

Also smoke-tests ``_fetch_actor_tasks``, ``planner_my_tasks`` and the new
``planner_next_task`` tool against a stubbed ``_call``: the sort, the limit
and the review filter are what the current contract requires.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


# Make the sibling package importable without installing it.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import mcp_planner as mp  # noqa: E402


# ---------------------------------------------------------------------------
# _priority_rank
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "priority,expected",
    [
        ("critical", 0),
        ("high", 1),
        ("medium", 2),
        ("low", 3),
        ("CRITICAL", 0),  # case-insensitive
        ("urgent", 4),    # unknown -> last
        (None, 4),
        (42, 4),
    ],
)
def test_priority_rank(priority, expected) -> None:
    assert mp._priority_rank(priority) == expected


# ---------------------------------------------------------------------------
# _is_executor_only_review
# ---------------------------------------------------------------------------

def test_executor_only_review_is_filtered() -> None:
    task = {"status": "queued", "wait_reason": "review", "assignee": "agent-a", "reviewer": "admin"}
    assert mp._is_executor_only_review(task, "agent-a") is True


def test_executor_only_review_not_filtered_when_also_reviewer() -> None:
    task = {"status": "queued", "wait_reason": "review", "assignee": "agent-a", "reviewer": "agent-a"}
    assert mp._is_executor_only_review(task, "agent-a") is False


def test_executor_only_review_not_filtered_for_reviewer_only() -> None:
    task = {"status": "queued", "wait_reason": "review", "assignee": "agent-b", "reviewer": "agent-a"}
    assert mp._is_executor_only_review(task, "agent-a") is False


def test_executor_only_review_requires_the_review_wait() -> None:
    task = {"status": "in_progress", "assignee": "agent-a", "reviewer": "admin"}
    assert mp._is_executor_only_review(task, "agent-a") is False


# ---------------------------------------------------------------------------
# _sort_tasks
# ---------------------------------------------------------------------------

def _task(**kw) -> dict:
    base = {
        "id": kw.pop("id", 1),
        "priority": kw.pop("priority", "medium"),
        "eta": kw.pop("eta", None),
        "due_date": kw.pop("due_date", None),
        "updated_at": kw.pop("updated_at", "2026-10-01T00:00:00Z"),
        "status": kw.pop("status", "queued"),
        "wait_reason": kw.pop("wait_reason", None),
        "assignee": kw.pop("assignee", "agent-a"),
        "reviewer": kw.pop("reviewer", None),
    }
    base.update(kw)
    return base


def test_sort_tasks_priority_wins_over_eta() -> None:
    # A medium with a near eta still loses to a high with a far eta.
    high_far = _task(id=1, priority="high", eta="2026-12-31")
    medium_near = _task(id=2, priority="medium", eta="2026-10-03")
    out = mp._sort_tasks([medium_near, high_far])
    assert [t["id"] for t in out] == [1, 2]


def test_sort_tasks_eta_wins_within_same_priority() -> None:
    far = _task(id=1, priority="high", eta="2026-12-31")
    near = _task(id=2, priority="high", eta="2026-10-03")
    out = mp._sort_tasks([far, near])
    assert [t["id"] for t in out] == [2, 1]


def test_sort_tasks_null_eta_sorts_last() -> None:
    no_eta = _task(id=1, priority="high", eta=None)
    with_eta = _task(id=2, priority="high", eta="2027-01-01")
    out = mp._sort_tasks([no_eta, with_eta])
    assert [t["id"] for t in out] == [2, 1]


def test_sort_tasks_due_date_breaks_tie_without_eta() -> None:
    later_due = _task(id=1, priority="high", due_date="2026-11-30")
    earlier_due = _task(id=2, priority="high", due_date="2026-10-10")
    out = mp._sort_tasks([later_due, earlier_due])
    assert [t["id"] for t in out] == [2, 1]


def test_sort_tasks_updated_at_tiebreak_is_descending() -> None:
    older = _task(id=1, priority="high", updated_at="2026-09-01T00:00:00Z")
    newer = _task(id=2, priority="high", updated_at="2026-10-01T00:00:00Z")
    out = mp._sort_tasks([older, newer])
    assert [t["id"] for t in out] == [2, 1]


def test_sort_tasks_three_way_full_order() -> None:
    high_near = _task(id=1, priority="high", eta="2026-10-03")
    high_far = _task(id=2, priority="high", eta="2026-12-01")
    medium_today = _task(id=3, priority="medium", eta="2026-10-02")
    out = mp._sort_tasks([medium_today, high_far, high_near])
    assert [t["id"] for t in out] == [1, 2, 3]


# ---------------------------------------------------------------------------
# planner_my_tasks and planner_next_task with a stubbed transport
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_backend(monkeypatch):
    """Replace mcp_planner._call with a canned task table.

    The stub honours ``status_filter``, ``wait_reason`` and ``offset``/``limit`` so pagination
    logic in ``_fetch_actor_tasks`` is exercised; it does not try to speak the
    full planner protocol.
    """

    def _install(tasks: list[dict], actor: str = "agent-a") -> None:
        monkeypatch.setenv("SN_AGENT_SLUG", actor)

        async def fake_call(method, path, body=None, params=None):  # noqa: ARG001
            assert method == "GET" and path == "/tasks"
            params = params or {}
            rows = [t for t in tasks if t["status"] == params.get("status_filter")]
            if params.get("wait_reason"):
                rows = [t for t in rows if t.get("wait_reason") == params["wait_reason"]]
            limit = int(params.get("limit", 500))
            offset = int(params.get("offset", 0))
            return json.dumps(rows[offset:offset + limit], ensure_ascii=False)

        monkeypatch.setattr(mp, "_call", fake_call)

    return _install


def _run(coro):
    return asyncio.run(coro)


def test_planner_my_tasks_default_excludes_executor_only_review(stub_backend) -> None:
    tasks = [
        _task(id=10, status="queued", wait_reason="review", assignee="agent-a", reviewer="admin"),
        _task(id=11, status="queued", wait_reason="review", assignee="agent-b", reviewer="agent-a"),
        _task(id=12, status="queued", assignee="agent-a", reviewer=None),
    ]
    stub_backend(tasks)
    out = json.loads(_run(mp.planner_my_tasks()))
    ids = sorted(t["id"] for t in out)
    assert ids == [11, 12]


def test_planner_my_tasks_explicit_status_includes_executor_only_review(stub_backend) -> None:
    tasks = [
        _task(id=10, status="queued", wait_reason="review", assignee="agent-a", reviewer="admin"),
        _task(id=11, status="queued", wait_reason="review", assignee="agent-b", reviewer="agent-a"),
    ]
    stub_backend(tasks)
    out = json.loads(_run(mp.planner_my_tasks(status="queued", wait_reason="review")))
    ids = sorted(t["id"] for t in out)
    assert ids == [10, 11]


def test_planner_next_task_returns_one_by_default(stub_backend) -> None:
    tasks = [
        _task(id=1, status="queued", priority="high", eta="2026-11-01"),
        _task(id=2, status="queued", priority="high", eta="2026-10-05"),
        _task(id=3, status="queued", priority="medium", eta="2026-10-02"),
    ]
    stub_backend(tasks)
    out = json.loads(_run(mp.planner_next_task()))
    assert [t["id"] for t in out] == [2]


def test_planner_next_task_respects_limit(stub_backend) -> None:
    tasks = [
        _task(id=1, status="queued", priority="high", eta="2026-11-01"),
        _task(id=2, status="queued", priority="high", eta="2026-10-05"),
        _task(id=3, status="queued", priority="medium", eta="2026-10-02"),
    ]
    stub_backend(tasks)
    out = json.loads(_run(mp.planner_next_task(limit=10)))
    assert [t["id"] for t in out] == [2, 1, 3]


def test_planner_next_task_skips_executor_only_review(stub_backend) -> None:
    tasks = [
        _task(id=1, status="queued", wait_reason="review", priority="critical",
              assignee="agent-a", reviewer="admin"),
        _task(id=2, status="queued", priority="low", assignee="agent-a"),
    ]
    stub_backend(tasks)
    out = json.loads(_run(mp.planner_next_task(limit=10)))
    # The critical review belongs to admin, so the actor sees only the new
    # task.
    assert [t["id"] for t in out] == [2]


def test_planner_next_task_includes_reviewer_role(stub_backend) -> None:
    tasks = [
        _task(id=1, status="queued", wait_reason="review", priority="high",
              assignee="agent-b", reviewer="agent-a"),
        _task(id=2, status="queued", priority="low", assignee="agent-a"),
    ]
    stub_backend(tasks)
    out = json.loads(_run(mp.planner_next_task(limit=10)))
    # Review-waiting-on-me is actionable and outranks a low-priority new task.
    assert [t["id"] for t in out] == [1, 2]


# ---------------------------------------------------------------------------
# planner_list: one status across every assignee and reviewer
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_listing(monkeypatch):
    """A canned task table that honours status_filter, wait_reason, project_id and paging."""

    def _install(tasks: list[dict]) -> None:
        monkeypatch.setenv("SN_AGENT_SLUG", "agent-a")

        async def fake_call(method, path, body=None, params=None):  # noqa: ARG001
            assert method == "GET" and path == "/tasks"
            params = params or {}
            rows = [t for t in tasks if t["status"] == params.get("status_filter")]
            if params.get("wait_reason"):
                rows = [t for t in rows if t.get("wait_reason") == params["wait_reason"]]
            if "project_id" in params:
                rows = [t for t in rows if t.get("project_id") == params["project_id"]]
            limit = int(params.get("limit", 500))
            offset = int(params.get("offset", 0))
            return json.dumps(rows[offset:offset + limit], ensure_ascii=False)

        monkeypatch.setattr(mp, "_call", fake_call)

    return _install


def test_planner_list_shows_tasks_the_caller_holds_no_role_in(stub_listing) -> None:
    # The gap this tool closes: planner_my_tasks returns none of these three.
    tasks = [
        _task(id=1, status="queued", wait_reason="review", assignee="agent-b", reviewer="admin"),
        _task(id=2, status="queued", wait_reason="review", assignee=None, reviewer=None),
        _task(id=3, status="queued", wait_reason="review", assignee="agent-c", reviewer="agent-b"),
        _task(id=4, status="queued", assignee="agent-b"),
    ]
    stub_listing(tasks)
    assert json.loads(_run(mp.planner_my_tasks(status="queued", wait_reason="review"))) == []
    out = json.loads(_run(mp.planner_list(status="queued", wait_reason="review")))
    assert out["total"] == 3 and out["returned"] == 3
    assert sorted(t["id"] for t in out["tasks"]) == [1, 2, 3]


def test_planner_list_rows_are_compact(stub_listing) -> None:
    stub_listing([_task(id=1, status="queued", title="t", description="x" * 5000)])
    out = json.loads(_run(mp.planner_list(status="queued")))
    assert set(out["tasks"][0]) == set(mp._LIST_FIELDS)
    assert "description" not in out["tasks"][0]


def test_planner_list_filters_by_project_and_role(stub_listing) -> None:
    tasks = [
        _task(id=1, status="queued", project_id=79, assignee="agent-b", reviewer="admin"),
        _task(id=2, status="queued", project_id=79, assignee=None, reviewer=None),
        _task(id=3, status="queued", project_id=24, assignee="agent-b", reviewer=None),
    ]
    stub_listing(tasks)

    def ids(**kw):
        return sorted(t["id"] for t in
                      json.loads(_run(mp.planner_list(status="queued", **kw)))["tasks"])

    assert ids(project_id=79) == [1, 2]
    assert ids(assignee="agent-b") == [1, 3]
    assert ids(assignee="none") == [2]
    assert ids(reviewer="admin") == [1]
    assert ids(reviewer="none") == [2, 3]
    assert ids(project_id=79, assignee="agent-b") == [1]


def test_planner_list_total_counts_past_the_page(stub_listing, monkeypatch) -> None:
    # 7 tasks read 3 at a time: the total is still 7, and the caller's own
    # limit and offset cut the answer, not the count.
    monkeypatch.setattr(mp, "_PAGE_SIZE", 3)
    tasks = [_task(id=i, status="queued", updated_at=f"2026-10-0{i}T00:00:00Z")
             for i in range(1, 8)]
    stub_listing(tasks)
    first = json.loads(_run(mp.planner_list(status="queued", limit=2)))
    second = json.loads(_run(mp.planner_list(status="queued", limit=2, offset=2)))
    assert first["total"] == second["total"] == 7
    # Same priority, so the most recently touched come first.
    assert [t["id"] for t in first["tasks"]] == [7, 6]
    assert [t["id"] for t in second["tasks"]] == [5, 4]
    assert second["offset"] == 2 and second["returned"] == 2


def test_planner_list_limit_is_capped(stub_listing) -> None:
    stub_listing([_task(id=i, status="queued") for i in range(1, 301)])
    out = json.loads(_run(mp.planner_list(status="queued", limit=5000)))
    assert out["total"] == 300
    assert out["returned"] == mp._LIST_MAX_LIMIT


def test_planner_list_passes_an_error_through_not_an_empty_list(monkeypatch) -> None:
    async def failing(method, path, body=None, params=None):  # noqa: ARG001
        return json.dumps({"error": "HTTP 403", "detail": "refused"})

    monkeypatch.setattr(mp, "_call", failing)
    out = json.loads(_run(mp.planner_list(status="queued")))
    assert out == {"error": "HTTP 403", "detail": "refused"}


# ---------------------------------------------------------------------------
# The three statuses: queue order, what waits on someone else, the move body
# ---------------------------------------------------------------------------

def test_planner_my_tasks_orders_started_then_review_then_new(stub_backend) -> None:
    tasks = [
        _task(id=1, status="queued", assignee="agent-a"),
        _task(id=2, status="queued", wait_reason="review", assignee="agent-b", reviewer="agent-a"),
        _task(id=3, status="in_progress", assignee="agent-a"),
    ]
    stub_backend(tasks)
    out = json.loads(_run(mp.planner_my_tasks()))
    assert [t["id"] for t in out] == [3, 2, 1]


def test_planner_my_tasks_leaves_out_tasks_that_wait_on_someone_else(stub_backend) -> None:
    # Queued for approval, for a person or for a date: not the agent's move.
    tasks = [
        _task(id=1, status="queued", wait_reason="approval", assignee="agent-a"),
        _task(id=2, status="queued", wait_reason="person", assignee="agent-a"),
        _task(id=3, status="queued", wait_reason="schedule", assignee="agent-a"),
        _task(id=4, status="closed", close_outcome="completed", assignee="agent-a"),
        _task(id=5, status="queued", assignee="agent-a"),
    ]
    stub_backend(tasks)
    assert [t["id"] for t in json.loads(_run(mp.planner_my_tasks()))] == [5]
    assert [t["id"] for t in json.loads(_run(mp.planner_next_task(limit=10)))] == [5]
    # ... and an explicit ask still finds them
    out = json.loads(_run(mp.planner_my_tasks(status="queued", wait_reason="approval")))
    assert [t["id"] for t in out] == [1]


def test_a_reviewer_is_not_handed_work_in_flight_that_is_not_theirs(stub_backend) -> None:
    """An in_progress task with no assignee, or someone else's, is not a reviewer's next task.

    An agent that was reviewer of a stalled, unassigned task was handed it as its next
    work and went off to carry it out. The reviewer's queue holds reviews waiting on them."""
    tasks = [
        _task(id=1, status="in_progress", assignee=None, reviewer="agent-a"),
        _task(id=2, status="in_progress", assignee="agent-b", reviewer="agent-a"),
        _task(id=3, status="queued", assignee=None, reviewer="agent-a"),
        _task(id=4, status="in_progress", assignee="agent-a", reviewer="agent-b"),
        _task(id=5, status="queued", wait_reason="review", assignee="agent-b", reviewer="agent-a"),
    ]
    stub_backend(tasks)
    # what to START next: only the assignee's work in hand and new work, and the reviews on you
    assert [t["id"] for t in json.loads(_run(mp.planner_next_task(limit=10)))] == [4, 5]
    # what you hold in either role is still listed
    assert {t["id"] for t in json.loads(_run(mp.planner_my_tasks()))} == {1, 2, 3, 4, 5}


def test_planner_advance_sends_reason_and_outcome(monkeypatch) -> None:
    sent: list = []

    async def fake_call(method, path, body=None, params=None):  # noqa: ARG001
        sent.append((method, path, body))
        return "{}"

    monkeypatch.setattr(mp, "_call", fake_call)
    _run(mp.planner_advance(7, "queued", wait_reason="person", comment="needs the owner"))
    _run(mp.planner_advance(7, "closed", close_outcome="completed", comment="report"))
    _run(mp.planner_advance(7, "in_progress"))
    assert sent == [
        ("PATCH", "/tasks/7", {"status": "queued", "wait_reason": "person", "comment": "needs the owner"}),
        ("PATCH", "/tasks/7", {"status": "closed", "close_outcome": "completed", "comment": "report"}),
        ("PATCH", "/tasks/7", {"status": "in_progress"}),
    ]


def test_every_call_asks_for_the_three_statuses(monkeypatch) -> None:
    seen: list = []

    class _Resp:
        status_code = 200
        content = b"[]"
        text = "[]"

        def json(self):
            return []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, headers=None, params=None, json=None):  # noqa: A002
            seen.append(params)
            return _Resp()

    monkeypatch.setattr(mp, "CORE_API_SOCKET", "/nonexistent")
    monkeypatch.setattr(mp, "CORE_API_URL", "http://test")
    monkeypatch.setattr(mp.httpx, "AsyncClient", _Client)
    _run(mp._call("GET", "/tasks", params={"status_filter": "queued"}))
    _run(mp._call("PATCH", "/tasks/1", {"status": "in_progress"}))
    assert seen == [{"status_filter": "queued", "vocabulary": "new"}, {"vocabulary": "new"}]


def test_objective_get_counts_in_the_three_statuses(monkeypatch) -> None:
    async def fake_call(method, path, body=None, params=None):  # noqa: ARG001
        if path == "/objectives/5":
            return json.dumps({"id": 5, "title": "o", "status": "active"})
        if path == "/objectives/5/progress":
            return json.dumps({"total_tasks": 3,
                               "tasks_by_status": {"done": 1, "open": 1, "awaiting_approval": 1},
                               "tasks_by_state": {"closed": 1, "queued": 2},
                               "queued_by_reason": {"none": 1, "approval": 1},
                               "closed_by_outcome": {"completed": 1}})
        return json.dumps([])

    monkeypatch.setattr(mp, "_call", fake_call)
    out = json.loads(_run(mp.planner_objective_get(5)))
    assert out["progress"] == {"total": 3, "completed": 1,
                               "by_status": {"closed": 1, "queued": 2},
                               "queued_by_reason": {"none": 1, "approval": 1},
                               "closed_by_outcome": {"completed": 1}}


def test_objective_close_counts_only_closed_tasks_as_finished(monkeypatch) -> None:
    async def fake_call(method, path, body=None, params=None):  # noqa: ARG001
        if path == "/objectives/5" and method == "GET":
            return json.dumps({"id": 5, "title": "o"})
        if path == "/tasks":
            return json.dumps([
                {"id": 1, "status": "closed", "close_outcome": "cancelled"},
                {"id": 2, "status": "queued", "wait_reason": "person"},
            ])
        raise AssertionError("must not close with an unfinished task")

    monkeypatch.setattr(mp, "_call", fake_call)
    out = json.loads(_run(mp.planner_objective_close(5)))
    assert [t["id"] for t in out["unfinished_tasks"]] == [2]


def test_planner_file_makes_a_recurring_task_and_planner_recur_changes_it(monkeypatch) -> None:
    """A recurring task is filed with its interval and first run, changed and ended by the
    same two fields; a task filed without them carries neither."""
    sent: list = []

    async def fake_call(method, path, body=None):
        sent.append((method, path, body))
        return json.dumps({"id": 1})

    monkeypatch.setattr(mp, "_call", fake_call)
    asyncio.run(mp.planner_file("t", "d", recur_interval="weekly", next_run="2026-10-26T07:00:00-05:00"))
    assert sent[-1][2]["recur_interval"] == "weekly"
    assert sent[-1][2]["next_run"] == "2026-10-26T07:00:00-05:00"
    asyncio.run(mp.planner_file("t", "d"))
    assert "recur_interval" not in sent[-1][2] and "next_run" not in sent[-1][2]
    asyncio.run(mp.planner_recur(7, recur_interval="2 weeks"))
    asyncio.run(mp.planner_recur(7, next_run="2026-11-01T07:00:00-05:00"))
    asyncio.run(mp.planner_recur(7, recur_interval="off"))
    assert sent[-3:] == [("PATCH", "/tasks/7", {"recur_interval": "2 weeks"}),
                         ("PATCH", "/tasks/7", {"next_run": "2026-11-01T07:00:00-05:00"}),
                         ("PATCH", "/tasks/7", {"recur_interval": "off"})]
    before = len(sent)
    assert "error" in json.loads(asyncio.run(mp.planner_recur(7)))
    assert len(sent) == before                  # nothing to change, nothing sent


def test_the_tools_describe_recurrence_themselves() -> None:
    """An agent learns this from the tool, not from its instructions."""
    for tool in (mp.planner_file, mp.planner_recur):
        doc = tool.__doc__ or ""
        assert "recur_interval" in doc and "next_run" in doc
    assert "reminder" in mp.planner_recur.__doc__ and '"off"' in mp.planner_recur.__doc__


def test_planner_file_binds_the_task_to_a_flow(monkeypatch) -> None:
    """A task filed with a workflow_id carries it, one filed without does not.

    The Runner starts a flow for a task that carries a workflow_id; without this an
    agent could file the task but never bind it, and could not start a flow at all.
    """
    sent: dict = {}

    async def fake_call(method, path, body=None):
        sent.update(method=method, path=path, body=body)
        return json.dumps({"id": 1})

    monkeypatch.setattr(mp, "_call", fake_call)
    asyncio.run(mp.planner_file("t", "d", workflow_id=64))
    assert sent["method"] == "POST" and sent["path"] == "/tasks"
    assert sent["body"]["workflow_id"] == 64
    sent.clear()
    asyncio.run(mp.planner_file("t", "d"))
    assert "workflow_id" not in sent["body"]
