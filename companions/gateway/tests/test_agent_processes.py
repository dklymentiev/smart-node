"""The process table and the stop: what is alive now, whose it is, and how an
agent's processes end without the send loop reading a crash. In-process state
only; the kill and the database are replaced."""
import asyncio
import os
import time

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backends.cli_claude as c  # noqa: E402
import sessions_v2_routes as routes  # noqa: E402


class Proc:
    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode


def _reset():
    for d in (c._PROCESSES, c._SESSION_CONVERSATION, c._SESSION_AGENT, c._TURN_STARTED,
              c._TURN_STATUS, c._RUNAS, c._SESSION_RUNTIME, c._SESSION_TYPES, c._LAST_USED,
              c._SPAWNED_AT, c._INTERRUPTS, c._SEND_LOCKS):
        d.clear()


def _two_agents():
    _reset()
    now = time.monotonic()
    c._PROCESSES.update({"s1": Proc(101), "s2": Proc(102), "s3": Proc(103), "s4": Proc(104, returncode=0)})
    c._SESSION_CONVERSATION.update({"s1": "agent-b:desk:abc", "s2": "agent-b:runner-agent-b-1-1",
                                    "s3": "agent-a:runner-agent-a-2-2", "s4": "agent-b:runner-agent-b-0-0"})
    c._RUNAS.update({"s1": "agent-b", "s2": "agent-b", "s3": "agent-a"})
    c._SESSION_RUNTIME.update({"s1": "claude-cli", "s2": "claude-cli", "s3": "claude-cli"})
    c._SESSION_TYPES.update({"s1": "person", "s2": "background", "s3": "background"})
    c._LAST_USED.update({"s1": now - 100, "s2": now - 300, "s3": now - 5})
    c._SPAWNED_AT.update({"s1": 1000.0, "s2": 2000.0, "s3": 3000.0})
    c._TURN_STARTED["s1"] = now - 42                        # s1 is mid-turn
    c._TURN_STATUS["s1"] = {"name": "Bash", "detail": "ls /work", "ts": now - 3}


def test_the_process_table_lists_live_processes_only_with_their_turns():
    _two_agents()
    table = c.processes()
    assert [p["session_id"] for p in table] == ["s3", "s1", "s2"]     # by agent, then session
    s1 = next(p for p in table if p["session_id"] == "s1")
    assert s1["agent"] == "agent-b" and s1["os_user"] == "agent-b" and s1["pid"] == 101
    assert s1["turn"]["in_progress"] and s1["turn"]["action"] == "Bash" and s1["idle_seconds"] is None
    assert s1["session_type"] == "person" and s1["spawned_at"] == 1000.0
    s2 = next(p for p in table if p["session_id"] == "s2")
    assert s2["turn"] == {"in_progress": False} and 299 <= s2["idle_seconds"] <= 301
    assert [p["session_id"] for p in c.processes("agent-a")] == ["s3"]
    assert c.processes("ghost") == []


def test_stop_interrupts_the_turn_in_flight_and_kills_every_process_of_the_agent(monkeypatch):
    _two_agents()
    killed = []

    async def fake_kill(sid, proc, reason, os_user=None):
        killed.append((sid, proc.pid, os_user, reason))

    monkeypatch.setattr(c, "_kill_tree", fake_kill)
    out = asyncio.run(c.stop_agent("agent-b", "user:alex", "stopped from the panel"))
    assert out == {"agent": "agent-b", "interrupted": ["s1"], "closed": ["s1", "s2"]}
    assert [(k[0], k[1], k[2]) for k in killed] == [("s1", 101, "agent-b"), ("s2", 102, "agent-b")]
    assert "user:alex" in killed[0][3]
    # The turn in flight is marked, so its send loop reads the EOF as stopped
    # and clears its own bookkeeping; the idle one is forgotten here.
    assert c._INTERRUPTS["s1"]["by"] == "user:alex"
    assert "s1" in c._PROCESSES and "s2" not in c._PROCESSES and "s2" not in c._RUNAS
    # The other agent is untouched.
    assert "s3" in c._PROCESSES and c.processes("agent-a")


def test_stop_of_an_agent_without_processes_is_a_no_op():
    _two_agents()
    assert asyncio.run(c.stop_agent("ghost", "user:x")) == {"agent": "ghost", "interrupted": [], "closed": []}


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_the_route_adds_the_last_model_call_per_session(api, monkeypatch):
    _two_agents()
    asked = []

    def last_calls(sids):
        asked.append(sorted(sids))
        return {"s1": {"at": "2026-10-15T10:00:00+00:00", "model": "claude-opus-4-7",
                       "outcome": "ok", "status": 200, "duration_ms": 1200}}

    monkeypatch.setattr(routes, "_last_model_calls", last_calls)
    r = api.get("/api/v1/sessions/processes?agent=agent-b")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2 and asked == [["s1", "s2"]]
    by_sid = {p["session_id"]: p for p in body["processes"]}
    assert by_sid["s1"]["last_call"]["model"] == "claude-opus-4-7"
    assert by_sid["s2"]["last_call"] is None


def test_the_table_answers_when_the_call_history_cannot_be_read(api, monkeypatch):
    _two_agents()

    def down(sids):
        raise RuntimeError("database down")

    monkeypatch.setattr(routes, "_last_model_calls", down)
    r = api.get("/api/v1/sessions/processes")
    assert r.status_code == 200 and r.json()["count"] == 3
    assert all(p["last_call"] is None for p in r.json()["processes"])


def test_the_stop_route_calls_the_backend(api, monkeypatch):
    seen = {}

    async def fake_stop(agent, requested_by, reason):
        seen.update(agent=agent, by=requested_by, reason=reason)
        return {"agent": agent, "interrupted": [], "closed": ["s9"]}

    monkeypatch.setattr(c, "stop_agent", fake_stop)
    r = api.post("/api/v1/sessions/agent-stop", json={"agent": "agent-b", "requested_by": "user:alex"})
    assert r.status_code == 200 and r.json()["closed"] == ["s9"]
    assert seen == {"agent": "agent-b", "by": "user:alex", "reason": "stopped by owner"}


def test_a_message_to_a_stopped_agents_session_is_refused(api, monkeypatch):
    monkeypatch.setattr(routes.session_service, "get_session", lambda sid: {
        "status": "active", "model": "claude-cli/sonnet", "agent_id": 7, "tenant_id": 1, "metadata": {}})
    monkeypatch.setattr(routes.session_service, "agent_state",
                        lambda agent_id: {"slug": "agent-b", "enabled": False})
    r = api.post("/api/v1/sessions/11111111-1111-1111-1111-111111111111/messages",
                 json={"role": "user", "content": "hi"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "agent_stopped" and detail["agent"] == "agent-b"
    assert "enable it to continue" in detail["reason"]


def test_an_unreadable_agent_store_does_not_block_the_turn():
    def boom(agent_id):
        raise RuntimeError("database down")

    saved = routes.session_service.agent_state
    routes.session_service.agent_state = boom
    try:
        assert routes._agent_stopped({"agent_id": 7}) is None
        assert routes._agent_stopped({"agent_id": None}) is None
    finally:
        routes.session_service.agent_state = saved
