"""Integration tests for the stale in_progress watchdog (planner.notify).

Why these exist: an ordinary task could sit in_progress under an agent for good,
because agents are woken by events and a turn that dies is not one. The watchdog
is the clock for that case, and a clock that wakes agents is only safe while it
is bounded. So these assert the bounds as hard as the reminder: what it must
leave alone, that it stops, and that it counts only reminders somebody received.

core-api is a stub here. It answers ``woke`` the way the real one does: the slug
for an agent, null for anybody else.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import pytest_asyncio

from planner import notify

pytestmark = pytest.mark.asyncio

AGENTS = {"agent-a", "agent-b", "agent-c"}
R, E = notify.STALE_REMINDER_TYPE, notify.STALE_ESCALATED_TYPE


@pytest.fixture
def core_api(monkeypatch):
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            woke = body["assignee"] if body["assignee"] in AGENTS else None
            out = json.dumps({"sent": [], "woke": woke}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(notify, "NOTIFY_URL", f"http://127.0.0.1:{server.server_port}/api/notify")
    monkeypatch.setattr(notify, "NOTIFY_REMOTE_USER", "stub")
    monkeypatch.setattr(notify, "NOTIFY_SERVICE_SECRET", "stub")
    yield calls
    server.shutdown()


@pytest_asyncio.fixture
async def make_task(pool):
    for actor in ("system", "agent-a", "agent-b", "agent-c", "admin", "person@example.com"):
        await pool.execute(
            "INSERT INTO actors (id, display_name, type) VALUES ($1, $1, 'agent') "
            "ON CONFLICT (id) DO NOTHING", actor)

    async def _make(title, *, assignee="agent-a", requester="agent-b", idle_hours=2.0,
                    workflow_id=None, tag=None, comments=()):
        tid = await pool.fetchval(
            "INSERT INTO tasks (title, assignee, requester, workflow_id) "
            "VALUES ($1, $2, $3, $4) RETURNING id", title, assignee, requester, workflow_id)
        await pool.execute(
            "UPDATE tasks SET status = 'in_progress', "
            "updated_at = now() - make_interval(secs => $2) WHERE id = $1",
            tid, idle_hours * 3600.0)
        if tag:
            await pool.execute("INSERT INTO task_tags (task_id, tag) VALUES ($1, $2)", tid, tag)
        for comment_type, hours_ago in comments:
            await pool.execute(
                "INSERT INTO task_comments (task_id, author, content, comment_type, created_at) "
                "VALUES ($1, 'system', 'fixture', $2, now() - make_interval(secs => $3))",
                tid, comment_type, hours_ago * 3600.0)
        return tid

    return _make


async def _written(pool, comment_type):
    rows = await pool.fetch(
        "SELECT task_id FROM task_comments WHERE comment_type = $1 AND content <> 'fixture' "
        "ORDER BY task_id", comment_type)
    return [r["task_id"] for r in rows]


async def test_idle_agent_task_is_reminded_and_its_agent_woken(pool, core_api, make_task):
    tid = await make_task("idle")

    done = await notify.stale_in_progress_pass(pool)

    assert done["reminded"] == [tid]
    assert await _written(pool, R) == [tid]
    assert [(c["task_id"], c["assignee"], c["event"]) for c in core_api] == [
        (tid, "agent-a", "stale_in_progress")]
    text = await pool.fetchval(
        "SELECT content FROM task_comments WHERE task_id = $1 AND comment_type = $2", tid, R)
    assert text.startswith("Reminder 1 of 3") and "agent-a" in text
    # The reminder also says what to do when the agent is waiting on somebody.
    assert "assign the task to them" in text


@pytest.mark.parametrize("kwargs", [
    {"idle_hours": 0.2},                       # fresh
    {"workflow_id": 7},                        # a workflow drives it
    {"tag": "blocked:approval"},               # held on purpose
    {"idle_hours": 30},                        # backlog, older than the window
    {"idle_hours": 5, "comments": [(None, 0.2)]},   # a comment is activity
    {"idle_hours": 5, "comments": [(R, 0.2)]},      # reminded minutes ago
    {"idle_hours": 6, "comments": [(R, 4), (R, 3), (R, 2), (E, 1.5)]},  # already escalated
])
async def test_tasks_the_watchdog_must_leave_alone(pool, core_api, make_task, kwargs):
    await make_task("untouched", **kwargs)

    done = await notify.stale_in_progress_pass(pool)

    assert done["reminded"] == [] and done["escalated"] == []
    assert core_api == []
    assert await _written(pool, R) == [] and await _written(pool, E) == []


async def test_a_person_by_name_is_asked_about_but_never_counted(pool, core_api, make_task):
    tid = await make_task("person", assignee="admin")

    done = await notify.stale_in_progress_pass(pool)

    # core-api said nobody was woken, so the reminder was rolled back.
    assert done["reminded"] == []
    assert [c["assignee"] for c in core_api] == ["admin"]
    assert await _written(pool, R) == []
    assert tid not in done["escalated"]


async def test_a_reminder_only_ever_asks_core_api_to_wake(pool, core_api, make_task):
    # core-api walks the person path on a plain notify: a person who holds an
    # idle task would get mail and a Telegram message on every scan, because a
    # reminder nobody was woken by is rolled back and is due again next pass.
    # The escalation is said once, so it stays an ordinary notice.
    await make_task("held by a person", assignee="admin")
    await make_task("reminded", assignee="agent-a")
    await make_task("escalated", assignee="agent-c", requester="admin", comments=[(R, 1.5)] * 3)

    await notify.stale_in_progress_pass(pool)

    assert sorted((c["event"], c.get("wake_only")) for c in core_api) == [
        ("stale_in_progress", True), ("stale_in_progress", True),
        ("stale_in_progress_escalated", None)]


async def test_a_person_by_email_is_not_sent_at_all(pool, core_api, make_task):
    # core-api mails a person on any notify, worded as a new assignment.
    await make_task("mailbox", assignee="person@example.com")

    await notify.stale_in_progress_pass(pool)

    assert core_api == []


async def test_after_the_last_reminder_it_escalates_once_and_stops(pool, core_api, make_task):
    tid = await make_task("stuck", idle_hours=6, comments=[(R, 4), (R, 3), (R, 2)])

    first = await notify.stale_in_progress_pass(pool)
    second = await notify.stale_in_progress_pass(pool)

    assert first["escalated"] == [tid] and first["reminded"] == []
    assert second["escalated"] == [] and second["reminded"] == []
    assert await _written(pool, E) == [tid]
    assert [(c["assignee"], c["event"]) for c in core_api] == [
        ("agent-b", "stale_in_progress_escalated")]


async def test_escalation_to_a_person_is_a_comment_and_no_wake(pool, core_api, make_task):
    tid = await make_task("stuck", requester="admin", idle_hours=6,
                          comments=[(R, 4), (R, 3), (R, 2)])

    done = await notify.stale_in_progress_pass(pool)

    assert done["escalated"] == [tid]
    assert await _written(pool, E) == [tid]
    assert [c["assignee"] for c in core_api] == ["admin"]


async def test_activity_starts_the_count_over(pool, core_api, make_task):
    # Three reminders, then work two hours ago: the old reminders no longer count.
    tid = await make_task("resumed", assignee="agent-c", idle_hours=2,
                          comments=[(R, 5), (R, 4), (R, 3)])

    done = await notify.stale_in_progress_pass(pool)

    assert done["reminded"] == [tid] and done["escalated"] == []
    text = await pool.fetchval(
        "SELECT content FROM task_comments WHERE task_id = $1 AND comment_type = $2 "
        "AND content <> 'fixture'", tid, R)
    assert text.startswith("Reminder 1 of 3")


async def test_a_second_pass_does_not_repeat_a_reminder(pool, core_api, make_task):
    tid = await make_task("idle")

    await notify.stale_in_progress_pass(pool)
    again = await notify.stale_in_progress_pass(pool)

    assert again["reminded"] == []
    assert await _written(pool, R) == [tid]
    assert len(core_api) == 1
