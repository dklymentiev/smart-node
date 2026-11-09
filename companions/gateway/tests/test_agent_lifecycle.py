"""The base of an agent's life on the node: a process starts, stops, starts
again, and every turn ends with an outcome the caller can read. These are the
first-level acceptance checks; each test names the criterion it holds.

In-process state only; the kill, the spawn and the database are replaced. The
end-to-end version of the same checks (real Claude Code processes against a
deterministic Anthropic-compatible mock, many agents, stops mid-turn) runs on a
stand; these pin the behaviour that run found."""
import asyncio
import os
import time

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402

import backends.cli_claude as c  # noqa: E402
import sessions_v2_routes as routes  # noqa: E402
from backends import BackendResponse  # noqa: E402


class Proc:
    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode


def _reset():
    for d in (c._PROCESSES, c._SESSION_CONVERSATION, c._SESSION_AGENT, c._TURN_STARTED,
              c._TURN_STATUS, c._RUNAS, c._SESSION_RUNTIME, c._SESSION_TYPES, c._LAST_USED,
              c._SPAWNED_AT, c._INTERRUPTS, c._SEND_LOCKS):
        d.clear()


SESS = {"status": "active", "model": "claude-cli/sonnet", "agent_id": 7, "tenant_id": 1, "metadata": {}}


class Backend:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def send(self, session_id, history, content):
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def store(monkeypatch):
    """session_service and budget, recorded instead of written."""
    seen = {"status": [], "messages": []}
    monkeypatch.setattr(routes.session_service, "set_status",
                        lambda sid, status, error=None: seen["status"].append((status, error)))
    monkeypatch.setattr(routes.session_service, "append_message",
                        lambda sid, **kw: seen["messages"].append(kw) or 42)
    monkeypatch.setattr(routes.session_service, "record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(routes.budget, "record_spend", lambda **kw: [])
    monkeypatch.setattr(routes.budget, "check_budget", lambda **kw: (True, None, []))
    return seen


# -- Criterion: stop keeps what the next start needs -------------------------

def test_stop_forgets_the_process_but_keeps_the_conversation_for_the_next_start(monkeypatch):
    _reset()
    c._PROCESSES["s1"] = Proc(101)
    c._SESSION_CONVERSATION["s1"] = "agent-b:runner-agent-b-1-1"
    c._RUNAS["s1"] = "agent-b"
    c._SESSION_TYPES["s1"] = "background"

    async def fake_kill(sid, proc, reason, os_user=None):
        proc.returncode = -15

    monkeypatch.setattr(c, "_kill_tree", fake_kill)
    out = asyncio.run(c.stop_agent("agent-b", "user:owner", "test"))
    assert out["closed"] == ["s1"]
    assert "s1" not in c._PROCESSES
    # The conversation stays: the next message respawns the same session with
    # --resume once the agent is enabled again.
    assert c._SESSION_CONVERSATION["s1"] == "agent-b:runner-agent-b-1-1"


# -- Criterion: a restart after stop does not lose the turn ------------------

def test_the_idle_sweep_leaves_a_dead_process_to_the_send_that_holds_its_lock():
    _reset()
    c._PROCESSES["s1"] = Proc(101, returncode=1)       # respawned and exited at once
    c._SESSION_CONVERSATION["s1"] = "agent-b:runner-agent-b-1-1"
    lock = asyncio.Lock()
    c._SEND_LOCKS["s1"] = lock
    c._LAST_USED["s1"] = time.monotonic()

    async def run():
        async with lock:                              # the send in progress
            await c._idle_sweep_once()
            return "s1" in c._PROCESSES and c._SEND_LOCKS.get("s1") is lock

    assert asyncio.run(run())


def test_the_idle_sweep_still_forgets_a_dead_process_nobody_is_sending_to():
    _reset()
    c._PROCESSES["s1"] = Proc(101, returncode=1)
    c._SESSION_CONVERSATION["s1"] = "agent-b:runner-agent-b-1-1"
    asyncio.run(c._idle_sweep_once())
    assert "s1" not in c._PROCESSES
    assert "s1" in c._SESSION_CONVERSATION


# -- Criterion: every turn ends with an outcome the caller can read ----------

def test_a_stopped_turn_answers_stopped_with_who_stopped_it(store):
    resp = BackendResponse(content="partial words", stopped=True, stopped_by="user:owner")
    out = asyncio.run(routes._complete_turn_async("s1", SESS, Backend(resp), [], "hi", 41))
    assert out["stopped"] is True and out["stopped_by"] == "user:owner"
    assert out["response"]["final_answer"] == ""         # a stopped turn has no answer
    assert store["messages"][-1]["is_partial"] is True   # and its row says so
    assert store["status"] == []                         # the session is not failed


def test_a_finished_turn_answers_not_stopped(store):
    resp = BackendResponse(content="ok 1234", final_answer="ok 1234")
    out = asyncio.run(routes._complete_turn_async("s1", SESS, Backend(resp), [], "hi", 41))
    assert out["stopped"] is False and out["response"]["final_answer"] == "ok 1234"
    assert store["status"] == []


# -- Criterion: the process cap refuses a turn, not the session --------------

CAP = c.SpawnCapReached("agent spawn cap reached (5/6 live, 5/5 background); "
                   "refusing new background session s1")


def test_a_spawn_cap_refusal_leaves_the_session_usable(store):
    with pytest.raises(RuntimeError):
        asyncio.run(routes._complete_turn_async("s1", SESS, Backend(error=CAP), [], "hi", 41))
    assert ("failed" not in [s for s, _ in store["status"]])


def test_a_real_backend_crash_still_fails_the_session(store):
    boom = RuntimeError("claude-cli exited before a result")
    with pytest.raises(RuntimeError):
        asyncio.run(routes._complete_turn_async("s1", SESS, Backend(error=boom), [], "hi", 41))
    assert [s for s, _ in store["status"]] == ["failed"]
